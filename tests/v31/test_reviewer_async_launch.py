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
from test_issue809_self_review_subagents import _s_run, _subagent_gates, fund_every_gear
from test_review_reroute import EXECUTOR

from office import contract, dispatch, gates, jobs, paths, review_parse, review_submit, self_review_subagents as srs, state
from office.state import Refused, Usage


@pytest.fixture(autouse=True)
def _in_process_routing(request):
    """These tests patch code that only reaches work running in this process."""
    if "env" in request.fixturenames:
        request.getfixturevalue("env").hide_agy = True
        fund_every_gear()  # #1233: these runs exercise the per-task audit machinery on S, M and L


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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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


def test_a_lane_review_calls_run_reviewer_once_and_the_settling_job_does_not_rebuild_it(env, monkeypatch):
    calls = []
    real = gates.run_reviewer

    def spy(con, run, gate, role, brief, **kw):
        calls.append(role)
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "run_reviewer", spy)
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _lane(env)["verdict"] == "APPROVED" and calls == ["code_reviewer"]


def test_a_reviewer_run_without_a_job_is_still_waited_on(scratch, monkeypatch):
    """A caller outside the job flow (a manual review, an injected reviewer) gets its outcome from the call."""
    con, run, tmp = scratch
    seen = _chain_spy(monkeypatch)
    gates.run_reviewer(con, run, {"id": "G1"}, "code_reviewer", "b", cwd=tmp)
    gates.run_reviewer(con, run, {"id": "G2"}, "code_reviewer", "b", cwd=tmp, job={"kind": "convergence_review", "payload": {}})
    assert [kw["supervise"] for kw in seen] == [False, True]
def test_the_funded_subagent_launches_through_the_convergence_job_and_is_not_waited_on(env, monkeypatch):
    """F11: a self-review subagent is a job-supervised reviewer like the lane's: launched, not waited on, settled by its own
    supervise job (which enters the convergence job for it), then composed by one more convergence job."""
    waits = {}
    real = dispatch.launch
    def spy(run, d, kind, ddir, **kw):
        waits[d["id"]] = kw.get("wait")
        return real(run, d, kind, ddir, **kw)
    monkeypatch.setattr(dispatch, "launch", spy)
    _s_run(env)
    (sub,) = _subagent_gates(env)
    assert sub["review_status"] == "COMPLETED" and waits[sub["reviewer_dispatch_id"]] is False
    (sup,) = [j for j in _supervise_jobs(env) if j["payload"]["gate_id"] == sub["id"]]
    assert sup["payload"]["origin"]["kind"] == "convergence_review"
    assert sup["payload"]["origin"]["payload"]["subagent"]["key"] == sub["input_key"]
    jobs_ = [json.loads(r["result_json"] or "{}") for r in _q(env, "SELECT result_json FROM outbox WHERE kind='convergence_review'")]
    assert {"subagents": "launched"} in jobs_, "the launching job did not wait for the subagent"
    assert _lane(env)["verdict"] == "APPROVED"
def test_independent_subagents_launch_in_one_job_and_one_compose_job_follows_the_last_settle(env, monkeypatch):
    waits = {}
    real = dispatch.launch
    def spy(run, d, kind, ddir, **kw):
        waits[d["id"]] = kw.get("wait")
        return real(run, d, kind, ddir, **kw)
    monkeypatch.setattr(dispatch, "launch", spy)
    monkeypatch.setattr(srs, "rubrics", lambda run, task: ("edge-cases", "test-strength"))
    _s_run(env, gear="L")
    subs = _subagent_gates(env)
    assert len(subs) == 2 and {g["review_status"] for g in subs} == {"COMPLETED"}
    assert [waits[g["reviewer_dispatch_id"]] for g in subs] == [False, False], "neither holds the launching job"
    assert len([j for j in _supervise_jobs(env) if j["payload"]["gate_id"] in {g["id"] for g in subs}]) == 2
    launching = [r for r in _q(env, "SELECT payload_json, result_json FROM outbox WHERE kind='convergence_review'")
                 if json.loads(r["result_json"] or "{}") == {"subagents": "launched"}]
    assert len(launching) == 1, "one convergence job launched both"
    composes = _q(env, "SELECT * FROM outbox WHERE dedup_key LIKE 'self_review_compose:%'")
    assert len(composes) == 1 and json.loads(composes[0]["payload_json"])[srs.COLLECT_KEY]["gates"] == [g["id"] for g in subs]
    con = env.con()
    try:
        run = state.get_run(con, subs[0]["run_id"])
        sa = next(j for j in _supervise_jobs(env) if j["payload"]["gate_id"] == subs[0]["id"])["payload"]["origin"]["payload"]["subagent"]
        srs._queue_compose(con, run, sa)
        assert _q(env, "SELECT COUNT(*) n FROM outbox WHERE dedup_key LIKE 'self_review_compose:%'")[0]["n"] == 1, "deduped"
    finally:
        con.close()
    brief = (paths.run_dir(subs[0]["run_id"]) / "dispatches" / _lane(env)["reviewer_dispatch_id"] / "brief.md").read_text()
    assert "edge-cases" in brief and "test-strength" in brief and brief.index("edge-cases") < brief.index("test-strength")
def test_a_subagent_that_never_launched_is_retried_through_the_job_flow_and_launches(env, monkeypatch):
    """F9 end to end: the first call finds no route, the bounded retry reopens the gate on a fresh chain, launches the
    subagent under supervise and its settle composes (the compose job carries nothing of the first chain)."""
    real = gates.run_reviewer
    calls = []
    def flaky(con, run, gate, role, brief, **kw):
        if "runtime self-review subagent" in brief:
            calls.append((gate["id"], kw.get("job") is not None))
            if len(calls) == 1:
                return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "summary": "no route"}
        return real(con, run, gate, role, brief, **kw)
    monkeypatch.setattr(gates, "run_reviewer", flaky)
    _s_run(env)
    (sub,) = _subagent_gates(env)
    assert [g for g, _ in calls] == [sub["id"]] * 2 and all(job for _, job in calls), "the same gate, both through the job"
    assert sub["review_status"] == "COMPLETED"
    assert _lane(env)["verdict"] == "APPROVED"
def test_a_subagent_whose_chain_is_exhausted_after_it_launched_is_launched_again_on_a_fresh_chain(env, monkeypatch):
    """The compose job's payload names the gates of the chain that failed: the bounded retry must not compose from them
    again but launch the subagent anew."""
    real = gates.settle_reviewer
    settled = []
    def settle(con, run, gate, job):
        if gate["kind"] != "self_review_subagent":
            return real(con, run, gate, job)
        settled.append(gate["id"])
        if len(settled) == 1:  # every route of the first chain failed: no dispatch to stand behind
            return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "summary": "every eligible route failed"}
        return real(con, run, gate, job)
    monkeypatch.setattr(gates, "settle_reviewer", settle)
    _s_run(env)
    (sub,) = _subagent_gates(env)
    assert len(settled) == 2 and set(settled) == {sub["id"]}, "the same gate was launched and settled twice"
    assert sub["review_status"] == "COMPLETED" and _lane(env)["verdict"] == "APPROVED"
    assert len(_q(env, "SELECT id FROM dispatches WHERE gate_id=? AND kind='reviewer'", (sub["id"],))) == 2
def test_revoking_a_lane_review_ends_its_launched_subagents_and_frees_what_they_held(env, monkeypatch):
    from office import convergence
    real = dispatch.launch
    monkeypatch.setattr(dispatch, "launch", lambda run, d, kind, ddir, **kw: real(run, d, kind, ddir, **kw) if kind == "worker"
                        else {"launcher": "parked"})
    monkeypatch.setattr(dispatch, "_wait_terminal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parked")))
    _start(env, gear="S", executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=None)
    (sub,) = _subagent_gates(env)
    lane = _lane(env)
    (d,) = _q(env, "SELECT * FROM dispatches WHERE gate_id=? AND kind='reviewer'", (sub["id"],))
    checkout = paths.run_dir(lane["run_id"]) / "checkouts" / f"converge-{lane['id']}"
    assert sub["status"] == "running" and checkout.is_dir()
    (job,) = [j for j in _supervise_jobs(env) if j["payload"]["gate_id"] == sub["id"]]
    con = env.con()
    try:
        convergence.revoke_lane_gate(con, state.get_run(con, d["run_id"]), "L-T1:convergence", "test")
    finally:
        con.close()
    assert _q(env, "SELECT status FROM outbox WHERE id=?", (job["id"],))[0]["status"] == "failed"
    assert _q(env, "SELECT status, review_status FROM gates WHERE id=?", (sub["id"],))[0]["status"] == "done"
    assert _q(env, "SELECT ended_at FROM dispatches WHERE id=?", (d["id"],))[0]["ended_at"], "the subagent's reviewer ended"
    assert _q(env, "SELECT revoked_at FROM leases WHERE id=?", (d["lease_id"],))[0]["revoked_at"]
    assert not checkout.exists()
def test_a_subagent_retry_starts_on_a_fresh_chain_of_routes(scratch, monkeypatch):
    """F9 through the job flow: a gate whose earlier attempt failed is retried on a chain the failed routes are eligible in."""
    con, run, tmp = scratch
    monkeypatch.setattr(gates, "review_attempts", lambda con_, gid: [])
    assert srs._fresh_chain(con, "G1") == {}
    monkeypatch.setattr(gates, "review_attempts", lambda con_, gid: [{"epoch": 0}, {"epoch": 1}])
    assert srs._fresh_chain(con, "G1") == {"review_fallback": {"epoch": 2, "chain_epoch": 2}}
def test_a_subagent_job_whose_lane_review_closed_blocks_its_gate_and_frees_the_checkout(scratch):
    con, run, tmp = scratch
    _gate_row(con, "Glane", status="stale")
    _gate_row(con, "Gsub", subject="T1", kind="self_review_subagent")
    checkout = paths.run_dir("R1") / "checkouts" / "converge-Glane"
    checkout.mkdir(parents=True)
    job = {"kind": "convergence_review", "payload": {"gate_id": "Gsub", "subagent": {
        "parent": {"payload": {"gate_id": "Glane"}}, "key": "R1:c:combined:v", "commit": "c", "retry": False}}}
    assert srs.job_subagent_review(con, run, job) == {"skipped": "stale"}
    assert not checkout.exists()
    row = con.execute("SELECT status, review_status FROM gates WHERE id='Gsub'").fetchone()
    assert (row["status"], row["review_status"]) == ("done", "EVIDENCE_BLOCKED")
def test_the_review_checkout_outlives_the_launching_job(env, monkeypatch):
    """The reviewer reads the checkout the brief names until its supervise job settles it."""
    seen = []
    real = gates.job_review_supervise

    def spy(con, run, job):
        checkout = paths.run_dir(run["id"]) / "checkouts" / f"converge-{job['payload']['gate_id']}"
        seen.append(checkout.is_dir())
        return real(con, run, job)

    monkeypatch.setattr(gates, "job_review_supervise", spy)
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert seen == [True], "the launching job left the checkout in place for the reviewer"
    gate = _lane(env)
    assert not (paths.run_dir(gate["run_id"]) / "checkouts" / f"converge-{gate['id']}").exists(), "settling removed it"


# ------------------------------------------------------------------ packet, lease, session

def test_a_reviewer_dispatch_carries_a_packet_and_a_lease_that_settling_releases(env):
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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


def _checkout_of(gate):
    return paths.run_dir(gate["run_id"]) / "checkouts" / f"converge-{gate['id']}"


def test_a_lane_revoke_fails_the_supervise_job_and_frees_what_the_reviewer_held(env, monkeypatch):
    from office import convergence
    gate, d = _open_review(env, monkeypatch)
    assert _checkout_of(gate).is_dir(), "the launching job left the checkout for the reviewer"
    (job,) = _supervise_jobs(env)
    con = env.con()
    try:
        run = state.get_run(con, d["run_id"])
        convergence.revoke_lane_gate(con, run, "L-T1:convergence", "test")
    finally:
        con.close()
    assert _q(env, "SELECT status FROM outbox WHERE id=?", (job["id"],))[0]["status"] == "failed"
    assert not _q(env, "SELECT 1 FROM outbox WHERE kind='review_fallback'"), "a revoke queues no fallback"
    assert not _checkout_of(gate).exists(), "no settle will run, so the revoke removed the checkout"
    assert _q(env, "SELECT revoked_at FROM leases WHERE id=?", (d["lease_id"],))[0]["revoked_at"]


def test_a_supervise_job_whose_gate_went_stale_frees_the_checkout_snapshot_and_lease(env, monkeypatch):
    gate, d = _open_review(env, monkeypatch)
    assert _checkout_of(gate).is_dir()
    (job,) = _supervise_jobs(env)
    con = env.con()
    try:
        con.execute("UPDATE gates SET status='stale' WHERE id=?", (gate["id"],))
        con.commit()
        run = state.get_run(con, d["run_id"])
        assert gates.job_review_supervise(con, run, {**job, "payload": job["payload"]}) == {"skipped": "stale"}
    finally:
        con.close()
    assert not _checkout_of(gate).exists()
    assert _q(env, "SELECT released_at FROM leases WHERE id=?", (d["lease_id"],))[0]["released_at"]


def test_a_reclaimed_supervise_job_runs_again_and_settles_nothing_twice(env):
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (job,) = _supervise_jobs(env)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    verdicts = _q(env, "SELECT COUNT(*) n FROM findings")[0]["n"], gate["env_failures"]
    con = env.con()
    try:
        run = state.get_run(con, gate["run_id"])
        with con:
            con.execute("UPDATE outbox SET status='claimed', attempts=1, claimed_pid=?, claim_token=NULL, claimed_by=NULL "
                        "WHERE id=?", (dead.pid, job["id"]))
            assert jobs.reclaim(con, run["id"]) == 1
        assert con.execute("SELECT status FROM outbox WHERE id=?", (job["id"],)).fetchone()["status"] == "queued"
        jobs.execute(con, job["id"])
    finally:
        con.close()
    again = _lane(env)
    assert _q(env, "SELECT status, result_json FROM outbox WHERE id=?", (job["id"],))[0]["status"] == "done"
    assert (again["review_status"], again["verdict"], again["env_failures"]) == ("COMPLETED", "APPROVED", gate["env_failures"])
    assert (_q(env, "SELECT COUNT(*) n FROM findings")[0]["n"], again["env_failures"]) == verdicts


def test_a_retried_failed_settle_counts_the_attempts_environment_failure_once(env):
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (d,) = _q(env, "SELECT id FROM dispatches WHERE gate_id=? AND kind='reviewer'", (gate["id"],))
    con = env.con()
    try:
        with con:
            gates._record_failure(con, gate, d["id"], "launch", "first", count=True)
            gates._record_failure(con, gate, d["id"], "launch", "same attempt, settled again", count=True)
        assert con.execute("SELECT env_failures FROM gates WHERE id=?", (gate["id"],)).fetchone()[0] == gate["env_failures"] + 1
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
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
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


# ------------------------------------------------------------------ every origin kind launches once and settles without a rebuild

@pytest.mark.review_contract("v3.1")
def test_a_task_gate_review_launches_once_and_its_supervise_job_settles_it_without_a_second_launch(env, monkeypatch):
    from conftest import GOOD_ADD, approved_run
    calls = []
    real = gates.run_reviewer

    def spy(con, run, gate, role, brief, **kw):
        calls.append(role)
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "run_reviewer", spy)
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    review = _q(env, "SELECT * FROM gates WHERE kind='code_review'")[-1]
    assert review["verdict"] == "PASS", review
    assert calls == ["code_reviewer"], "the settling job did not run the review again"
    (job,) = [j for j in _supervise_jobs(env) if j["payload"]["origin"]["kind"] == "review"]
    assert job["status"] == "done" and job["payload"]["gate_id"] == review["id"]
    assert not (paths.run_dir(review["run_id"]) / "checkouts" / f"review-{review['id']}").exists()


def test_an_integration_review_launches_once_then_settles_and_accepts_without_a_second_launch(env, monkeypatch):
    from office import integration
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _lane(env)["run_id"])
        gate = _insert_gate(con, run, "Ginteg9", "integration", "integration_review")
        con.commit()
        head = env.git("rev-parse", "HEAD").strip()
        ctx = {"key": "k", "commit": head, "base": head, "branch": "b", "integ_rev": {"id": "I1", "commit_sha": head},
               "run_checks": [], "executed": [], "checks_gate_id": None, "results": {}}
        checkout = paths.run_dir(run["id"]) / "checkouts" / "integration-Ginteg9"
        removed, launched, accepted = [], [], []

        def make(run_, commit, name, *, purpose):
            checkout.mkdir(parents=True, exist_ok=True)
            return checkout

        monkeypatch.setattr(gates, "detached_checkout", make)
        monkeypatch.setattr(gates, "remove_checkout", lambda run_, path: (removed.append(Path(path)), None)[1])
        monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: (launched.append(1),
                            {"supervise_queued": "Jsup", "fallback_queued": "Jsup", "dispatch_id": "Dint"})[1])
        monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"status": contract.COMPLETED, "verdict": "PASS",
                                                                       "summary": "ok", "parsed": None})
        monkeypatch.setattr(integration, "_accept_composed", lambda con_, run_, c, review=None: (
        accepted.append(c), integration._finish_review_gate(con_, *review), {"accepted": True})[2])
        monkeypatch.setattr(integration.paths, "git", lambda *a, **k: "")
        first = integration._review_composed(con, run, {"id": "J1", "kind": "integration_review",
                                                        "payload": {"gate_id": "Ginteg9"}}, "Ginteg9", ctx)
        assert first == {"supervise": "Jsup", "dispatch_id": "Dint"} and removed == [] and accepted == []
        assert con.execute("SELECT status FROM gates WHERE id='Ginteg9'").fetchone()[0] == "running", "the gate stays open"
        second = integration._review_composed(con, run, {"id": "J2", "kind": "integration_review", "payload": {
            "gate_id": "Ginteg9", gates.SETTLE_KEY: {"dispatch_id": "Dint"}}}, "Ginteg9", ctx)
        assert len(launched) == 1, "settling does not launch the review again"
        assert removed == [checkout] and len(accepted) == 1 and second == {"accepted": True}
        assert con.execute("SELECT verdict FROM gates WHERE id='Ginteg9'").fetchone()[0] == "PASS"
    finally:
        con.close()


def test_a_lane_visual_review_settles_from_the_launch_without_capturing_or_launching_again(env, monkeypatch):
    from office import convergence, visual
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "--as", "claude/claude-opus-5-5@medium", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _lane(env)["run_id"])
        commit = convergence.scope_state(run, "L-T1")["commit"]
        con.execute("INSERT INTO gates(id, run_id, subject, scope, kind, input_key, status, created_at) "
                    "VALUES('Gvis1', ?, 'lane', 'L-T1', 'visual', ?, 'running', '2026-01-01T00:00:00+00:00')",
                    (run["id"], f"L-T1:{commit}"))
        con.commit()

        def forbidden(*a, **k):
            raise AssertionError("the settling job captured or launched again")

        monkeypatch.setattr(visual, "capture_all", forbidden)
        monkeypatch.setattr(gates, "run_reviewer", forbidden)
        monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"fallback_queued": "Jnext"})
        job = {"id": "Jv", "kind": "lane_visual", "payload": {"gate_id": "Gvis1", "scope": "L-T1",
                                                              gates.SETTLE_KEY: {"dispatch_id": "Dv"}}}
        assert convergence.job_lane_visual(con, run, job) == {"fallback": "Jnext"}
    finally:
        con.close()


# ------------------------------------------------------------------ unit tier: the same behaviours on a scratch database

@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A scratch runs.db (foreign keys off, so a test inserts only the rows it reads) and a run directory under tmp_path."""
    from office import db
    con = db.connect(tmp_path / "runs.db")
    con.execute("PRAGMA foreign_keys=OFF")
    monkeypatch.setattr(paths, "run_dir", lambda run_id: tmp_path / "runs" / run_id)
    run = {"id": "R1", "repo_root": str(tmp_path), "office_version": "3.1"}
    yield con, run, tmp_path
    con.close()


def _gate_row(con, gid, *, subject="lane", kind="convergence_review", status="running"):
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (gid, "R1", subject, kind, f"{subject}:k", status, "2026-01-01T00:00:00+00:00"))
    return dict(con.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())


def _supervise_row(con, gid, did, kind="review_supervise"):
    con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", ("J" + did, "R1", kind, f"k{did}", json.dumps({"gate_id": gid, "dispatch_id": did}),
                                            "3.1", "queued", "2026-01-01T00:00:00+00:00"))
    return "J" + did


def test_a_stale_gate_frees_what_a_launched_reviewer_held(scratch, monkeypatch):
    con, run, tmp = scratch
    _gate_row(con, "G1", status="stale")
    checkout = paths.run_dir("R1") / "checkouts" / "converge-G1"
    snapshot = tmp / "snap"
    for d in (checkout, snapshot):
        d.mkdir(parents=True)
    ddir = paths.run_dir("R1") / "dispatches" / "D1"
    ddir.mkdir(parents=True)
    (ddir / gates.ATTEMPT_FILE).write_text(json.dumps({"snapshot": str(snapshot)}))
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, dispatch_id) "
                "VALUES('L1','R1','code_reviewer','[]','D1','2026-01-01','2026-01-02','D1')")
    import shutil
    monkeypatch.setattr(gates, "remove_checkout", lambda run_, path: shutil.rmtree(path, ignore_errors=True))
    monkeypatch.setattr(gates, "_drop_review_snapshot", lambda cwd, path: shutil.rmtree(path, ignore_errors=True))
    job = {"payload": {"gate_id": "G1", "dispatch_id": "D1", "origin": {"kind": "convergence_review", "payload": {}}}}
    assert gates.job_review_supervise(con, run, job) == {"skipped": "stale"}
    assert not checkout.exists() and not snapshot.exists()
    assert con.execute("SELECT released_at FROM leases WHERE id='L1'").fetchone()[0], "its lease is released"


def test_a_retried_settle_records_the_attempts_environment_failure_once(scratch):
    con, run, tmp = scratch
    gate = _gate_row(con, "G2")
    con.execute("INSERT INTO dispatches(id, run_id, route_json) VALUES('D2','R1','{}')")
    gates._record_failure(con, gate, "D2", "launch", "first", count=True)
    gates._record_failure(con, gate, "D2", "launch", "the same attempt, settled again", count=True)
    assert con.execute("SELECT env_failures FROM gates WHERE id='G2'").fetchone()[0] == 1


def test_a_lane_revoke_reaches_the_supervise_job_that_owns_the_review(scratch):
    from office import convergence
    con, run, tmp = scratch
    gate = _gate_row(con, "G3")
    mine = _supervise_row(con, "G3", "D3")
    _supervise_row(con, "Gother", "D4")
    owned = [j["id"] for j in gates.owning_jobs(con, run, gate) if j["kind"] in convergence.LANE_REVOKE_JOBS]
    assert owned == [mine], "only this gate's supervise job is fenced by a lane revoke"


def test_an_integration_review_launches_once_and_its_settling_run_accepts_without_launching_again(scratch, monkeypatch):
    from office import integration
    con, run, tmp = scratch
    _gate_row(con, "Gint", subject="integration", kind="integration_review")
    checkout = paths.run_dir("R1") / "checkouts" / "integration-Gint"
    removed, launched, accepted = [], [], []
    monkeypatch.setattr(gates, "detached_checkout", lambda *a, **k: (checkout.mkdir(parents=True, exist_ok=True), checkout)[1])
    monkeypatch.setattr(gates, "remove_checkout", lambda run_, path: removed.append(Path(path)))
    monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: (launched.append(1),
                        {"supervise_queued": "Jsup", "fallback_queued": "Jsup", "dispatch_id": "Dint"})[1])
    monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"status": contract.COMPLETED, "verdict": "PASS",
                                                                   "summary": "ok", "parsed": None})
    monkeypatch.setattr(integration, "_accept_composed", lambda con_, run_, c, review=None: (
        accepted.append(c), integration._finish_review_gate(con_, *review), {"accepted": True})[2])
    monkeypatch.setattr(integration, "_set_integration", lambda *a, **k: None)
    monkeypatch.setattr(integration.briefs, "code_review_brief", lambda *a, **k: "brief")
    monkeypatch.setattr(integration.paths, "git", lambda *a, **k: "")
    ctx = {"key": "k", "commit": "c" * 40, "base": "b" * 40, "branch": "b", "integ_rev": {"id": "I1"}, "results": {}}
    first = integration._review_composed(con, run, {"id": "J1", "kind": "integration_review",
                                                    "payload": {"gate_id": "Gint"}}, "Gint", ctx)
    assert first == {"supervise": "Jsup", "dispatch_id": "Dint"} and removed == [] and accepted == []
    assert con.execute("SELECT status FROM gates WHERE id='Gint'").fetchone()[0] == "running", "the gate stays open"
    second = integration._review_composed(con, run, {"id": "J2", "kind": "integration_review", "payload": {
        "gate_id": "Gint", gates.SETTLE_KEY: {"dispatch_id": "Dint"}}}, "Gint", ctx)
    assert len(launched) == 1, "settling does not launch the review again"
    assert removed == [checkout] and len(accepted) == 1 and second == {"accepted": True}
    assert con.execute("SELECT verdict FROM gates WHERE id='Gint'").fetchone()[0] == "PASS"


# ------------------------------------------------------------------ the v3.1 visual review runs as a job (F-2)

def _visual_scratch(scratch, monkeypatch, *, payload=None):
    """A visual gate whose capture receipt binds to it, with the review's collaborators recorded."""
    from office import conformance, visual
    con, run, tmp = scratch
    run = {**run, "plan_version": 1, "gates": {}}
    gate = _gate_row(con, "Gv", subject="task", kind="visual")
    con.execute("UPDATE gates SET task_id='T1', revision_id='Rev1' WHERE id='Gv'")
    receipt = tmp / "evidence" / "capture.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"office_version": run["office_version"], "revision": "Rev1", "reference": None,
                                   "frames": []}))
    seen = {"run_reviewer": [], "settle": [], "ingest": [], "route": []}
    monkeypatch.setattr(state, "get_task", lambda con_, run_id, tid: {"id": "T1", "title": "t", "accept": ["a"], "visual": {}})
    monkeypatch.setattr(state, "get_run", lambda con_, run_id: run)
    monkeypatch.setattr(state, "pinned_config", lambda run_: {})
    monkeypatch.setattr(conformance, "ensure_vision_route", lambda *a, **k: seen["route"].append(1))
    monkeypatch.setattr(gates, "ingest_task_gate", lambda con_, run_, gid, outcome: seen["ingest"].append(outcome))
    job = {"id": "Jv", "kind": "visual_review", "payload": {"gate_id": "Gv", "capture": str(receipt), **(payload or {})}}
    return visual, con, run, gate, job, seen


def test_the_visual_review_launches_through_its_job_and_leaves_the_gate_open(scratch, monkeypatch):
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch)
    monkeypatch.setattr(gates, "run_reviewer", lambda con_, run_, gate_, role, brief, **kw: (
        seen["run_reviewer"].append((role, kw)),
        {"supervise_queued": "Jsup", "fallback_queued": "Jsup", "dispatch_id": "Dvis"})[1])
    result = visual.job_visual_review(con, run, job)
    assert result == {"supervise": "Jsup", "dispatch_id": "Dvis"}
    ((role, kw),) = seen["run_reviewer"]
    assert role == "visual_reviewer" and kw["job"] is job and kw["visual"] is True and kw["kind"] == "vision"
    assert seen["ingest"] == [], "a launched visual reviewer's verdict is recorded by its supervise job, not here"


def test_a_visual_review_no_route_answered_queues_a_review_fallback_for_the_visual_job(scratch, monkeypatch):
    """No verdict from the attempt: the real run_reviewer queues the one fallback job, and the visual job ingests nothing."""
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch)
    attempts = []

    def failing_attempt(*a, **k):
        attempts.append(1)
        assert len(attempts) == 1, "the next route ran in this call: the visual job was not handed to run_reviewer"
        return None, {"reason": "no reply", "dispatch_id": "Dv1"}

    monkeypatch.setattr(gates, "_review_attempt", failing_attempt)
    result = visual.job_visual_review(con, run, job)
    fallback = con.execute("SELECT id FROM outbox WHERE kind='review_fallback'").fetchone()[0]
    assert result == {"fallback": fallback}
    assert seen["ingest"] == [], "the gate stays open for the queued re-run"
    queued = json.loads(con.execute("SELECT payload_json FROM outbox WHERE id=?", (fallback,)).fetchone()[0])
    assert queued["origin"]["kind"] == "visual_review" and queued["origin"]["payload"]["gate_id"] == "Gv"
    assert queued["fallback"]["epoch"] == 0


def test_a_review_fallback_reenters_the_visual_job_with_its_chain(scratch, monkeypatch):
    from office import jobs, visual
    visual_, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch)
    seen["entered"] = []
    monkeypatch.setattr(visual, "job_visual_review", lambda con_, run_, inner: (seen["entered"].append(inner), {"ok": 1})[1])
    monkeypatch.setattr(jobs, "handler", lambda kind: visual.job_visual_review)
    fb = {"epoch": 0, "chain_epoch": 0, "redispatches": 0, "commit_retries": 0, "resume_from": None, "correction": None}
    fallback_job = {"id": "Jfb", "kind": "review_fallback", "payload": {
        "gate_id": "Gv", "origin": {"kind": "visual_review", "payload": job["payload"]}, "fallback": fb}}
    assert gates.job_review_fallback(con, run, fallback_job) == {"ok": 1}
    (inner,) = seen["entered"]
    assert inner["kind"] == "visual_review" and inner["payload"]["review_fallback"] == fb


def test_a_settling_visual_job_records_the_verdict_without_launching_or_rebuilding(scratch, monkeypatch):
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch,
                                                        payload={gates.SETTLE_KEY: {"dispatch_id": "Dvis"}})
    monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: seen["run_reviewer"].append(1))
    outcome = {"verdict": "PASS", "summary": "ok", "route": "r", "parsed": None}
    monkeypatch.setattr(gates, "settle_reviewer", lambda con_, run_, gate_, job_: (seen["settle"].append(job_), outcome)[1])
    result = visual.job_visual_review(con, run, job)
    assert result == {"verdict": "PASS"}
    assert seen["run_reviewer"] == [] and seen["route"] == [], "settling launches nothing and rebuilds no brief"
    assert seen["settle"] == [job] and [o["verdict"] for o in seen["ingest"]] == ["PASS"]
    assert seen["ingest"][0]["evidence_status"] == "COMPARABLE"


def test_a_settling_visual_job_whose_attempt_failed_queues_the_next_route_and_ingests_nothing(scratch, monkeypatch):
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch,
                                                        payload={gates.SETTLE_KEY: {"dispatch_id": "Dvis"}})
    monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"fallback_queued": "Jnext"})
    assert visual.job_visual_review(con, run, job) == {"fallback": "Jnext"}
    assert seen["ingest"] == []


def test_a_settling_visual_job_whose_capture_is_gone_frees_the_launched_reviewer(scratch, monkeypatch):
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch,
                                                        payload={gates.SETTLE_KEY: {"dispatch_id": "Dvis"}})
    Path(job["payload"]["capture"]).unlink()
    released = []
    monkeypatch.setattr(gates, "release_launched_review", lambda con_, run_, gid, did: released.append((gid, did)))
    monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: pytest.fail("nothing to settle against"))
    result = visual.job_visual_review(con, run, job)
    assert result["verdict"] == "UNAVAILABLE" and released == [("Gv", "Dvis")]
    assert [o["evidence_status"] for o in seen["ingest"]] == ["CAPTURE_BLOCKED"]


def test_a_settled_visual_verdict_of_invalid_comparison_still_recaptures(scratch, monkeypatch):
    visual, con, run, gate, job, seen = _visual_scratch(scratch, monkeypatch,
                                                        payload={gates.SETTLE_KEY: {"dispatch_id": "Dvis"}})
    parsed = type("P", (), {"evidence_status": "INVALID_COMPARISON", "verdict": None})()
    monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"verdict": "PASS", "parsed": parsed, "route": "r"})
    monkeypatch.setattr(state, "enqueue", lambda con_, run_, kind, payload, **k: seen["route"].append(kind))
    assert visual.job_visual_review(con, run, job) == {"recapture": True}
    assert seen["ingest"] == [] and seen["route"] == ["visual_capture"]


# ------------------------------------------------------------------ a redispatch starts a new chain (F-1)

def test_a_redispatch_launches_a_second_reviewer_on_routes_the_exhausted_chain_failed_on(env):
    """Every route of the first chain answers nothing. The automatic redispatch then runs on a new chain whose attempts
    actually launch (the first chain's failures no longer exclude every route), while a harness a failure in the
    current chain walled stays excluded for the rest of that chain."""
    from test_review_fallback_job import EMPTY, _attempts, _redispatches
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[EMPTY])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    attempts = _attempts(env, gate["id"])
    first_chain = [a for a in attempts if a["epoch"] == 0]
    second_chain = [a for a in attempts if a["epoch"] == 1]
    assert len(_redispatches(env)) == 1 and first_chain, "the first chain ran and was exhausted"
    assert second_chain, "the redispatch launched a reviewer instead of finding every route excluded"
    assert {a["triple"] for a in second_chain} & {a["triple"] for a in first_chain}, "a route of the first chain ran again"
    walled = set()
    for a in second_chain:
        assert a["harness"] not in walled, "a harness walled in the current chain stays excluded"
        if a["failure"] and a["failure"].get("harness_wide"):
            walled.add(a["harness"])
    assert gate["review_status"] == "UNAVAILABLE"


def test_route_exclusions_read_only_the_current_chain_of_a_redispatch():
    walled = lambda i, epoch, harness_wide: {"id": f"D{i}", "triple": f"h{i}/m@low", "harness": f"h{i}", "epoch": epoch,
                                              "failure": {"class": "silent", "reason": "x", **({"harness_wide": True} if harness_wide else {})}}
    history = [walled(0, 0, True), walled(1, 1, True), walled(2, 1, False)]
    chain = lambda start, epoch: gates.route_exclusions([a for a in history if start <= a["epoch"] <= epoch])
    assert chain(0, 1) == {"h0/m@low", "harness:h0", "h1/m@low", "harness:h1", "h2/m@low"}, "one chain: every failure"
    assert chain(1, 1) == {"h1/m@low", "harness:h1", "h2/m@low"}, "a redispatch's chain: the old chain is eligible again"
    assert gates._fallback_state({"payload": {}})["chain_epoch"] == 0


def test_only_a_redispatch_starts_a_new_chain():
    run = {"gates": {"review_redispatch_max": 3}}
    job = {"kind": "convergence_review", "payload": {"review_fallback": {"epoch": 2, "chain_epoch": 1, "redispatches": 1}}}
    gate = {"id": "G1", "task_id": "T1"}
    queued = []

    class Con:
        def execute(self, *a, **k):
            raise AssertionError("not reached")
    import contextlib
    from unittest import mock
    with mock.patch.object(gates.db, "transaction", lambda con: contextlib.nullcontext()), \
         mock.patch.object(gates.state, "enqueue", lambda con, run_, kind, payload, **k: (queued.append(payload["fallback"]), "J")[1]), \
         mock.patch.object(gates.state, "emit", lambda *a, **k: None):
        gates.queue_review_fallback(Con(), {**run, "id": "R1"}, gate, job, "no verdict", redispatch=True)
        gates.queue_review_fallback(Con(), {**run, "id": "R1"}, gate, job, "bad commit", commit_retry={"resume_from": "D1", "correction": "c"})
        gates.queue_review_fallback(Con(), {**run, "id": "R1"}, gate, job, "next route")
    redispatch, commit_retry, nxt = queued
    assert (redispatch["epoch"], redispatch["chain_epoch"]) == (3, 3), "a redispatch starts its own chain"
    assert (commit_retry["epoch"], commit_retry["chain_epoch"]) == (3, 1), "a commit retry stays in the chain"
    assert (nxt["epoch"], nxt["chain_epoch"]) == (2, 1)


# ------------------------------------------------------------------ a guarded reviewer never runs in the shared worktree (F-3)

def _guard_first_attempt(monkeypatch, *, snapshot_fails):
    """Mark only the first reviewer dispatch's harness as one with no read-only sandbox, so it takes a snapshot and a
    worktree guard. Returns the first attempt's identity and the (dispatch id, cwd) of every launch."""
    from office import adapters
    first = {}
    launched = []
    real_profile, real_guard, real_snapshot, real_launch = adapters.profile, gates._worktree_guard, gates._review_snapshot, dispatch.launch

    def profile(adapter, kind):
        prof = real_profile(adapter, kind)
        if kind == "reviewer" and first.get("harness") and (adapter or {}).get("id") == first["harness"]:
            return {**(prof or {}), "verify_worktree_unchanged": True}
        return prof

    def guard_for(d, profile_kind, cwd):
        if profile_kind != "reviewer":
            return None
        first.setdefault("dispatch", d["id"])
        first.setdefault("harness", d.get("adapter_id") or d.get("harness"))
        return real_guard(d, profile_kind, cwd) if d["id"] == first["dispatch"] else None

    def snapshot(cwd, dest):
        return None if snapshot_fails and dest.parent.name == first.get("dispatch") else real_snapshot(cwd, dest)

    def launch(run, d, kind, ddir, **kw):
        launched.append((d["id"], kw.get("cwd")))
        return real_launch(run, d, kind, ddir, **kw)

    monkeypatch.setattr(adapters, "profile", profile)
    monkeypatch.setattr(gates, "_worktree_guard", guard_for)
    monkeypatch.setattr(gates, "_review_snapshot", snapshot)
    monkeypatch.setattr(dispatch, "launch", launch)
    return first, launched


def test_a_guarded_reviewer_whose_snapshot_cannot_be_made_fails_its_attempt_and_never_runs_in_the_shared_checkout(env, monkeypatch):
    first, launched = _guard_first_attempt(monkeypatch, snapshot_fails=True)
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    attempts = gates_attempts(env, gate["id"])
    assert attempts[0]["id"] == first["dispatch"] and attempts[0]["failure"]["class"] == "snapshot"
    assert first["dispatch"] not in [d for d, _ in launched], "the reviewer was never launched"
    (row,) = _q(env, "SELECT status, terminal_classification, ended_at FROM dispatches WHERE id=?", (first["dispatch"],))
    assert row["status"] == "failed" and row["terminal_classification"] == "launch_failed" and row["ended_at"]
    (lease,) = _q(env, "SELECT released_at FROM leases WHERE dispatch_id=?", (first["dispatch"],))
    assert lease["released_at"], "the failed attempt released its lease"
    assert gate["review_status"] == "COMPLETED" and gate["verdict"] == "APPROVED", "the next route settled the review"
    assert attempts[1]["triple"] != attempts[0]["triple"]


def test_an_edit_to_the_shared_checkout_while_a_guarded_reviewer_runs_is_not_the_reviewers_write(env, monkeypatch):
    """The guard fingerprints the reviewer's own snapshot, so another process editing the shared worktree meanwhile
    neither degrades the review nor reaches the reviewer."""
    first, launched = _guard_first_attempt(monkeypatch, snapshot_fails=False)
    real_launch = dispatch.launch
    edited = []

    def launch(run, d, kind, ddir, **kw):
        if d["id"] == first.get("dispatch"):
            for name in ("converge", "review", "integration"):
                shared = paths.run_dir(run["id"]) / "checkouts" / f"{name}-{d['gate_id']}"
                if shared.is_dir():
                    (shared / "STRAY_EDIT.txt").write_text("another process")
                    edited.append(shared)
            assert Path(kw["cwd"]) != edited[-1], "the reviewer runs in its snapshot, not the shared checkout"
        return real_launch(run, d, kind, ddir, **kw)

    monkeypatch.setattr(dispatch, "launch", launch)
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    assert edited, "the shared checkout was edited while the guarded reviewer ran"
    attempts = gates_attempts(env, gate["id"])
    assert attempts[0]["failure"] is None and len(attempts) == 1, attempts
    assert gate["review_status"] == "COMPLETED" and gate["verdict"] == "APPROVED"


def gates_attempts(env, gate_id):
    con = env.con()
    try:
        return gates.review_attempts(con, gate_id)
    finally:
        con.close()


def test_a_directory_that_is_no_checkout_is_isolated_by_a_plain_copy(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "shot.png").write_bytes(b"png")
    assert gates._review_snapshot(evidence, tmp_path / "dispatch" / "snapshot") is None, "no checkout, no worktree"
    snap = gates._evidence_copy(evidence, tmp_path / "dispatch" / "snapshot")
    assert snap is not None and (snap / "shot.png").read_bytes() == b"png" and snap != evidence
    (evidence / "late.png").write_bytes(b"x")
    assert not (snap / "late.png").exists(), "an edit to the source after the copy never reaches the snapshot"


def test_a_checkout_whose_snapshot_git_refuses_has_no_snapshot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)  # a work tree with no commit: worktree add of HEAD fails
    assert gates._review_snapshot(repo, tmp_path / "dispatch" / "snapshot") is None
    assert gates._evidence_copy(repo, tmp_path / "dispatch" / "copy") is None, "a checkout is never copied around the failure"
    assert not (tmp_path / "dispatch" / "copy").exists()


# ------------------------------------------------------------------ the integration reviewer's pin (F-4)

def test_the_integration_reviewer_reads_a_pin_stored_on_the_integration_record(scratch, monkeypatch):
    from office import candidates
    con, run, tmp = scratch
    stored = {"landing": {}}
    monkeypatch.setattr(state, "get_run", lambda con_, run_id: {**run, "landing": stored["landing"]})
    monkeypatch.setattr(state, "update_run", lambda con_, run_id, **kw: stored.update(kw))
    seen = []
    monkeypatch.setattr(candidates, "declared_decision", lambda text, *, role=None, **kw: (seen.append(role), {"status": "selected"})[1])
    integration_gate = {"id": "Gi", "subject": "integration"}  # no task_id, no scope
    assert gates.reviewer_pin(con, run, "integration_reviewer", integration_gate) is None, "nothing stored: not pinned"
    pin = gates.review_pin("codex/gpt-6-luna@xhigh")
    gates.set_integration_pin(con, run, pin)
    assert stored["landing"]["integration"]["review_override"] == pin
    assert gates.reviewer_pin(con, run, "integration_reviewer", integration_gate) == pin
    assert seen == ["integration_reviewer"], "validated against the integration role's own capability"
    assert gates.reviewer_pin(con, run, "code_reviewer", integration_gate) is None, "other roles do not read it"
    gates.set_integration_pin(con, run, None)
    assert gates.reviewer_pin(con, run, "integration_reviewer", integration_gate) is None


def test_storing_an_integration_pin_keeps_the_rest_of_the_integration_record(scratch, monkeypatch):
    con, run, tmp = scratch
    stored = {"landing": {"integration": {"status": "verifying", "pr": 7}, "other": 1}}
    monkeypatch.setattr(state, "get_run", lambda con_, run_id: {**run, "landing": stored["landing"]})
    monkeypatch.setattr(state, "update_run", lambda con_, run_id, **kw: stored.update(kw))
    gates.set_integration_pin(con, run, gates.review_pin("claude/claude-opus-5-5@high"))
    assert stored["landing"]["integration"]["status"] == "verifying" and stored["landing"]["integration"]["pr"] == 7
    assert stored["landing"]["other"] == 1


# ------------------------------------------------------------------ round 2: chains, snapshots and acceptance

def _chain_spy(monkeypatch):
    seen = []
    monkeypatch.setattr(gates, "_review_attempt", lambda con, run, gate, role, brief, **kw: (
        seen.append(kw), ({"verdict": "PASS", "summary": "ok"}, None))[1])
    return seen


def _fb(epoch, chain_epoch, redispatches):
    return {"epoch": epoch, "chain_epoch": chain_epoch, "redispatches": redispatches, "commit_retries": 0,
            "resume_from": None, "correction": None}


def test_a_redispatched_chain_never_resumes_the_old_chains_reviewer(scratch, monkeypatch):
    """F8: a plan job carries `resume_from` in its payload on every re-entry; a redispatch's fresh chain must not use it."""
    con, run, tmp = scratch
    seen = _chain_spy(monkeypatch)

    def call(fb):
        job = {"kind": "plan_review", "payload": {"review_fallback": fb}}
        gates.run_reviewer(con, run, {"id": "G1"}, "plan_reviewer", "b", cwd=tmp, plan_review=True, resume_from="Dold", job=job)
        return seen[-1]["resume_from"]

    assert call(_fb(0, 0, 0)) == "Dold", "a first review still resumes the reviewer it was asked to"
    assert call(_fb(1, 1, 1)) is None, "the first attempt of a redispatched chain starts fresh"
    assert call({**_fb(2, 1, 1), "resume_from": "Dnew", "correction": "c"}) == "Dnew", \
        "a correction in that chain carries its own resume, not the job's"
    assert call(_fb(2, 1, 1)) is None, "past the first round the job's own resume_from is never used"


def test_a_call_outside_the_job_flow_starts_a_fresh_chain(scratch, monkeypatch):
    """F9: the bounded no-job retry (a self-review subagent) must not stay excluded from the routes an earlier call failed on."""
    con, run, tmp = scratch
    seen = _chain_spy(monkeypatch)
    prior = [{"id": "D0", "triple": "t0", "harness": "h0", "epoch": 0, "failure": {"class": "launch", "reason": "x"}}]
    monkeypatch.setattr(gates, "review_attempts", lambda con_, gid: prior)
    gates.run_reviewer(con, run, {"id": "G1"}, "code_reviewer", "b", cwd=tmp)
    assert seen[0]["epoch"] == 1 and seen[0]["chain_epoch"] == 1


def test_a_no_job_retry_runs_on_a_route_the_exhausted_earlier_call_failed_on(scratch, monkeypatch):
    from office import candidates
    con, run, tmp = scratch
    prior = [{"id": "D0", "triple": "t0", "harness": "h0", "epoch": 0,
              "failure": {"class": "silent", "reason": "x", "harness_wide": True}}]
    asked = []
    monkeypatch.setattr(gates, "review_attempts", lambda con_, gid: prior)
    monkeypatch.setattr(candidates, "route_role", lambda *a, **k: (asked.append(set(k.get("exclude") or ())),
                                                                   {"status": "no_route"})[1])
    monkeypatch.setattr(gates, "reviewer_pin", lambda *a, **k: None)
    monkeypatch.setattr(gates, "_producer_route", lambda *a, **k: None)
    monkeypatch.setattr(state, "get_run", lambda con_, run_id: run)
    gates.run_reviewer(con, run, {"id": "G1", "task_id": "T1"}, "code_reviewer", "b", cwd=tmp)
    assert asked and not (asked[0] & {"t0", "harness:h0"}), "the exhausted earlier call's routes are eligible again"


def _launch_kwargs(monkeypatch):
    captured = {}
    real = dispatch.launch

    def spy(run, d, kind, ddir, **kw):
        captured.setdefault(d["id"], {"ddir": ddir, **kw})
        return real(run, d, kind, ddir, **kw)

    monkeypatch.setattr(dispatch, "launch", spy)
    return captured


def test_a_guarded_reviewer_is_pointed_only_at_its_snapshot(env, monkeypatch):
    """F15: cwd is the snapshot, so its include dirs and the path its brief names must be the snapshot too."""
    first, launched = _guard_first_attempt(monkeypatch, snapshot_fails=False)
    captured = _launch_kwargs(monkeypatch)
    _start(env, gear="XS", extra=("--no-fastlane",), executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    kw = captured[first["dispatch"]]
    snapshot = Path(kw["cwd"])
    shared = paths.run_dir(gate["run_id"]) / "checkouts" / f"converge-{gate['id']}"
    assert snapshot.name == "snapshot" and snapshot != shared
    assert kw["include_dirs"] and all(Path(p) == snapshot or snapshot in Path(p).parents for p in kw["include_dirs"]), kw["include_dirs"]
    brief = (kw["ddir"] / "brief.md").read_text()
    assert str(shared) not in brief and str(snapshot) in brief, "the brief names the snapshot, not the shared checkout"
    assert [a["failure"] for a in gates_attempts(env, gate["id"])][0] is None


def test_retargeting_rewrites_only_paths_of_the_checkout():
    cwd, snap = Path("/r/checkouts/c-1"), Path("/r/dispatches/D1/snapshot")
    text = "read /r/checkouts/c-1/src and /r/checkouts/c-1. not /r/checkouts/c-10 or /r/checkouts/c-1-x"
    assert gates._retarget_text(text, cwd, snap) == ("read /r/dispatches/D1/snapshot/src and /r/dispatches/D1/snapshot. "
                                                     "not /r/checkouts/c-10 or /r/checkouts/c-1-x")
    assert gates._retarget_path(Path("/r/checkouts/c-1/a/b"), cwd, snap) == snap / "a" / "b"
    assert gates._retarget_path(cwd, cwd, snap) == snap
    assert gates._retarget_path(Path("/elsewhere/evidence"), cwd, snap) == Path("/elsewhere/evidence")


def test_a_guarded_synchronous_reviewer_keeps_its_snapshot_until_it_is_checked(env, monkeypatch):
    """F10: a guarded reviewer run without a job (a self-review subagent) is judged on its snapshot, not on a removed one."""
    from test_issue809_self_review_subagents import _subagent_gates
    first, launched = _guard_first_attempt(monkeypatch, snapshot_fails=False)
    _s_run(env)
    (sub,) = _subagent_gates(env)
    attempts = gates_attempts(env, sub["id"])
    assert attempts[0]["id"] == first["dispatch"] and attempts[0]["failure"] is None, attempts[0]
    assert sub["review_status"] == contract.COMPLETED and len(attempts) == 1
    assert not (paths.run_dir(sub["run_id"]) / "dispatches" / first["dispatch"] / "snapshot").exists(), "settling removed it"


def test_a_crash_between_an_integration_verdict_and_its_acceptance_leaves_the_gate_open(scratch, monkeypatch):
    """F12: the gate finishes in the acceptance's own transition, so a reclaimed job still finds it open."""
    from office import integration
    con, run, tmp = scratch
    _gate_row(con, "Gint", subject="integration", kind="integration_review")
    crashed = []

    def set_integration(con_, run_, **fields):
        if fields.get("status") == "accepted" and not crashed:
            crashed.append(1)
            raise RuntimeError("the supervisor died here")

    monkeypatch.setattr(integration, "_set_integration", set_integration)
    monkeypatch.setattr(integration, "compose_base", lambda run_: "b" * 40)
    monkeypatch.setattr(integration.verification_coverage, "audit", lambda *a, **k: None)
    monkeypatch.setattr(integration.state, "get_run", lambda con_, run_id: run)
    monkeypatch.setattr(integration.state, "emit", lambda *a, **k: None)
    monkeypatch.setattr(gates, "settle_reviewer", lambda *a, **k: {"verdict": "PASS", "summary": "ok", "parsed": None})
    monkeypatch.setattr(gates, "remove_checkout", lambda *a, **k: None)
    ctx = {"key": "k", "commit": "c" * 40, "base": "b" * 40, "branch": "b", "integ_rev": {"id": "I1"}, "results": {},
           "run_checks": [], "executed": [], "checks_gate_id": "Gc"}
    job = {"id": "J", "kind": "integration_review", "payload": {"gate_id": "Gint", gates.SETTLE_KEY: {"dispatch_id": "Dint"}}}
    with pytest.raises(RuntimeError):
        integration._review_composed(con, run, job, "Gint", ctx)
    assert tuple(con.execute("SELECT status, verdict FROM gates WHERE id='Gint'").fetchone()) == ("running", None)
    assert integration._review_composed(con, run, job, "Gint", ctx) == {"integration_review": "PASS"}  # the reclaimed run
    assert tuple(con.execute("SELECT status, verdict FROM gates WHERE id='Gint'").fetchone()) == ("done", "PASS")

# ------------------------------------------------------------------ F11 on the unit tier: the job reaches every reviewer
def _plan_gate(con):
    row = _gate_row(con, "Glead", subject="plan", kind="plan_review")
    return {**row, "plan_version": 1, "round": 1}
def _launch_result(job_id="Jsup", dispatch="Dgf"):
    return {"supervise_queued": job_id, "fallback_queued": job_id, "dispatch_id": dispatch}
def test_the_first_goldfish_launches_with_the_job_and_its_launch_ends_the_call(scratch, monkeypatch):
    from office import xl_review
    con, run, tmp = scratch
    seen = []
    monkeypatch.setattr(gates, "run_reviewer", lambda con_, run_, gate, role, brief, **kw: (seen.append((gate, kw)), _launch_result())[1])
    job = {"kind": "plan_review", "payload": {"gate_id": "Glead", "resume_from": "Dold"}}
    out = xl_review.audit_plan_job(con, {**run, "risk": {}}, _plan_gate(con), {"tasks": []}, "ROLE r\nbody", tmp, job)
    assert out["supervise_queued"] == "Jsup", "the call ends at the launch: nothing waits for the Goldfish"
    ((gate, kw),) = seen
    stage = kw["job"]["payload"]["xl"]["current"]
    assert gate["id"] == stage["gate"] != "Glead" and stage["name"] == "requirements"
    assert kw["job"]["payload"]["gate_id"] == gate["id"] and kw["job"]["payload"]["xl"]["lead_gate"] == "Glead"
    assert con.execute("SELECT subject FROM gates WHERE id=?", (gate["id"],)).fetchone()["subject"] == "goldfish:requirements"
def test_a_settling_goldfish_job_settles_its_stage_and_launches_the_next_without_rerunning_it(scratch, monkeypatch):
    from office import xl_review
    con, run, tmp = scratch
    lead = _plan_gate(con)
    stage_gate = _gate_row(con, "Gfish1", subject="goldfish:requirements", kind=xl_review.GOLDFISH_KIND)
    launched, settled = [], []
    monkeypatch.setattr(gates, "run_reviewer", lambda con_, run_, gate, role, brief, **kw: (launched.append((gate["id"], kw["job"])), _launch_result("Jnext", "Dnext"))[1])
    monkeypatch.setattr(gates, "settle_reviewer", lambda con_, run_, gate, job: (settled.append(gate["id"]), {"status": "COMPLETED"})[1])
    monkeypatch.setattr(xl_review, "_record_goldfish", lambda con_, run_, g, name, outcome, dispatches, sessions, *, live: {"rubric": name})
    xl = {"lead_gate": "Glead", "reports": [], "dispatches": [], "sessions": [], "current": {"name": "requirements", "gate": "Gfish1"}}
    job = {"kind": "plan_review", "payload": {"gate_id": "Gfish1", gates.SETTLE_KEY: {"dispatch_id": "Dgf"}, "xl": xl}}
    out = xl_review.audit_plan_job(con, {**run, "risk": {}}, lead, {"tasks": []}, "ROLE r\nbody", tmp, job)
    assert settled == ["Gfish1"] and out["supervise_queued"] == "Jnext"
    ((next_gate, next_job),) = launched
    assert next_gate != "Gfish1" and gates.SETTLE_KEY not in next_job["payload"], "the next stage launches afresh"
    assert next_job["payload"]["xl"]["reports"] == [{"rubric": "requirements"}] and next_job["payload"]["xl"]["current"]["name"] == "architecture"
def test_a_goldfish_fallback_job_reruns_the_same_stage_on_its_own_gate(scratch, monkeypatch):
    from office import xl_review
    con, run, tmp = scratch
    _gate_row(con, "Gfish1", subject="goldfish:requirements", kind=xl_review.GOLDFISH_KIND)
    seen = []
    monkeypatch.setattr(gates, "run_reviewer", lambda con_, run_, gate, role, brief, **kw: (seen.append((gate["id"], kw["job"])), _launch_result())[1])
    xl = {"lead_gate": "Glead", "reports": [], "dispatches": [], "sessions": [], "current": {"name": "requirements", "gate": "Gfish1"}}
    fb = {"epoch": 0, "chain_epoch": 0}
    job = {"kind": "plan_review", "payload": {"gate_id": "Gfish1", "review_fallback": fb, "xl": xl}}
    xl_review.audit_plan_job(con, {**run, "risk": {}}, _plan_gate(con), {"tasks": []}, "ROLE r\nbody", tmp, job)
    ((gid, rerun),) = seen
    assert gid == "Gfish1" and rerun["payload"]["review_fallback"] == fb, "the next route of the same chain"
    assert con.execute("SELECT COUNT(*) n FROM gates WHERE kind='goldfish_audit'").fetchone()["n"] == 1
def test_a_compose_job_belongs_to_the_chain_it_was_queued_for():
    mark = {"gates": ["G1", "G2"], "epoch": 1}
    job = lambda fb: {"payload": {srs.COLLECT_KEY: mark, **({"review_fallback": fb} if fb else {})}}
    assert srs.collected_gates(job({"epoch": 1})) == ["G1", "G2"]
    assert srs.collected_gates(job({"epoch": 2})) is None, "a redispatch launches the subagents anew"
    assert srs.collected_gates({"payload": {}}) is None
    assert srs.collected_gates({"payload": {srs.COLLECT_KEY: {"gates": ["G1"], "epoch": 0}}}) == ["G1"]
def test_only_the_supervise_jobs_of_subagents_name_subagent_gates():
    row = lambda kind, **p: {"kind": kind, "payload_json": json.dumps(p)}
    sub = {"origin": {"kind": "convergence_review", "payload": {"subagent": {"key": "k"}}}}
    lane = {"origin": {"kind": "convergence_review", "payload": {"scope": "L-T1"}}}
    assert srs.supervised_gates([row("review_supervise", gate_id="Gs", **sub), row("review_supervise", gate_id="Gl", **lane),
                                 row("convergence_review", gate_id="Gx", **sub)]) == ["Gs"]

def _subagent_scope(con, tmp, monkeypatch, rubrics=("edge-cases", "test-strength")):
    """A funded run with one task whose accepted revision awaits `rubrics` subagent reviews, on a scratch database."""
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, base_commit, commit_sha, tree_sha, requirements_version, "
                "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES('R1v','R1','T1',1,'b','c','t',1,1,1,'e','op','accepted','2026-01-01')")
    monkeypatch.setattr(srs, "rubrics", lambda run, task: rubrics)
    monkeypatch.setattr(srs.paths, "git", lambda *a, **k: "")
    run = {"id": "R1", "repo_root": str(tmp), "office_version": "3.1", "gear": "S", "plan_version": 1, "risk": {},
           "gates": {"gear_model_version": "run-gear-1"}}
    return run, [{"id": "T1", "title": "t", "scope": [], "accepted_revision_id": "R1v"}]
def test_every_subagent_launches_with_the_job_and_the_call_returns_waiting(scratch, monkeypatch):
    """F11: independent subagents each launch through run_reviewer with the convergence job (none is waited on) and the
    call returns at once; their supervise jobs settle them."""
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch)
    seen = []
    monkeypatch.setattr(gates, "run_reviewer", lambda con_, run__, gate, role, brief, **kw: (seen.append((gate, kw)), _launch_result("J" + gate["id"], "D" + gate["id"]))[1])
    job = {"kind": "convergence_review", "payload": {"gate_id": "Glane", "scope": "L-T1"}}
    out = srs.audit_scope(con, run, tasks, tmp, "c", job=job, retry=True)
    assert out["status"] == srs.WAITING
    assert [kw["job"]["payload"]["subagent"]["key"].split(":")[2] for _, kw in seen] == ["edge-cases", "test-strength"]
    for gate, kw in seen:
        assert kw["job"]["payload"]["gate_id"] == gate["id"] and kw["job"]["payload"]["scope"] == "L-T1"
        assert kw["job"]["payload"]["subagent"]["parent"]["payload"]["gate_id"] == "Glane"
        assert kw["job"]["payload"]["subagent"]["keys"] == [k["job"]["payload"]["subagent"]["key"] for _, k in seen], "each knows its siblings"
    assert {r["status"] for r in con.execute("SELECT status FROM gates WHERE kind='self_review_subagent'")} == {"queued"}
    assert con.execute("SELECT COUNT(*) n FROM outbox WHERE dedup_key LIKE 'self_review_compose:%'").fetchone()["n"] == 0, \
        "the last settle queues the compose job, not the launching call while one is still open"
def test_subagents_that_find_no_route_are_composed_in_the_launching_call(scratch, monkeypatch):
    con, run_, tmp = scratch
    run, tasks = _subagent_scope(con, tmp, monkeypatch, rubrics=("combined",))
    monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: {"status": contract.UNAVAILABLE, "summary": "no route"})
    job = {"kind": "convergence_review", "payload": {"gate_id": "Glane", "scope": "L-T1"}}
    assert srs.audit_scope(con, run, tasks, tmp, "c", job=job, retry=True)["status"] == srs.RETRY
    assert srs.audit_scope(con, run, tasks, tmp, "c", job=job, retry=False)["status"] == contract.EVIDENCE_BLOCKED
