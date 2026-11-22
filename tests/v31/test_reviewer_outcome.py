"""One reviewer result shape for both review contracts, and a REVOKED outcome that is not a reviewer failure."""
from __future__ import annotations

import pytest

from office import contract, convergence, gates, review_parse

KINDS = ("completed", "evidence_blocked", "invalid", "unavailable", "revoked")


def _parsed(verdict):
    return review_parse.Parsed(verdict=verdict, evidence_status=None if verdict else "CAPTURE_BLOCKED")


def _build(convergence_run: bool, kind: str) -> dict:
    parsed = _parsed("APPROVED" if convergence_run else "PASS") if kind == "completed" else \
        _parsed(None) if kind == "evidence_blocked" else None
    return gates.reviewer_outcome(convergence_run, kind, summary=f"{kind} summary", parsed=parsed, route="r/m/h",
                                  dispatch_id="D1", producer_route="p/m/h", env_failures=1)


@pytest.mark.parametrize("kind,status", [("completed", "COMPLETED"), ("evidence_blocked", "EVIDENCE_BLOCKED"),
                                         ("invalid", "INVALID_RESULT"), ("unavailable", "UNAVAILABLE"),
                                         ("revoked", "REVOKED")])
def test_convergence_status_for_each_outcome_kind(kind, status):
    out = _build(True, kind)
    assert out["status"] == status
    assert out["verdict"] == ("APPROVED" if kind == "completed" else None)
    assert (out["parsed"] is not None) == (kind in ("completed", "evidence_blocked"))
    assert out["summary"] == f"{kind} summary"
    assert out.get("exhausted") is (True if kind == "unavailable" else None)
    assert out.get("revoked") is (True if kind == "revoked" else None)


@pytest.mark.parametrize("kind,verdict", [("completed", "PASS"), ("invalid", "ATTENTION"),
                                          ("unavailable", "UNAVAILABLE"), ("revoked", "REVOKED")])
def test_legacy_verdict_for_each_outcome_kind(kind, verdict):
    out = _build(False, kind)
    assert out["verdict"] == verdict
    assert "status" not in out and "exhausted" not in out and "env_failures" not in out


def test_legacy_evidence_blocked_has_no_verdict_and_no_status():
    out = _build(False, "evidence_blocked")
    assert out["verdict"] is None and "status" not in out


def test_revoked_is_distinct_from_unavailable_and_both_settle_as_no_answer():
    assert contract.REVOKED != contract.UNAVAILABLE
    assert contract.REVOKED in contract.STATUSES and "REVOKED" in contract.LEGACY_VERDICTS
    assert set(contract.NO_ANSWER) == {contract.UNAVAILABLE, contract.REVOKED}
    assert contract.REVOKED in convergence.FALLBACK_STATUSES


@pytest.mark.parametrize("gate", [{"verdict": "REVOKED"}, {"verdict": None, "review_status": "REVOKED"},
                                  {"verdict": "UNAVAILABLE"}])
def test_revoked_review_counts_as_a_review_with_no_verdict(gate):
    assert gates.review_unavailable(gate)


def test_a_completed_review_is_not_unavailable():
    assert not gates.review_unavailable({"verdict": "PASS", "review_status": None})
    assert not gates.review_unavailable({"verdict": None, "review_status": "COMPLETED"})


def _plan_review_status(env, outcome_status: str) -> tuple[dict, str]:
    """Start a run, record one plan-review outcome as the job would, return (plan_review state, status text)."""
    import json

    from conftest import PLAN_ONE
    from office import db, plans, state
    manual = {"OFFICE_JOBS": "manual"}
    env.trust()
    env.office("start", "fixture goal", "--gear", "L", "--planner", "inline", check=0, env=manual)
    env.write_plan(PLAN_ONE)
    env.office("submit", check=0, env=manual)
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()["id"])
        gid = [g for g in plans.plan_gates(con, run["id"]) if g["status"] in ("queued", "running")][-1]["id"]
        with db.transaction(con):
            plans._ingest_convergence(con, run, gid, {"status": outcome_status, "verdict": None, "route": "fake/r",
                                                       "dispatch_id": None, "summary": outcome_status})
        row = con.execute("SELECT plan_review_json FROM runs WHERE id=?", (run["id"],)).fetchone()
        pr = json.loads(row["plan_review_json"])
    finally:
        con.close()
    code, out = env.office("status", check=0, env=manual)
    return pr, out


def test_revoked_plan_review_is_named_a_revoke_in_state_and_status(env):
    pr, out = _plan_review_status(env, contract.REVOKED)
    assert pr["status"] == "unavailable" and pr["revoked"] is True  # resume still requeues it
    assert "REVOKED" in out and "not a reviewer failure" in out


def test_unavailable_plan_review_is_not_named_a_revoke(env):
    pr, out = _plan_review_status(env, contract.UNAVAILABLE)
    assert pr["revoked"] is False
    assert "REVOKED" not in out and "UNAVAILABLE" in out
