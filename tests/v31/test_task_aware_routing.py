"""#415 regression tests: per-task descriptors, benchmark fit, and tiered costs."""
import pytest
from office import adaptive, db, planfile, route_learning, task_descriptors

IDX = "Artificial Analysis Intelligence Index v4.3.2"


def candidate(model="m", scores=(), tiered=False):
    price = {"input_per_mtok": 0.1, "output_per_mtok": 0.5}
    if tiered:
        price.update(prompt_token_threshold=100000,
                     above_threshold_input_per_mtok=0.5, above_threshold_output_per_mtok=2.5)
    return {"harness": "claude", "harness_version": "2.0", "model_id": model,
            "invocation_model_id": model, "effort": "medium", "capabilities": ["builder"],
            "benchmark_indexes": {IDX: 42}, "task_benchmarks": list(scores),
            "price_fields": price, "cost": {"money_estimate": 0.5}, "quota": {"status": "unknown"}}


def benchmark(model="m", dimension="ui_appearance", delta=0.6, confidence=0.8):
    return {"dimension": dimension, "benchmark_name": "held-out-tasks",
            "benchmark_version": "v1", "snapshot_date": "2026-10-08",
            "source_url": "https://example.org/eval", "normalization": "held-out",
            "calibration_version": "415-replay-v1", "model_id": model, "effort": "medium",
            "calibrated_log_odds_delta": delta, "confidence": confidence}


def score(c, descriptor=None):
    context = {"task_descriptor": descriptor} if descriptor else {}
    return adaptive.score([c], {"context": context, "evidence": {"routes": {}, "pooling": {"prior_strength": 8}},
                                "policy": {}}, adaptive.settings(None))[0]


def test_parse_optional_task_fields():
    plan = ("## Requirements\ndone:\n- updated\n## Tasks\n### T1: hero\nscope: src/ui.py\n"
            "depends: none\nchecks: none\nvisual: none\naccept:\n- hero works\n"
            "domain: ui\nquality: taste\nwork: implementation\nmodality: visual\n"
            "task_size: M\nestimated_input_tokens: 100001\nestimated_output_tokens: 7500\n")
    parsed = planfile.parse(plan)
    assert not parsed.errors, parsed.errors
    assert parsed.tasks[0]["descriptor"]["estimated_input_tokens"] == 100001
    assert parsed.tasks[0]["descriptor"]["domain"] == "ui"
    assert planfile.parse(plan.replace("domain: ui", "domain: impossible")).errors
    assert not planfile.parse(plan.replace("domain: ui\n", "")).tasks[0]["descriptor"].get("domain")


@pytest.mark.parametrize("tokens,tier,unit", [
    (100000, "standard", 0.5),
    (100001, "above-threshold", 2.5),
    (None, "unknown-conservative", 2.5),
])
def test_tier_boundary(tokens, tier, unit):
    desc = {} if tokens is None else {"estimated_input_tokens": tokens}
    chosen = task_descriptors.price_tier(candidate(tiered=True)["price_fields"], desc)
    assert chosen["tier"] == tier and chosen["output_per_mtok"] == unit


def test_long_context_cost_is_real_and_unknown_is_conservative():
    c, settings = candidate(tiered=True), adaptive.settings(None)
    short = adaptive._attempt_cost_prior(c, settings, {"estimated_input_tokens": 100000,
                                                         "estimated_output_tokens": 10000})
    long = adaptive._attempt_cost_prior(c, settings, {"estimated_input_tokens": 100001,
                                                        "estimated_output_tokens": 10000})
    assert 0 < short < long
    assert adaptive._attempt_cost_prior(c, settings, {}) > 0


def test_matching_calibrated_benchmark_changes_prior_not_legacy():
    c = candidate(scores=[benchmark()])
    ui = score(c, {"domain": "ui", "quality": "taste"})
    api = score(c, {"domain": "backend", "quality": "objective"})
    legacy = score(c)
    assert ui["benchmark"]["prior_p"] > api["benchmark"]["prior_p"]
    assert api["benchmark"]["prior_p"] == legacy["benchmark"]["prior_p"]
    assert ui["benchmark"]["task_fit"]["applied"][0]["dimension"] == "ui_appearance"


def test_family_dedupe_and_missing_provenance():
    missing = {"dimension": "ui_appearance", "calibrated_log_odds_delta": 99}
    c = candidate(scores=[benchmark(delta=0.2, confidence=0.4),
                          benchmark(delta=0.7, confidence=0.9), missing])
    fit = task_descriptors.benchmark_fit(c, {"domain": "ui", "quality": "taste"})
    assert len(fit["applied"]) == 1
    assert fit["log_odds_adjustment"] == pytest.approx(0.7)
    assert fit["ignored"]


def test_descriptor_mismatch_discounts_without_hard_filter():
    cfg = route_learning.DEFAULTS
    context = {"task_descriptor": {"domain": "backend", "modality": "code"}}
    exact = route_learning.comparability({"descriptor": {"domain": "backend", "modality": "code"}}, context, cfg)
    other = route_learning.comparability({"descriptor": {"domain": "ui", "modality": "visual"}}, context, cfg)
    unknown = route_learning.comparability({"descriptor": {}}, context, cfg)
    assert exact == 1.0 and 0 < other < exact and 0 < unknown < exact


def test_long_context_spend_never_uses_short_history():
    c = candidate(tiered=True)
    key = route_learning.candidate_key(c)
    past = {"run_id": "R1", "task_id": "T1", "role": "worker", "route": key,
            "harness_major": "2", "playbook": "Change", "kind": "fresh",
            "success": True, "attribution": "route", "learn_weight": 1.0,
            "attempts": 1, "review_rounds": 0, "money_actual": 0.01,
            "descriptor": {"estimated_input_tokens": 90000}, "ended_at": "2026-10-07T00:00:00Z"}
    ctx = {"playbook": "Change", "task_descriptor": {"estimated_input_tokens": 150000}}
    evidence = route_learning.evidence_for([past], [c], ctx, as_of="2026-10-08T00:00:00Z")
    assert evidence["routes"][key]["n_effective"] > 0
    assert evidence["routes"][key]["money_actual_median"] is None
    past["descriptor"]["estimated_input_tokens"] = 150001
    matched = route_learning.evidence_for([past], [c], ctx, as_of="2026-10-08T00:00:00Z")
    assert matched["routes"][key]["money_actual_median"] == 0.01


def test_db_schema_adds_nullable_task_and_dispatch_snapshots(tmp_path):
    con = db.connect(tmp_path / "run.db")
    for table in ("tasks", "dispatches"):
        assert "descriptor_json" in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    con.close()
