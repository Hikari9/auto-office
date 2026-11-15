"""T5: a reviewer launched by a job is not waited on. A `review_supervise` job records its result once the dispatch ends,
like an executor's supervised end. The dispatch row carries a packet and a lease, the independent convergence
subagent reviews launch together, and an integration review is supervised the same way.

Runs use scripted fake harnesses and injected reviewers; no real agent runs.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from test_convergence_contract import APPROVED, _gates, _q, _start
from test_issue809_self_review_subagents import _s_run
from test_review_reroute import EXECUTOR

from office import contract, dispatch, gates, jobs, paths, review_parse, review_submit, self_review_subagents as srs, state
from office.state import Refused, Usage


@pytest.fixture(autouse=True)
def _in_process_routing(request):
    """These tests patch code that only reaches work running in this process."""
    if "env" in request.fixturenames:
        request.getfixturevalue("env").hide_agy = True


def _lane(env):
    return _gates(env, "convergence_review")[-1]


def _supervise_jobs(env):
    return [{**r, "payload": json.loads(r["payload_json"])}
            for r in _q(env, "SELECT * FROM outbox WHERE kind='review_supervise' ORDER BY created_at")]


# ------------------------------------------------------------------ launch does not wait, a supervise job settles

def test_a_job_launched_review_is_not_waited_on_and_a_supervise_job_records_its_result(env, monkeypatch):
    waits = {}
    real = dispatch.launch

    def spy(run, d, kind, ddir, **kw):
        waits[d["id"]] = (kind, kw.get("wait"))
        return real(run, d, kind, ddir, **kw)

    monkeypatch.setattr(dispatch, "launch", spy)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (review,) = _q(env, "SELECT id FROM dispatches WHERE gate_id=? AND kind='reviewer'", (gate["id"],))
    assert waits[review["id"]][1] is False, "the launching job does not hold for the reviewer"
    (job,) = _supervise_jobs(env)
    assert job["status"] == "done" and job["payload"]["dispatch_id"] == review["id"]
    assert job["payload"]["gate_id"] == gate["id"] and job["payload"]["origin"]["kind"] == "convergence_review"
    assert gate["review_status"] == "COMPLETED" and gate["verdict"] == "APPROVED"
    origin = _q(env, "SELECT result_json FROM outbox WHERE kind='convergence_review' AND status='done'")
    assert any("supervise" in json.loads(r["result_json"] or "{}") for r in origin), \
        "the reviewing job returned after the launch: it did not finalize the gate itself"


def test_a_reviewer_run_without_a_job_is_still_waited_on(env, monkeypatch):
    """A caller outside the job flow (a manual review, an injected reviewer) gets its outcome from the call."""
    waits = []
    real = dispatch.launch

    def spy(run, d, kind, ddir, **kw):
        waits.append(kw.get("wait"))
        return real(run, d, kind, ddir, **kw)

    monkeypatch.setattr(dispatch, "launch", spy)
    _s_run(env)  # the funded subagent runs through audit_scope: no job, so it is waited on
    assert True in waits and False in waits


def test_the_review_checkout_outlives_the_launching_job(env, monkeypatch):
    """The reviewer reads the checkout the brief names until its supervise job settles it."""
    seen = []
    real = gates.job_review_supervise

    def spy(con, run, job):
        checkout = paths.run_dir(run["id"]) / "checkouts" / f"converge-{job['payload']['gate_id']}"
        seen.append(checkout.is_dir())
        return real(con, run, job)

    monkeypatch.setattr(gates, "job_review_supervise", spy)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert seen == [True], "the launching job left the checkout in place for the reviewer"
    gate = _lane(env)
    assert not (paths.run_dir(gate["run_id"]) / "checkouts" / f"converge-{gate['id']}").exists(), "settling removed it"


# ------------------------------------------------------------------ packet, lease, session

def test_a_reviewer_dispatch_carries_a_packet_and_a_lease_that_settling_releases(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (d,) = _q(env, "SELECT * FROM dispatches WHERE gate_id=? AND kind='reviewer'", (gate["id"],))
    packet = json.loads(Path(d["packet_path"]).read_text())
    assert packet["packet_kind"] == "reviewer-dispatch" and packet["packet_hash"] == d["packet_hash"]
    assert packet["gate_id"] == gate["id"] and packet["dispatch_id"] == d["id"] and packet["lease_id"] == d["lease_id"]
    (lease,) = _q(env, "SELECT * FROM leases WHERE id=?", (d["lease_id"],))
    assert lease["dispatch_id"] == d["id"] and lease["holder_id"] == d["id"] and lease["role"] == "code_reviewer"
    assert lease["scope"] == "[]" and lease["task_id"] is None, "a reviewer lease holds no task scope"
    assert lease["fencing"] == packet["fencing"] and lease["released_at"], "settling the review releases its lease"


def test_a_reviewer_lease_never_holds_the_scope_of_a_task(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
        d = _q(env, "SELECT * FROM dispatches WHERE kind='reviewer'")[0]
        with con:
            con.execute("UPDATE leases SET released_at=NULL WHERE id=?", (d["lease_id"],))
        assert dispatch.scope_holder(con, run, state.get_task(con, run["id"], "T1")) is None
    finally:
        con.close()


def test_settling_records_a_session_for_a_harness_that_assigns_none_at_launch(env, monkeypatch):
    asked = []
    real = dispatch.backfill_session

    def spy(con, run, d):
        asked.append(d["id"])
        return real(con, run, d)

    monkeypatch.setattr(dispatch, "backfill_session", spy)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    (d,) = _q(env, "SELECT id FROM dispatches WHERE gate_id=? AND kind='reviewer'", (_lane(env)["id"],))
    assert asked == [d["id"]]


def _open_review(env, monkeypatch):
    """A convergence review whose reviewer is launched and never ends: the gate and dispatch stay open."""
    real = dispatch.launch

    def launch(run, d, kind, ddir, **kw):
        return real(run, d, kind, ddir, **kw) if kind == "worker" else {"launcher": "parked"}

    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(dispatch, "_wait_terminal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parked")))
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=None)
    gate = _lane(env)
    (d,) = _q(env, "SELECT * FROM dispatches WHERE gate_id=? AND kind='reviewer'", (gate["id"],))
    return gate, d


def test_a_revoked_reviewer_lease_fences_its_late_submit(env, monkeypatch):
    gate, d = _open_review(env, monkeypatch)
    assert gate["status"] == "running"
    con = env.con()
    try:
        run = state.get_run(con, d["run_id"])
        env_vars = review_submit.token_env(run, d)
        for k, v in env_vars.items():
            monkeypatch.setenv(k, v)
        with pytest.raises(Usage):  # the lease is live: the submit gets past the fence and fails on its empty text
            review_submit.submit(con, "")
        with con:
            con.execute("UPDATE leases SET revoked_at='2026-01-01T00:00:00+00:00' WHERE id=?", (d["lease_id"],))
        with pytest.raises(Refused) as fenced:
            review_submit.submit(con, f"REVIEWED_COMMIT abc\n{APPROVED}")
        assert fenced.value.category == "lease-lost"
    finally:
        con.close()


def test_revoking_a_reviewer_dispatch_revokes_its_lease(env, monkeypatch):
    gate, d = _open_review(env, monkeypatch)
    con = env.con()
    try:
        run = state.get_run(con, d["run_id"])
        dispatch._end_dispatch(con, run, state.get_dispatch(con, d["id"]), "revoked", "test", stop=False)
        row = con.execute("SELECT revoked_at, revoke_reason FROM leases WHERE id=?", (d["lease_id"],)).fetchone()
        assert row["revoked_at"] and row["revoke_reason"] == "revoked"
    finally:
        con.close()


def test_settling_a_dispatch_that_left_no_launch_record_is_refused(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    con = env.con()
    try:
        run = state.get_run(con, gate["run_id"])
        with pytest.raises(Refused) as err:
            gates.settle_review(con, run, gate, "Dnever")
        assert err.value.category == "review-attempt-missing"
    finally:
        con.close()


# ------------------------------------------------------------------ supervise jobs are owned, fenced and reclaimed

def _insert_gate(con, run, gid, subject, kind):
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (gid, run["id"], subject, kind, f"{subject}:k", "running", "2026-01-01T00:00:00+00:00"))
    return dict(con.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())


def _enqueue_supervise(con, run, gid, did):
    with con:
        return state.enqueue(con, run, "review_supervise", {"gate_id": gid, "dispatch_id": did,
                                                            "origin": {"kind": "integration_review", "payload": {}}},
                             dedup_key=f"review_supervise:{did}")


def test_an_integration_gate_is_owned_by_its_supervise_job(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _lane(env)["run_id"])
        gate = _insert_gate(con, run, "Ginteg1", "integration", "integration_review")
        assert gates.owning_jobs(con, run, gate) == []
        job_id = _enqueue_supervise(con, run, "Ginteg1", "Dx")
        assert [j["id"] for j in gates.owning_jobs(con, run, gate)] == [job_id], \
            "the gate is not orphaned while its supervise job can still record the review"
    finally:
        con.close()


def test_revoking_integration_fences_the_supervise_job_of_its_review(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _lane(env)["run_id"])
        _insert_gate(con, run, "Ginteg2", "integration", "integration_review")
        other = _insert_gate(con, run, "Glane2", "lane", "convergence_review")
        mine = _enqueue_supervise(con, run, "Ginteg2", "Dy")
        lane = _enqueue_supervise(con, run, other["id"], "Dz")
        dispatch._fence_integrate_jobs(con, run, "stop")
        status = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM outbox WHERE kind='review_supervise'")}
        assert status[mine] == "failed" and status[lane] == "queued", "only the integration review's job is fenced"
    finally:
        con.close()


def test_a_lane_revoke_stops_the_supervise_job():
    from office import convergence
    assert "review_supervise" in convergence.LANE_REVOKE_JOBS


def test_a_supervise_claim_whose_process_died_is_reclaimed_by_the_next_command(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    con = env.con()
    try:
        run = state.get_run(con, _lane(env)["run_id"])
        job_id = _enqueue_supervise(con, run, "Gx", "Dw")
        with con:
            con.execute("UPDATE outbox SET status='claimed', attempts=1, claimed_pid=?, claim_token=NULL, claimed_by=NULL "
                        "WHERE id=?", (dead.pid, job_id))
            assert jobs.reclaim(con, run["id"]) >= 1
        row = con.execute("SELECT status FROM outbox WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "queued", "an executor's dead claim is retried the same way"
    finally:
        con.close()


# ------------------------------------------------------------------ independent subagent reviews launch together

def _fake_reviewer(run, commit, *, fail=(), barrier=None):
    seen = {"now": 0, "max": 0}
    lock = threading.Lock()

    def reviewer(con, run_, gate, role, brief, **kw):
        rubric = next(k for k, text in srs.RUBRICS.items() if text in brief)
        with lock:
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
        try:
            if barrier is not None:
                barrier.wait(timeout=10)  # only returns when both reviews are in flight
            if rubric in fail:
                return {"status": contract.UNAVAILABLE, "summary": "no route"}
            d = f"Dpar-{rubric}-{uuid.uuid4().hex[:4]}"
            reply = paths.run_dir(run["id"]) / "dispatches" / d / "reply.txt"
            reply.parent.mkdir(parents=True, exist_ok=True)
            reply.write_text(f"REVIEWED_COMMIT {commit}\nVERDICT: APPROVED\n")
            return {"status": contract.COMPLETED, "dispatch_id": d, "route": "fake@1/m@low",
                    "parsed": review_parse.Parsed(verdict="APPROVED", contract=contract.CONVERGENCE)}
        finally:
            with lock:
                seen["now"] -= 1

    return reviewer, seen


def _scope_after_run(env, monkeypatch):
    _s_run(env, gear="L")
    monkeypatch.setattr(srs, "rubrics", lambda run, task: ("edge-cases", "test-strength"))
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    return con, run, [state.get_task(con, run["id"], "T1")]


def _shape(out):
    return out["status"], [(r["rubric"], r["advisory"], r["reused"]) for r in out["reports"]]


def test_independent_subagent_reviews_launch_together_and_compose_in_order(env, monkeypatch):
    con, run, tasks = _scope_after_run(env, monkeypatch)
    monkeypatch.setenv("OFFICE_LAUNCHER", "auto")
    commit = "f" * 40
    reviewer, seen = _fake_reviewer(run, commit, barrier=threading.Barrier(2))
    out = srs.audit_scope(con, run, tasks, env.repo, commit, reviewer=reviewer)
    assert seen["max"] == 2, "both reviews were in flight at once"
    assert out["status"] == contract.COMPLETED
    assert [r["rubric"] for r in out["reports"]] == list(srs.rubrics(run, tasks[0]))


def test_the_composed_result_is_what_running_them_one_at_a_time_gives(env, monkeypatch):
    con, run, tasks = _scope_after_run(env, monkeypatch)
    monkeypatch.setenv("OFFICE_LAUNCHER", "sync")
    one, other = "e" * 40, "d" * 40
    serial_reviewer, serial_seen = _fake_reviewer(run, one)
    serial = srs.audit_scope(con, run, tasks, env.repo, one, reviewer=serial_reviewer)
    monkeypatch.setenv("OFFICE_LAUNCHER", "auto")
    parallel_reviewer, parallel_seen = _fake_reviewer(run, other, barrier=threading.Barrier(2))
    parallel = srs.audit_scope(con, run, tasks, env.repo, other, reviewer=parallel_reviewer)
    assert serial_seen["max"] == 1 and parallel_seen["max"] == 2
    assert _shape(serial) == _shape(parallel)


def test_a_failed_subagent_blocks_the_scope_and_its_siblings_are_still_recorded(env, monkeypatch):
    con, run, tasks = _scope_after_run(env, monkeypatch)
    monkeypatch.setenv("OFFICE_LAUNCHER", "auto")
    first = srs.rubrics(run, tasks[0])[0]
    commit = "c" * 40
    reviewer, _ = _fake_reviewer(run, commit, fail=(first,), barrier=threading.Barrier(2))
    out = srs.audit_scope(con, run, tasks, env.repo, commit, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and out["reports"] == []
    assert f"{first} self-review subagent" in out["reason"]
    rows = con.execute("SELECT review_status FROM gates WHERE kind='self_review_subagent' AND input_key LIKE ?",
                       (f"%:{commit}:%",)).fetchall()
    assert sorted(r[0] for r in rows) == sorted([contract.COMPLETED, contract.EVIDENCE_BLOCKED]), \
        "the sibling that ran is recorded too, so no gate is left queued"


def test_one_at_a_time_a_failed_subagent_stops_the_scope_before_the_next_launches(env, monkeypatch):
    con, run, tasks = _scope_after_run(env, monkeypatch)
    monkeypatch.setenv("OFFICE_LAUNCHER", "sync")
    first = srs.rubrics(run, tasks[0])[0]
    commit = "b" * 40
    reviewer, seen = _fake_reviewer(run, commit, fail=(first,))
    calls = []
    inner = reviewer

    def counting(*a, **k):
        calls.append(1)
        return inner(*a, **k)

    out = srs.audit_scope(con, run, tasks, env.repo, commit, reviewer=counting)
    assert out["status"] == contract.EVIDENCE_BLOCKED and len(calls) == 1


def test_a_retried_settle_keeps_the_guard_verdict_of_the_snapshot_an_earlier_one_removed(env, monkeypatch):
    """A settle that died after dropping the snapshot must not read the missing snapshot as a worktree change."""
    guarded = []

    def guard_for(d, profile_kind, cwd):
        if profile_kind != "reviewer":
            return None
        guarded.append(str(cwd))
        return lambda before: {} if before is None else ("tracked diff" if not Path(cwd).is_dir() else None)

    monkeypatch.setattr(gates, "_worktree_guard", guard_for)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (d,) = _q(env, "SELECT id, run_id FROM dispatches WHERE gate_id=? AND kind='reviewer'", (gate["id"],))
    ddir = paths.run_dir(d["run_id"]) / "dispatches" / d["id"]
    rec = json.loads((ddir / gates.ATTEMPT_FILE).read_text())
    assert rec["snapshot"] and rec["changed"] is None, "the verdict is kept on the launch record"
    assert not Path(rec["snapshot"]).exists(), "the snapshot is gone"
    con = env.con()
    try:
        run = state.get_run(con, d["run_id"])
        outcome, step = gates.settle_review(con, run, dict(gate), d["id"])
    finally:
        con.close()
    assert step is None and outcome["status"] == contract.COMPLETED, "a retry settles on the recorded verdict"
