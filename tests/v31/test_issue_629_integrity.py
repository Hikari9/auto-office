"""Regression coverage for review/validation integrity umbrella #629."""
from __future__ import annotations

from pathlib import Path

from conftest import PLAN_ONE
from test_plan_review_lifecycle import _start, _revise, _ingest, _plan_gates, _run, _q, recheck


def test_superseded_older_review_cannot_erase_current_findings_or_spend_round(env):
    from office import db, plans, review_parse, state
    _start(env)
    first = _plan_gates(env)[0]["id"]
    _revise(env, PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (p2)"))
    second = _plan_gates(env)[-1]["id"]
    _ingest(env, recheck(code="P2", what="newer plan must retain its blocker"), gate_id=second)
    assert _run(env)["plan_review"]["status"] == "recheck"
    con = env.con()
    try:
        run = state.get_run(con, _run(env)["id"])
        old = review_parse.parse(recheck(code="P1", what="stale finding"), plan_review=True, contract="convergence-v1")
        with db.transaction(con):
            plans.ingest_plan_review(con, run, first, {"status": "COMPLETED", "verdict": "RECHECK",
                "parsed": old, "route": "fake/old", "summary": "stale older review"})
        gates = {g["id"]: g for g in plans.plan_gates(con, run["id"])}
        assert gates[first]["status"] == "stale"
        assert plans.substantive_rounds(con, run) == 1
        assert {f["code"] for f in plans.blocking_findings(con, run)} == {"P2"}
        assert state.get_run(con, run["id"])["plan_review"]["status"] == "recheck"¶»§q«^