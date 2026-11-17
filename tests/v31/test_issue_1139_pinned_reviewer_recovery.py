"""#1139: a walled or failing pinned reviewer recovers instead of blocking the lane for good.

A self-review subagent whose reviewer launched and failed is retryable (UNAVAILABLE), a pinned route that already
failed a gate with a route failure is dropped for the next attempt with a notice, and `review_only` re-runs a lane
gate that a reviewer or subagent failure left EVIDENCE_BLOCKED. Unit tier on a scratch database; no agent runs.
"""
from __future__ import annotations

import json

import pytest

from office import candidates, contract, convergence, gates, routing
from office import self_review_subagents as srs
from test_reviewer_async_launch import _subagent_scope, scratch  # noqa: F401  (scratch is a fixture)

PIN = {"as": "codex/gpt-6-luna@xhigh", "cli": None, "external": False, "by": "user"}
TRIPLE = "codex@1/gpt-6-luna@xhigh"


def _item(con, run, tasks, tmp, monkeypatch):
    plan = srs._plan(con, run, tasks, "c")
    (item,) = [i for i in plan if i["kind"] == "review"][:1]
    srs._prepare(con, run, item, tmp, "c")
    return item


@pytest.mark.parametrize("summary", ["", "model_not_found: no such model", "HTTP 402 payment required"])
def test_a_launched_subagent_that_did_not_complete_is_retryable(scratch, monkeypatch, summary):
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch, rubrics=("combined",))
    item = _item(con, run, tasks, tmp, monkeypatch)
    outcome = {"status": contract.UNAVAILABLE, "dispatch_id": "D1", "summary": summary}
    failure = srs._record(con, run, item, outcome, "c", retry=True)
    assert failure["retried"] is True
    gate = con.execute("SELECT review_status FROM gates WHERE id=?", (item["gate"]["id"],)).fetchone()
    assert gate["review_status"] == contract.UNAVAILABLE


def test_without_retry_or_after_a_completed_bad_report_it_stays_blocked(scratch, monkeypatch):
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch, rubrics=("combined",))
    item = _item(con, run, tasks, tmp, monkeypatch)
    failed = {"status": contract.UNAVAILABLE, "dispatch_id": "D1"}
    assert srs._record(con, run, item, failed, "c", retry=False)["retried"] is False
    assert con.execute("SELECT review_status FROM gates WHERE id=?", (item["gate"]["id"],)).fetchone()[0] \
        == contract.EVIDENCE_BLOCKED
    item = _item(con, run, tasks, tmp, monkeypatch)
    bad = {"status": contract.COMPLETED, "dispatch_id": "D2", "parsed": None}
    assert srs._record(con, run, item, bad, "c", retry=True)["retried"] is False


def test_an_empty_reply_reviewer_runs_the_fallback_chain_through_compose(scratch, monkeypatch):
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch, rubrics=("combined",))
    # the reviewer launched (it has a dispatch) and left no reply: model_not_found / empty
    monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: {
        "status": contract.UNAVAILABLE, "dispatch_id": "D1", "summary": "model_not_found"})
    job = {"kind": "convergence_review", "payload": {"gate_id": "Glane", "scope": "L-T1"}}
    out = srs.audit_scope(con, run, tasks, tmp, "c", job=job, retry=True)
    assert out["status"] == srs.RETRY and srs.SUBAGENT_FAILURE_MARK in out["reason"], out
    out = srs.audit_scope(con, run, tasks, tmp, "c", job=job, retry=False)
    assert out["status"] == contract.EVIDENCE_BLOCKED, "the bounded fallback is spent: the lane blocks"


@pytest.mark.parametrize("status", [contract.REVOKED, contract.INVALID_RESULT])
def test_a_launched_subagent_that_was_revoked_or_returned_an_invalid_result_stays_blocked(scratch, monkeypatch, status):
    """Review finding F1: only a no-answer route failure retries; a revoke or an invalid result is never retried."""
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch, rubrics=("combined",))
    item = _item(con, run, tasks, tmp, monkeypatch)
    failure = srs._record(con, run, item, {"status": status, "dispatch_id": "D1"}, "c", retry=True)
    assert failure["retried"] is False
    assert con.execute("SELECT review_status FROM gates WHERE id=?", (item["gate"]["id"],)).fetchone()[0] \
        == contract.EVIDENCE_BLOCKED


# ------------------------------------------------------------------ a pinned route that already failed the gate

def _attempt(con, gid, did, triple, failure, epoch=0):
    meta = {"review_attempt": {"epoch": epoch, **({"failure": failure} if failure else {})}}
    con.execute("INSERT INTO dispatches(id, run_id, gate_id, kind, triple, harness, route_json, started_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (did, "R1", gid, "reviewer", triple, "codex", json.dumps(meta),
                                           f"2026-01-01T00:00:0{len(did)}"))


@pytest.fixture
def pinned_route(monkeypatch):
    monkeypatch.setattr(candidates, "declared_decision", lambda *a, **k: {"candidate": {"id": "x"}})
    monkeypatch.setattr(routing, "candidate_id", lambda cand: TRIPLE)
    monkeypatch.setattr(gates.state, "pinned_config", lambda run: {})


@pytest.mark.parametrize("cls", ["wall", "launch", "silent"])
def test_a_pinned_route_that_failed_with_a_route_failure_is_dropped(pinned_route, cls):
    history = [{"id": "D1", "triple": TRIPLE, "harness": "codex", "epoch": 0,
                "failure": {"class": cls, "reason": f"{TRIPLE}: 402 payment required"}}]
    route, reason = gates._failed_pin({"id": "R1"}, PIN, "code_reviewer", history)
    assert route == TRIPLE and "402" in reason


@pytest.mark.parametrize("attempt", [
    {"triple": TRIPLE, "failure": None},  # the pinned route has not failed
    {"triple": TRIPLE, "failure": {"class": "pointer", "reason": "launch pointer never landed"}},  # retried once
    {"triple": TRIPLE, "failure": {"class": "invalid", "reason": "unreadable reply"}},  # a reply, not a route failure
    {"triple": "claude@1/opus@high", "failure": {"class": "wall", "reason": "quota"}},  # another route failed
])
def test_a_pin_without_a_route_failure_on_its_own_route_is_kept(pinned_route, attempt):
    history = [{"id": "D1", "harness": "codex", "epoch": 0, **attempt}]
    assert gates._failed_pin({"id": "R1"}, PIN, "code_reviewer", history) is None


def test_review_attempt_launches_the_next_fallback_with_a_notice_naming_the_dropped_pin(scratch, monkeypatch, pinned_route):
    con, run_, tmp = scratch
    seen = {}

    def fake_route(con_, config, run, role, **kw):
        seen["exclude"] = kw.get("exclude")
        return {"status": "no_candidate"}

    monkeypatch.setattr(candidates, "route_role", fake_route)
    monkeypatch.setattr(gates, "reviewer_pin", lambda *a, **k: PIN)
    monkeypatch.setattr(gates, "_cooling_pin", lambda *a: None)
    monkeypatch.setattr(gates, "_reserve_blocked_pin", lambda *a: None)
    monkeypatch.setattr(gates, "_producer_route", lambda *a: None)
    monkeypatch.setattr(gates.state, "get_run", lambda con_, rid: {"id": rid, "gates": {}, "office_version": "3.1"})
    monkeypatch.setattr(gates, "_unavailable", lambda *a, **k: {"unavailable": True})
    gate = {"id": "G1", "task_id": "T1"}
    _attempt(con, "G1", "D1", TRIPLE, {"class": "wall", "reason": "quota [quota]", "harness_wide": True})
    outcome, _ = gates._review_attempt(con, {"id": "R1"}, gate, "code_reviewer", "brief", cwd=tmp, plan_review=False,
                                      visual=False, images=None, include_dirs=None, exclude=None, kind=None,
                                      resume_from=None, prefer_not=None, epoch=1, chain_epoch=1)
    assert TRIPLE in seen["exclude"], "the dropped pin is routed away from, not launched again"
    assert "harness:codex" in seen["exclude"], "a harness-wide wall on the dropped pin rules out its whole harness"
    assert outcome == {"unavailable": True}


# ------------------------------------------------------------------ review_only accepts reviewer-blocked gates

def test_reviewer_blocked_gates_are_lane_subagent_failures_and_non_spec_visual_blocks(monkeypatch):
    rows = [
        {"id": "Gc", "kind": "convergence_review", "summary": f"T1 combined {srs.SUBAGENT_FAILURE_MARK}: no route; x"},
        {"id": "Gn", "kind": "convergence_review", "summary": "T1: no accepted revision for the combined subagent review"},
        {"id": "Gv", "kind": "visual", "summary": "capture failed", "evidence_status": "CAPTURE_BLOCKED"},
        {"id": "Gs", "kind": "visual", "summary": "bad plan", "evidence_status": convergence.SPEC_DEFECT},
    ]
    seen = {}

    def fake_open(con, run, scope_id, statuses):
        seen["statuses"] = statuses
        return rows

    monkeypatch.setattr(convergence, "_open_gates", fake_open)
    got = convergence._reviewer_blocked_gates(None, {"id": "R1"}, "L-T1")
    assert [g["id"] for g in got] == ["Gc", "Gv"]
    assert seen["statuses"] == (contract.EVIDENCE_BLOCKED,)
