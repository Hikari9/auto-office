"""#811 complete published Terra roster and exact-lineage supersession behavior."""
from office import shipped_catalog, adaptive, config, paths

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


def test_six_terra_rows_are_present_and_only_probed_efforts_dispatch():
    catalog = shipped_catalog.load()["models"]
    expected = {"none": 33, "low": 36, "medium": 41, "high": 44, "xhigh": 45, "max": 48}
    found = {r["effort"]: r for r in catalog if r["model_id"] == "gpt-6.1-terra"}
    assert set(found) == set(expected)
    assert {e: r["benchmark_indexes"][INDEX] for e, r in found.items()} == expected
    probed = {"low", "medium", "high"}
    for effort in probed:
        row = found[effort]
        assert row.get("dispatchable") is not False
        assert str(row["invocation_source"]).startswith("local-evidence:")
        assert f"at effort {effort}" in row["invocation_source"]
    assert all(found[e].get("dispatchable") is False and found[e].get("discovery") == "eligible"
               for e in set(found) - probed)


def recommend(rows, **extra):
    request = {"role": "executor", "policy": {"quota_reserve_percent": 5.0},
               "routing_seed": "no-exploration", "run_id": "test", "task_id": "T1",
               "adaptive_config": {"exploration": {"rate": 0.0}}, "preferred_seed": [],
               "cost_policy": "balanced", **extra}
    return adaptive.recommend(rows, request)


def models(rec):
    return [r["route"].split("/", 1)[1] for r in rec["rows"]]


def test_supersession_orders_only_same_lineage_effort_and_harness():
    for model in ("sol", "terra"):
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            old, new = row(f"gpt-5.6-{model}", effort, score=55), row(f"gpt-6.1-{model}", effort, score=40)
            rec = recommend([old, new])
            # 5.6 scores better, yet 6.1 leads and 5.6 stays as its fallback.
            assert models(rec) == [f"gpt-6.1-{model}@{effort}", f"gpt-5.6-{model}@{effort}"]
            moved = rec["audit"]["lineage_supersession"]
            assert [(m["label"], m["supersedes_label"]) for m in moved] == [
                (f"codex/gpt-6.1-{model}@{effort}", f"codex/gpt-5.6-{model}@{effort}")]
            assert "lineage_supersession" not in recommend([old])["audit"]
            assert "lineage_supersession" not in recommend([new])["audit"]


def test_no_cross_lineage_effort_harness_or_unlisted_supersession():
    old = row("gpt-5.6-sol", "low", score=55)
    modern = row("gpt-6.1-sol", "high")
    another = row("gpt-6.1-terra", "low")
    unlisted = {**row("gpt-6.1-sol", "low"), "model_id": "gpt-7-sol", "invocation_model_id": "gpt-7-sol"}
    assert "lineage_supersession" not in recommend([old, modern, another, unlisted])["audit"]
    different_harness = {**row("gpt-6.1-sol", "low"), "harness": "agy"}
    assert "lineage_supersession" not in recommend([old, different_harness])["audit"]
    # Only Sol and Terra are listed: Luna generations keep the utility order.
    luna_old = row("gpt-5.6-luna", "high", lineage="openai/gpt-luna", version="5.6", score=55)
    luna_new = row("gpt-6-luna", "high", lineage="openai/gpt-luna", version="6", score=40)
    assert "lineage_supersession" not in recommend([luna_old, luna_new])["audit"]


def test_explicit_user_preference_or_route_preserves_old_model():
    old, new = row("gpt-5.6-sol", "high", score=55), row("gpt-6.1-sol", "high", score=40)
    seeded = recommend([old, new], preferred_seed=[{"model_id": "gpt-5.6-sol", "effort": "high"}])
    assert models(seeded)[0] == "gpt-5.6-sol@high" and "lineage_supersession" not in seeded["audit"]
    for key in ("manual_route", "declared_route", "exact_route"):
        rec = recommend([old, new], **{key: "codex@1/gpt-5.6-sol@high"})
        assert models(rec)[0] == "gpt-5.6-sol@high" and "lineage_supersession" not in rec["audit"]


def test_adaptive_prefers_newer_only_within_comparable_qualified_lineage():
    old = row("gpt-5.6-terra", "high", score=55)
    new = row("gpt-6.1-terra", "high", score=44)
    other = row("gpt-6.1-sol", "high", score=50)
    rec = recommend([old, new, other])
    # 5.6 Terra scores best, but 6.1 Terra competes with 6.1 Sol on its own utility:
    # it never inherits its predecessor's place ahead of a stronger other lineage.
    assert models(rec) == ["gpt-6.1-sol@high", "gpt-6.1-terra@high", "gpt-5.6-terra@high"]
    assert rec["audit"]["lineage_supersession"][0]["label"] == "codex/gpt-6.1-terra@high"
    retry = recommend([old, other])
    assert "gpt-5.6-terra@high" in models(retry) and "lineage_supersession" not in retry["audit"]


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
    assert models(rec) == ["gpt-5.6-sol@medium"]
    assert any("budget ceiling" in r["reason"] for r in rec["rejected"])
    assert "lineage_supersession" not in rec["audit"]
