"""Risk-triggered integrated review (#422), end to end through the real CLI.

Lane review stays the normal unit; a shared interface, cross-lane acceptance
dependency, shared registry or high integration risk adds one review bound to the
composed scope; every case records why it was required or skipped.
"""
from __future__ import annotations

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, PLAN_TWO
from test_convergence_contract import (APPROVED, _events, _gates, _q, _run_row, _scope, _start, _status, finding,
                                       recheck)

WORK = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}


def _two(env, plan, *reviews, **extra):
    _start(env, plan=plan, executor=[{"write_by_task": WORK, "submit": True}],
           convergence_reviewer=[{"reply": r} for r in reviews or [APPROVED]], **extra)
    env.office("dispatch", "T1", "T2", "--parallel", check=0)


def _scopes(env):
    return sorted({g["scope"] for g in _gates(env, "convergence_review")})


def _decision(env):
    return _run_row(env)["landing"]["integrated_review"]


def _receipt(env):
    code, data = env.ojson("inspect", "convergence")
    return (data.get("data") or data)["integrated_review"]


def test_independent_lanes_get_no_integrated_review_and_a_recorded_reason(env):
    _two(env, PLAN_TWO)
    assert _scopes(env) == ["L-T1", "L-T2"]
    d = _decision(env)
    assert d["required"] is False and "independent" in d["reason"] and d["scopes"] == []
    assert _events(env, "convergence.integrated_review")
    assert _receipt(env)["required"] is False


def test_shared_interface_lanes_get_one_integrated_review(env):
    plan = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\ninterfaces: provides add\n").replace(
        "scope: mul.py\n", "scope: mul.py\ninterfaces: consumes add\n")
    _two(env, plan)
    assert _scopes(env) == ["L-T1", "L-T2", "S-T1+T2"]
    d = _decision(env)
    assert d["required"] and d["scopes"][0]["triggers"] == ["interface"]
    gate = [g for g in _gates(env, "convergence_review") if g["scope"] == "S-T1+T2"][0]
    assert gate["input_key"].startswith("S-T1+T2:") and gate["verdict"] == "APPROVED"
    assert gate["input_key"].split(":")[1] == _scope(env, "S-T1+T2")["commit"], "bound to the composed revision"
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"


def test_cross_lane_acceptance_dependency_triggers_it(env):
    plan = PLAN_TWO.replace("depends: none\nchecks: python3 -c \"import mul",
                            "depends: none\naccept_needs: T1\nchecks: python3 -c \"import mul")
    _two(env, plan)
    assert _scopes(env) == ["L-T1", "L-T2", "S-T1+T2"]
    assert _decision(env)["scopes"][0]["triggers"] == ["acceptance"]
    assert "acceptance depends on T1" in _receipt(env)["reason"]


def test_accept_needs_unknown_task_is_a_plan_error():
    from office import planfile
    bad = planfile.parse(PLAN_TWO.replace("depends: none\nchecks: python3 -c \"import mul",
                                         "depends: none\naccept_needs: T9\nchecks: python3 -c \"import mul"))
    assert any("accept_needs" in e for e in bad.errors)
    bad = planfile.parse(PLAN_TWO.replace("scope: mul.py\n", "scope: mul.py\nintegration_risk: maybe\n"))
    assert any("integration_risk" in e for e in bad.errors)


def test_emergent_cross_lane_failure_is_found_by_the_integrated_review(env):
    plan = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\ninterfaces: provides add\n").replace(
        "scope: mul.py\n", "scope: mul.py\ninterfaces: consumes add\n")
    _two(env, plan, APPROVED, APPROVED, recheck(finding("F1", "T2", where="mul.py:1", what="mul ignores add's contract")))
    assert _scope(env, "L-T1")["status"] == "approved" and _scope(env, "L-T2")["status"] == "approved"
    assert _scope(env, "S-T1+T2")["status"] == "recheck"
    assert _status(env)["data"]["tasks"]["T2"] == "changes_required"
    assert (_run_row(env)["landing"].get("integration") or {}).get("status") != "accepted"


def test_high_plan_declared_integration_risk_reviews_independent_lanes(env):
    plan = PLAN_TWO.replace("scope: mul.py\n", "scope: mul.py\nintegration_risk: high\n")
    _two(env, plan)
    assert _scopes(env) == ["L-T1", "L-T2", "S-T1+T2"]
    d = _decision(env)
    assert d["scopes"][0]["triggers"] == ["risk"] and "plan declares it on T2" in _receipt(env)["reason"]


def test_high_run_risk_marker_is_read(env):
    _start(env, plan=PLAN_TWO, extra=("--blast-radius", "production"),
           executor=[{"write_by_task": WORK, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)
    assert _run_row(env)["risk_json"] and '"high": true' in _run_row(env)["risk_json"], _run_row(env)["risk_json"]
    assert "S-T1+T2" in _scopes(env)
    assert "high risk" in _receipt(env)["reason"]


def test_high_risk_single_lane_is_covered_by_the_lane_review(env):
    _start(env, plan=PLAN_ONE.replace("scope: calc.py\n", "scope: calc.py\nintegration_risk: high\n"),
           executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _scopes(env) == ["L-T1"]
    d = _decision(env)
    assert d["required"] is False and "one lane" in d["reason"]


def test_exact_covered_result_is_not_reviewed_again(env):
    from office import convergence, contract, db, state
    plan = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\ninterfaces: provides add\n").replace(
        "scope: mul.py\n", "scope: mul.py\ninterfaces: consumes add\n")
    _two(env, plan)
    con = env.con()
    try:
        run = state.get_run(con, _run_row(env)["id"])
        scope = convergence.find_scope(con, run, "S-T1+T2")
        st = convergence.scope_state(run, "S-T1+T2")
        assert st["status"] == "approved"
        # Another scope with the same task set over the identical tree is covered.
        assert convergence._covered_by(con, run, {"id": "S-other", "tasks": scope["tasks"]},
                                       {"tree": st["tree"]}) == "S-T1+T2"
        assert convergence._covered_by(con, run, {"id": "S-other", "tasks": scope["tasks"] + ["T3"]},
                                       {"tree": st["tree"]}) is None
        assert convergence._covered_by(con, run, {"id": "S-other", "tasks": scope["tasks"]},
                                       {"tree": "0" * 40}) is None
    finally:
        con.close()


def test_covered_shared_scope_converges_without_a_second_review(env, monkeypatch):
    """job_converge skips the review when a covering independent review exists."""
    from office import convergence
    monkeypatch.setattr(convergence, "_covered_by", lambda *a, **k: "L-X")
    plan = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\ninterfaces: provides add\n").replace(
        "scope: mul.py\n", "scope: mul.py\ninterfaces: consumes add\n")
    _two(env, plan)
    assert _scopes(env) == ["L-T1", "L-T2"], "no shared-scope review ran"
    st = _scope(env, "S-T1+T2")
    assert st["status"] == "not_required" and st["covered_by"] == "L-X" and "already covered" in st["basis"]
    assert _events(env, "convergence.covered")
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"
