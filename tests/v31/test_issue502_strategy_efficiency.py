"""Focused, no-harness regressions for #502 shadow economics."""
import importlib.util
from pathlib import Path

from office import delegation_shadow, economics_efficiency


def task(id, scope, depends=()):
    return {"id": id, "scope": scope, "depends": None if depends is None else list(depends)}


def test_shadow_never_changes_actual_parallel_request_or_steals_authority():
    r = delegation_shadow.propose([task("T1", ["src/a"]), task("T2", ["src/b"])], parallel_requested=False)
    assert r["proposal"] == "independent-lanes" and r["actual"] == "serial"
    assert r["mode"] == "shadow" and r["policy_effect"].startswith("none:")
    assert r["whole_run_cost"].startswith("unknown:")


def test_dependencies_overlapping_scopes_and_directory_ancestors_are_coupled():
    t = [task("T1", ["src/office"]), task("T2", ["src/office/db.py"], ["T1"])]
    r = delegation_shadow.propose(t, parallel_requested=True)
    assert r["proposal"] == "cohesive-or-serial" and r["actual"] == "parallel"
    assert {e["kind"] for e in r["conflicts"]} == {"dependency", "scope-overlap"}


def test_glob_scopes_overlapping_a_concrete_file_are_coupled():
    r = delegation_shadow.propose(
        [task("T1", ["src/*.py"]), task("T2", ["src/foo.py"])],
        parallel_requested=True,
    )
    assert r["proposal"] == "cohesive-or-serial"
    assert r["conflicts"] == [{"tasks": ["T1", "T2"], "kind": "scope-overlap"}]


def test_absolute_scope_is_unresolved_instead_of_claiming_independence():
    r = delegation_shadow.propose(
        [task("T1", ["/Users/rico/Git/repo/src/foo.py"]), task("T2", ["src/foo.py"])],
        parallel_requested=True,
    )
    assert r["proposal"] == "unresolved"
    assert r["actual"] == "parallel"


def test_trailing_slash_scope_normalizes_to_the_same_file():
    r = delegation_shadow.propose(
        [task("T1", ["src/foo.py/"]), task("T2", ["src/foo.py"])],
        parallel_requested=True,
    )
    assert r["proposal"] == "cohesive-or-serial"
    assert r["conflicts"] == [{"tasks": ["T1", "T2"], "kind": "scope-overlap"}]


def test_missing_scope_and_duplicate_ids_fail_to_observe_not_to_veto():
    r = delegation_shadow.propose([task("T1", []), task("T1", ["src/b"])], parallel_requested=True)
    assert r["proposal"] == "unresolved" and r["actual"] == "parallel"


def test_unknown_dependency_evidence_does_not_claim_independence():
    r = delegation_shadow.propose([task("T1", ["a"], None), task("T2", ["b"])], parallel_requested=True)
    assert r["proposal"] == "unresolved" and r["actual"] == "parallel"


def test_unknown_cache_neutral_and_verified_cache_still_only_advisory():
    tasks = [task("T1", ["a"]), task("T2", ["b"])]
    r = delegation_shadow.propose(tasks, parallel_requested=True, cache_policy={"ttl": 300})
    assert r["cache_policy"] == "unknown-neutral" and r["cache_source"] is None
    r = delegation_shadow.propose(tasks, parallel_requested=True, cache_policy={
        "verified": True, "auth_mode": "subscription", "source": "harness receipt"})
    assert r["cache_policy"] == "verified-soft-context-only" and r["actual"] == "parallel"


def test_efficiency_partial_and_root_missing_not_zero():
    rows = [{"role": "root", "attribution": "office", "dispatch_id": None,
             "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": None,
             "cache_write_tokens": None, "cost_kind": "unknown", "cost_usd": None},
            {"role": "executor", "attribution": "office", "dispatch_id": "d1",
             "input_tokens": 200, "output_tokens": 80, "cache_read_tokens": 20,
             "cache_write_tokens": 0, "cost_kind": "estimated-nominal", "cost_usd": 1.23},
            {"role": "root", "attribution": "outside", "dispatch_id": None,
             "input_tokens": 10000, "output_tokens": 2000, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "cost_kind": "actual", "cost_usd": 90}]
    s = economics_efficiency.summarize(rows, total_dispatches=2)
    assert s["rows"] == 2 and s["outside_rows"] == 1
    assert s["token_coverage"] == "partial-or-unmeasured"
    assert s["dispatch_coverage"] == "partial-or-unknown"
    assert s["actual_usd_observed"] is None and s["root_share_of_actual_usd"] is None
    assert s["root_share_of_observed_token_components"] == 150 / 450
    assert s["cache_coverage"] == {"cache_read_tokens": "1/2", "cache_write_tokens": "1/2"}


def test_malformed_token_values_are_unknown_not_measured():
    rows = [{"role": "root", "attribution": "office", "input_tokens": True, "output_tokens": 1,
             "cache_read_tokens": None, "cache_write_tokens": 0},
            {"role": "executor", "attribution": "office", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0}]
    s = economics_efficiency.summarize(rows)
    assert s["missing_token_fields"]["input_tokens"] == 1
    assert s["token_components"]["input_tokens"] == 0
    assert s["token_coverage"] == "partial-or-unmeasured"
    assert s["root_share_of_observed_token_components"] == 1


def test_complete_recorded_actual_cost_is_distinct_from_subscription_quota():
    a = {"role": "root", "attribution": "office", "dispatch_id": None,
         "input_tokens": 50, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "cost_kind": "actual", "cost_usd": 2}
    b = {**a, "role": "reviewer", "dispatch_id": "r1", "cost_usd": 3}
    s = economics_efficiency.summarize([a, b], total_dispatches=1)
    assert s["root_share_of_actual_usd"] == 0.4 and s["actual_usd_observed"] == 5
    b["cost_kind"] = "quota"
    assert economics_efficiency.summarize([a, b], total_dispatches=1)["root_share_of_actual_usd"] is None


def test_matched_evaluation_requires_quality_and_complete_metrics():
    path = Path(__file__).resolve().parents[2] / "eval/v31/compare_economics.py"
    spec = importlib.util.spec_from_file_location("office502_compare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def arm(pair, name, complete=True, defects=0):
        return {"pair_id": pair, "arm": name, "task_shape": "independent", "harness": "codex",
                "billing_mode": "subscription", "verified_completion": True,
                "integrated_defects": defects, "metrics": {"total_tokens": 10 if name == "baseline" else 8},
                "complete_metrics": ["total_tokens"] if complete else []}
    one = module.compare([arm("1", "baseline"), arm("1", "treatment")])
    assert one["paired"] == 1 and one["metrics"]["total_tokens"]["change_pct"] is None
    rows = [arm(str(i), a) for i in range(3) for a in ("baseline", "treatment")]
    three = module.compare(rows)
    assert three["metrics"]["total_tokens"]["change_pct"] == -20
    assert three["metrics"]["total_tokens"]["claim_ready"]
    for row in rows:
        if row["arm"] == "treatment":
            row["metrics"]["total_tokens"] = 12
    assert not module.compare(rows)["metrics"]["total_tokens"]["claim_ready"]
    for row in rows:
        if row["arm"] == "treatment":
            row["metrics"]["total_tokens"] = 8
    rows[-1]["integrated_defects"] = 1
    assert not module.compare(rows)["metrics"]["total_tokens"]["claim_ready"]
    rows[-1]["complete_metrics"] = []
    assert module.compare(rows)["metrics"]["total_tokens"]["change_pct"] is None
    malformed = [dict(row, complete_metrics="total_tokens", integrated_defects=0) for row in rows]
    assert not module.compare(malformed)["metrics"]["total_tokens"]["claim_ready"]


def test_actual_usd_requires_api_billing_and_actual_cost_kinds():
    path = Path(__file__).resolve().parents[2] / "eval/v31/compare_economics.py"
    spec = importlib.util.spec_from_file_location("office502_compare_usd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def arm(pair, name, billing="api", cost_kind="actual"):
        return {"pair_id": pair, "arm": name, "task_shape": "independent", "harness": "codex",
                "billing_mode": billing, "verified_completion": True, "integrated_defects": 0,
                "metrics": {"actual_usd": 10 if name == "baseline" else 8},
                "complete_metrics": ["actual_usd"], "cost_kind": cost_kind}

    def comparison(rows):
        return module.compare(rows)["metrics"]["actual_usd"]

    api_rows = [arm(str(i), a) for i in range(3) for a in ("baseline", "treatment")]
    assert comparison(api_rows)["claim_ready"]
    subscription_rows = [arm(str(i), a, billing="subscription")
                         for i in range(3) for a in ("baseline", "treatment")]
    assert comparison(subscription_rows)["matched"] == 0
    estimated_rows = [arm(str(i), a, cost_kind="estimated-nominal")
                      for i in range(3) for a in ("baseline", "treatment")]
    assert comparison(estimated_rows)["matched"] == 0


def test_matched_evaluation_rejects_malformed_json_shapes():
    path = Path(__file__).resolve().parents[2] / "eval/v31/compare_economics.py"
    spec = importlib.util.spec_from_file_location("office502_compare_input", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.compare({"pair_id": "1"})["error"] == "input must be a JSON array"
    assert module.compare([None])["error"] == "each array item must be an object"
    valid = [{"pair_id": str(i), "arm": arm, "task_shape": "x", "harness": "x",
              "billing_mode": "subscription", "verified_completion": True, "integrated_defects": 0,
              "complete_metrics": ["total_tokens"], "metrics": {"total_tokens": 10 if arm == "baseline" else 8}}
             for i in range(3) for arm in ("baseline", "treatment")]
    malformed_identity = module.compare(valid + [{**valid[0], "pair_id": None}])
    assert malformed_identity == {
        "error": "each row must have a non-empty pair_id and valid arm", "rows": [6]
    }
    malformed_arm = module.compare(valid + [{**valid[0], "pair_id": "extra", "arm": "control"}])
    assert malformed_arm == {
        "error": "each row must have a non-empty pair_id and valid arm", "rows": [6]
    }
    whitespace_pair = module.compare(valid + [{**valid[0], "pair_id": " "}])
    assert whitespace_pair == {
        "error": "each row must have a non-empty pair_id and valid arm", "rows": [6]
    }
    malformed_metrics = [{"pair_id": "1", "arm": arm, "task_shape": "x", "harness": "x",
                          "billing_mode": "subscription", "verified_completion": True,
                          "integrated_defects": 0, "complete_metrics": ["total_tokens"], "metrics": ["bad"]}
                         for arm in ("baseline", "treatment")]
    assert module.compare(malformed_metrics)["metrics"]["total_tokens"]["matched"] == 0
