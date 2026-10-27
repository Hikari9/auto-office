"""#811 complete published Terra roster and exact-lineage supersession behavior."""
from office import adaptive, config, model_lineages, paths

INDEX = "Artificial Analysis Intelligence Index v4.3.2"


def row(model, effort, *, lineage=None, version=None, score=48):
    lineage = lineage or ("openai/gpt-sol" if "sol" in model else "openai/gpt-terra")
    version = version or ("6.1" if "6.1" in model else "5.6")
    return {"model_id": model, "invocation_model_id": model, "harness": "codex",
            "harness_version": "1.0", "effort": effort, "lineage_key": lineage,
            "generation_version": version, "capabilities": ["builder"],
            "benchmark_indexes": {INDEX: score},
            "price_fields": {"input_per_mtok": 1.0, "output_per_mtok": 2.0},
            "quota": {"status": "ok", "tightest_remaining_percent": 100}}


def test_six_terra_rows_are_present_but_do_not_fake_conformance():
    catalog = config.load_yaml(paths.resources_root() / "catalog" / "seed.yaml")["models"]
    expected = {"none": 33, "low": 36, "medium": 41, "high": 44, "xhigh": 45, "max": 48}
    found = {r["effort"]: r for r in catalog if r["model_id"] == "gpt-6.1-terra"}
    assert set(found) == set(expected)
    assert {e: r["benchmark_indexes"][INDEX] for e, r in found.items()} == expected
    assert all(r.get("dispatchable") is False and r.get("discovery") == "eligible"
               for r in found.values())


def test_supersession_replaces_only_same_lineage_effort_and_harness():
    for model in ("sol", "terra"):
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            old, new = row(f"gpt-5.6-{model}", effort), row(f"gpt-6.1-{model}", effort)
            active, suppressed = model_lineages.prefer_qualified_generation([old, new])
            assert active == [new] and len(suppressed) == 1
            assert suppressed[0]["model_id"] == old["model_id"]
            assert "recomputes" in suppressed[0]["reason"]
            assert model_lineages.prefer_qualified_generation([old])[0] == [old]
            assert model_lineages.prefer_qualified_generation([new])[0] == [new]


def test_no_cross_lineage_or_effort_supersession_or_unverified_generation():
    old = row("gpt-5.6-sol", "low")
    modern = row("gpt-6.1-sol", "high")
    another = row("gpt-6.1-terra", "low")
    missing = {**row("gpt-6.1-sol", "low"), "generation_version": None}
    active, dropped = model_lineages.prefer_qualified_generation([old, modern, another, missing])
    assert active == [old, modern, another, missing] and not dropped
    different_harness = {**row("gpt-6.1-sol", "low"), "harness": "agy"}
    active, _ = model_lineages.prefer_qualified_generation([old, different_harness])
    assert len(active) == 2


def test_explicit_user_preference_can_preserve_old_model():
    old, new = row("gpt-5.6-sol", "high"), row("gpt-6.1-sol", "high")
    active, dropped = model_lineages.prefer_qualified_generation(
        [old, new], preserved={"openai/gpt-sol"})
    assert active == [old, new] and not dropped


def test_adaptive_prefers_newer_only_within_comparable_qualified_lineage():
    old = row("gpt-5.6-terra", "high", score=55)
    new = row("gpt-6.1-terra", "high", score=44)
    other = row("gpt-6.1-sol", "high", score=50)
    request = {"role": "executor", "policy": {"quota_reserve_percent": 5.0},
               "routing_seed": "no-exploration", "run_id": "test", "task_id": "T1",
               "preferred_seed": [], "cost_policy": "balanced"}
    rec = adaptive.recommend([old, new, other], request)
    assert old["model_id"] not in [r["model_id"] for r in rec["by_id"].values()]
    assert rec["audit"]["lineage_supersession"][0]["newer_model_id"] == new["model_id"]
    assert set(c["model_id"] for c in rec["by_id"].values()) == {new["model_id"], other["model_id"]}
    retry = adaptive.recommend([old, other], request)
    assert old["model_id"] in [r["model_id"] for r in retry["by_id"].values()]
    pinned = {**request, "preferred_seed": [{"model_id": "gpt-5.6-terra", "effort": "high"}]}
    kept = adaptive.recommend([old, new], pinned)
    assert len(kept["by_id"]) == 2


def test_budget_ineligible_newer_does_not_suppress_affordable_older():
    old = row("gpt-5.6-sol", "medium", score=42)
    new = row("gpt-6.1-sol", "medium", score=48)
    new["price_fields"] = {"input_per_mtok": 1000.0, "output_per_mtok": 5000.0}
    req = {"role": "executor", "policy": {"quota_reserve_percent": 5.0},
           "run_id": "budget", "task_id": "T2", "routing_seed": "budget-case",
           "adaptive_config": {"budget_ceiling_usd": 5.0,
                               "exploration": {"rate": 0.0}},
           "cost_policy": "balanced"}
    rec = adaptive.recommend([old, new], req)
    assert [r["model_id"] for r in rec["by_id"].values()] == ["gpt-5.6-sol"]
    assert any("budget ceiling" in r["reason"] for r in rec["rejected"])
    assert not rec["audit"]["lineage_supersession"]
