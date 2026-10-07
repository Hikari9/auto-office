"""One run_checks job, one owner (#403, #404).

The failure chain these pin down: a checks worker whose liveness could not be
read was taken for dead, its claim was requeued while it ran on, a second
`office _job` ran the same check suite beside it, both verdicts were applied,
and the task ended up both accepted and changes-required. A worker that really
died left its gate `running` with no owner, and the stall detector reported
"no job is queued or running" while `_job` processes were alive.

The concurrency tests run real `office _job` processes against a check command
that counts how many copies of itself run at once, so they measure the
invariant (at most one execution per job) rather than the final database rows.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run

PROBE = r'''
import fcntl, json, os, signal, sys, time
d, secs = sys.argv[1], float(sys.argv[2])
os.makedirs(d, exist_ok=True)
if len(sys.argv) > 3 and sys.argv[3] == "stubborn" and not os.path.exists(os.path.join(d, "stubborn.done")):
    # The first run outlives its worker: it ignores the SIGTERM a supervisor sends.
    open(os.path.join(d, "stubborn.done"), "w").close()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
lock = open(os.path.join(d, "probe.lock"), "a")

def bump(delta):
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = os.path.join(d, "state.json")
    st = json.load(open(path)) if os.path.exists(path) else {"now": 0, "max": 0, "runs": 0}
    st["now"] += delta
    st["max"] = max(st["max"], st["now"])
    st["runs"] += 1 if delta > 0 else 0
    json.dump(st, open(path, "w"))
    fcntl.flock(lock, fcntl.LOCK_UN)

bump(1)
time.sleep(secs)
bump(-1)
'''


def _probe_plan(env, secs: float, *extra: str) -> tuple[str, Path]:
    probe = env.tmp / "probe.py"
    probe.write_text(PROBE)
    out = env.tmp / "probe"
    command = " ".join(["python3", str(probe), str(out), str(secs), *extra])
    return PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {command}"), out


def _probe_state(out: Path) -> dict:
    path = out / "state.json"
    return json.loads(path.read_text()) if path.exists() else {"now": 0, "max": 0, "runs": 0}


def _submitted(env, monkeypatch, plan=PLAN_ONE):
    """T1 submitted, its run_checks job queued and not started."""
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}] * 3)
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    env.office("dispatch", "T1", check=0)
    from office import jobs, state
    con = env.con()
    jobs.execute(con, con.execute("SELECT id FROM outbox WHERE kind='launch_agent' AND status='queued'").fetchone()[0])
    job = con.execute("SELECT * FROM outbox WHERE kind='run_checks' AND status='queued'").fetchone()
    assert job is not None
    run = state.get_run(con, job["run_id"])
    return con, run, dict(job)


def _job_process(env, job_id: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "office", "_job", job_id], cwd=str(env.repo),
                            env={**os.environ, "OFFICE_JOBS": "manual"}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)


def _until(pred, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _row(con, job_id):
    return dict(con.execute("SELECT * FROM outbox WHERE id=?", (job_id,)).fetchone())


def _gate(con, gate_id):
    return dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())


def _events(con, run_id, kind):
    return [dict(r) for r in con.execute("SELECT * FROM events WHERE run_id=? AND kind=? ORDER BY seq", (run_id, kind))]


# ---------------------------------------------------------------- at most one execution per job

def test_a_live_claim_wrongly_requeued_never_runs_twice_at_once(env, monkeypatch):
    """#403's trigger: attempt A is running when its claim is requeued (an older
    runtime took an unreadable `ps` for death). The second `office _job` that a
    kick then starts cannot run the suite beside A, and A's late result, now
    unowned, is never applied. The retry runs once A is gone: one verdict."""
    plan, out = _probe_plan(env, 3)
    con, run, job = _submitted(env, monkeypatch, plan)
    gate_id = json.loads(job["payload_json"])["gate_id"]
    first = _job_process(env, job["id"])
    assert _until(lambda: _probe_state(out)["now"] == 1)
    con.execute("UPDATE outbox SET status='queued', claim_token=NULL, claimed_pid=NULL WHERE id=?", (job["id"],))
    second = _job_process(env, job["id"])
    assert second.wait(timeout=60) == 0
    assert _row(env.con(), job["id"])["status"] == "queued"  # it never claimed: A's processes hold the lock
    assert first.wait(timeout=60) == 1, first.stdout.read()  # A lost its claim: its result is discarded
    assert _gate(env.con(), gate_id)["status"] == "running"
    third = _job_process(env, job["id"])
    assert third.wait(timeout=60) == 0, third.stdout.read()
    st = _probe_state(out)
    assert st["max"] == 1 and st["runs"] == 2, st
    con = env.con()
    row = _row(con, job["id"])
    assert row["status"] == "done", row
    gate = _gate(con, gate_id)
    assert gate["status"] == "done" and gate["verdict"] == "APPROVED", gate
    assert len(_events(con, run["id"], "gate.approved")) == 1
    assert len(_events(con, run["id"], "job.stale_attempt")) == 1
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    log = jobs_log(run, job["id"])
    assert "still hold the execution lock" in log and "discarded" in log, log


def jobs_log(run: dict, job_id: str) -> str:
    from office import jobs
    return jobs.log_path(run["id"], job_id).read_text()


def test_a_replacement_never_starts_beside_checks_its_dead_worker_left_running(env, monkeypatch):
    """The worker is SIGKILLed mid-check and its check command survives the
    supervisor's SIGTERM. The claim is retried, but the retry's checks cannot
    start until every process of the first attempt is gone."""
    plan, out = _probe_plan(env, 4, "stubborn")
    con, run, job = _submitted(env, monkeypatch, plan)
    first = _job_process(env, job["id"])
    assert _until(lambda: _probe_state(out)["now"] == 1)
    worker = _row(env.con(), job["id"])["claimed_pid"]
    os.kill(worker, signal.SIGKILL)
    assert first.wait(timeout=30) != 0
    con = env.con()
    row = _row(con, job["id"])
    assert row["status"] == "queued" and row["claim_token"] is None and row["attempts"] == 1, row
    # The orphaned check still runs and still holds the job's execution lock.
    assert _probe_state(out)["now"] == 1
    retry = _job_process(env, job["id"])
    assert retry.wait(timeout=30) == 0
    assert _row(env.con(), job["id"])["status"] == "queued"  # it could not start
    assert _until(lambda: _probe_state(out)["now"] == 0)
    again = _job_process(env, job["id"])
    assert again.wait(timeout=60) == 0, again.stdout.read()
    st = _probe_state(out)
    assert st["max"] == 1 and st["runs"] == 2, st
    con = env.con()
    assert _row(con, job["id"])["status"] == "done"
    retried = _events(con, run["id"], "job.retry")
    assert len(retried) == 1
    diag = json.loads(retried[0]["payload_json"])
    assert diag["exit"] == "killed by SIGKILL" and diag["pid"] == worker and diag["children_stopped"], diag
    log = jobs_log(run, job["id"])
    assert "killed by SIGKILL" in log and "requeued: worker process died" in log, log


# ---------------------------------------------------------------- a stale attempt commits nothing

def test_an_attempt_that_lost_its_claim_writes_nothing(env, monkeypatch):
    """Attempt A runs; its claim is recovered and attempt B takes the job and
    approves the gate; A then finishes late with a failing check. A's result
    is audit evidence only: no verdict, finding, task status or job result."""
    con, run, job = _submitted(env, monkeypatch)
    from office import db, gates, jobs
    gate_id = json.loads(job["payload_json"])["gate_id"]

    def late_failure(con_, run_, commands, cwd, rev, gate, **kw):
        with db.unfenced():  # attempt B, elsewhere, owns the job from here on
            other = env.con()
            with db.transaction(other):
                other.execute("UPDATE outbox SET claim_token='B-attempt', attempts=attempts+1 WHERE id=?", (job["id"],))
                gates.ingest_task_gate(other, run, gate_id, gates._run_commands(other, run, commands, cwd, rev, gate, {}))
            other.close()
        failing = gates.review_parse.Parsed(verdict="CHANGES_REQUIRED", findings=[
            {"code": "C1", "severity": "material", "location": "check", "summary": "late failure", "action": "fix"}])
        return {"verdict": "CHANGES_REQUIRED", "parsed": failing, "summary": "0/1 checks passed", "route": "deterministic"}

    monkeypatch.setattr(gates, "run_commands", late_failure)
    assert jobs.execute(con, job["id"]) == 1
    con = env.con()
    gate = _gate(con, gate_id)
    assert gate["verdict"] == "APPROVED", gate
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    assert con.execute("SELECT COUNT(*) FROM findings WHERE task_id='T1'").fetchone()[0] == 0
    assert not _events(con, run["id"], "gate.recheck") and not _events(con, run["id"], "task.findings_queued")
    row = _row(con, job["id"])
    assert row["status"] == "claimed" and row["claim_token"] == "B-attempt" and row["result_json"] is None, row
    (stale,) = _events(con, run["id"], "job.stale_attempt")
    assert "lost its claim" in stale["summary"]


# ---------------------------------------------------------------- one verdict per gate round

@pytest.mark.parametrize("first,second", [("pass", "fail"), ("fail", "pass")])
def test_a_decided_checks_gate_rejects_a_second_result(env, monkeypatch, first, second):
    con, run, job = _submitted(env, monkeypatch)
    from office import db, gates, review_parse
    gate_id = json.loads(job["payload_json"])["gate_id"]
    outcomes = {
        "pass": {"verdict": "PASS", "parsed": review_parse.Parsed(verdict="PASS", findings=[]), "summary": "1/1 checks passed"},
        "fail": {"verdict": "CHANGES_REQUIRED", "summary": "0/1 checks passed", "parsed": review_parse.Parsed(
            verdict="CHANGES_REQUIRED", findings=[{"code": "C1", "severity": "material", "location": "c",
                                                   "summary": "vitest failed", "action": "fix"}])},
    }
    for which in (first, second):
        with db.transaction(con):
            gates.ingest_task_gate(con, run, gate_id, dict(outcomes[which]))
    gate = _gate(con, gate_id)
    task = con.execute("SELECT status, accepted_revision_id FROM tasks WHERE id='T1'").fetchone()
    if first == "pass":
        assert gate["verdict"] == "APPROVED" and task["status"] == "accepted", (gate, dict(task))
        assert con.execute("SELECT COUNT(*) FROM findings WHERE task_id='T1' AND state='open'").fetchone()[0] == 0
        assert not _events(con, run["id"], "gate.recheck")
    else:
        assert gate["verdict"] == "RECHECK" and task["status"] == "changes_required", (gate, dict(task))
        assert task["accepted_revision_id"] is None and not _events(con, run["id"], "task.accepted")
    (dup,) = _events(con, run["id"], "gate.duplicate_result")
    payload = json.loads(dup["payload_json"])
    assert payload["recorded"]["verdict"] == gate["verdict"]
    assert payload["rejected"]["verdict"] == outcomes[second]["verdict"]


@pytest.mark.review_contract("v3.1")
def test_a_decided_v31_gate_rejects_a_second_result(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import db, gates, review_parse
    gate_id = json.loads(job["payload_json"])["gate_id"]
    with db.transaction(con):
        gates.ingest_task_gate(con, run, gate_id, {"verdict": "PASS", "parsed": review_parse.Parsed(verdict="PASS")})
    with db.transaction(con):
        gates.ingest_task_gate(con, run, gate_id, {"verdict": "CHANGES_REQUIRED", "summary": "late", "parsed":
                               review_parse.Parsed(verdict="CHANGES_REQUIRED", findings=[
                                   {"code": "C1", "severity": "material", "location": "c", "summary": "x"}])})
    assert _gate(con, gate_id)["verdict"] == "PASS"
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] != "changes_required"
    assert con.execute("SELECT COUNT(*) FROM findings WHERE task_id='T1'").fetchone()[0] == 0
    assert len(_events(con, run["id"], "gate.duplicate_result")) == 1


def test_a_stale_revision_result_is_not_a_duplicate(env, monkeypatch):
    """Different audit reasons: a result for a superseded revision lands as
    `stale` on an open gate; only a result for a decided gate is a duplicate."""
    con, run, job = _submitted(env, monkeypatch)
    from office import db, gates
    gate_id = json.loads(job["payload_json"])["gate_id"]
    con.execute("UPDATE tasks SET current_revision_id='R-newer' WHERE id='T1'")
    with db.transaction(con):
        gates.ingest_task_gate(con, run, gate_id, {"verdict": "PASS", "summary": "1/1"})
    gate = _gate(con, gate_id)
    assert gate["status"] == "stale" and "no longer current" in gate["stale_reason"]
    assert not _events(con, run["id"], "gate.duplicate_result")


# ---------------------------------------------------------------- liveness is tri-state

def test_an_unreadable_start_time_is_unknown_not_dead(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import util
    me = os.getpid()
    claim = util.claim_identity(me)
    assert util.claim_liveness(me, claim)[0] == util.ALIVE
    monkeypatch.setattr(util, "process_start", lambda pid: None)  # `ps` failed or is not allowed here
    state, why = util.claim_liveness(me, claim)
    assert state == util.UNKNOWN and "could not be read" in why
    assert util.claim_alive(me, claim)
    assert util.claim_liveness(99999999, claim)[0] == util.DEAD


def test_reclaim_leaves_an_unknown_owner_alone(env, monkeypatch):
    """An older runtime's claim (no token) on a live process whose start time
    cannot be read now: no requeue, no second worker (#403's trigger)."""
    con, run, job = _submitted(env, monkeypatch)
    from office import db, gates, guide, jobs, util
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        con.execute("UPDATE outbox SET status='claimed', attempts=1, claimed_pid=?, claimed_by=? WHERE id=?",
                    (sleeper.pid, util.claim_identity(sleeper.pid), job["id"]))
        gate_id = json.loads(job["payload_json"])["gate_id"]
        con.execute("UPDATE gates SET status='running' WHERE id=?", (gate_id,))
        monkeypatch.setattr(util, "process_start", lambda pid: None)
        for _ in range(3):  # status, wait and resume all reclaim
            with db.transaction(con):
                assert jobs.reclaim(con, run["id"]) == 0
        assert _row(con, job["id"])["status"] == "claimed"
        assert guide.stalls(con, run) == []
        owner, detail = gates.owner_state(con, run, _gate(con, gate_id))
        assert owner == "unknown" and "could not be read" in detail
        assert "liveness unknown" in guide._waiting_on(con, run, dict(con.execute("SELECT * FROM tasks").fetchone()))
    finally:
        sleeper.kill()


def test_a_held_execution_lock_is_alive_and_a_free_one_is_dead(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import jobs, util
    con.execute("UPDATE outbox SET status='claimed', attempts=1, claim_token='A1', claimed_pid=1 WHERE id=?", (job["id"],))
    row = _row(con, job["id"])
    assert jobs.claim_state(row) == (util.UNKNOWN, "its execution lock file is missing")
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); "
                               "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
                               str(jobs.lock_path(run["id"], job["id"]))], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert jobs.claim_state(row)[0] == util.ALIVE
    finally:
        holder.kill()
        holder.wait()
    assert jobs.claim_state(row)[0] == util.DEAD


# ---------------------------------------------------------------- a dead worker is recovered once

def _dead_claim(con, job, attempts):
    """A token claim whose owner is gone: nothing holds its execution lock."""
    from office import jobs
    con.execute("UPDATE outbox SET status='claimed', attempts=?, claim_token='Adead', claimed_pid=999999, "
                "claimed_by='host:999999@then', claimed_at='2026-10-07T00:00:00+00:00' WHERE id=?", (attempts, job["id"]))
    path = jobs.lock_path(job["run_id"], job["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    con.execute("UPDATE gates SET status='running' WHERE id=?", (json.loads(job["payload_json"])["gate_id"],))


def test_a_dead_worker_with_attempts_left_is_retried_exactly_once(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import db, guide, jobs
    _dead_claim(con, job, attempts=1)
    for _ in range(3):  # repeated status / wait / resume calls
        with db.transaction(con):
            jobs.reclaim(con, run["id"])
    row = _row(con, job["id"])
    assert row["status"] == "queued" and row["claim_token"] is None and row["attempts"] == 1, row
    assert len(_events(con, run["id"], "job.retry")) == 1
    log = jobs.log_path(run["id"], job["id"]).read_text()
    assert log.strip() and "requeued: worker process died: no process holds its execution lock" in log
    assert '"pid": 999999' in log and '"claim": "Adead"' in log and "not observed" in log
    assert guide.stalls(con, run) == []  # its gate has a queued owner again


def test_a_dead_worker_out_of_attempts_settles_its_gate_and_resume_reruns_it(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import db, guide, jobs
    gate_id = json.loads(job["payload_json"])["gate_id"]
    _dead_claim(con, job, attempts=2)
    with db.transaction(con):
        jobs.reclaim(con, run["id"])
    assert _row(con, job["id"])["status"] == "failed"
    gate = _gate(con, gate_id)
    assert gate["status"] == "done" and gate["review_status"] == "UNAVAILABLE", gate
    task = dict(con.execute("SELECT * FROM tasks WHERE id='T1'").fetchone())
    assert task["status"] == "blocked" and task["pause_reason"].startswith("checks gate unavailable"), task
    assert "office resume" in task["pause_reason"]
    assert guide.next_action(con, run).startswith("resolve T1 (checks gate unavailable")
    assert not [s for s in guide.stalls(con, run) if "no job is queued or running" in s]
    (failed,) = _events(con, run["id"], "job.failed")
    assert json.loads(failed["payload_json"])["liveness"].startswith("no process holds")
    monkeypatch.setenv("OFFICE_JOBS", "inline")
    env.office("resume", check=0)
    con = env.con()
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    assert con.execute("SELECT COUNT(*) FROM gates WHERE kind='checks' AND task_id='T1'").fetchone()[0] == 2


def test_resume_settles_a_gate_an_older_runtime_left_running_after_its_job_failed(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import db, guide, lifecycle
    gate_id = json.loads(job["payload_json"])["gate_id"]
    con.execute("UPDATE outbox SET status='failed', error='worker process died', attempts=2 WHERE id=?", (job["id"],))
    con.execute("UPDATE gates SET status='running' WHERE id=?", (gate_id,))
    (stall,) = guide.stalls(con, run, since="9999")
    assert "is running but no job is queued or running" in stall
    with db.transaction(con):
        notes = lifecycle.reconcile(con, run)
    assert any("gate settled" in n for n in notes)
    assert _gate(con, gate_id)["status"] == "done"
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "blocked"
    assert guide.stalls(con, run, since="9999") == []


def test_a_live_job_is_never_reported_as_no_job_running(env, monkeypatch):
    con, run, job = _submitted(env, monkeypatch)
    from office import guide, jobs
    gate_id = json.loads(job["payload_json"])["gate_id"]
    con.execute("UPDATE outbox SET status='claimed', attempts=1, claim_token='Alive', claimed_pid=1 WHERE id=?",
                (job["id"],))
    con.execute("UPDATE gates SET status='running' WHERE id=?", (gate_id,))
    fd = jobs.try_lock(run["id"], job["id"])  # this process stands in for the live worker
    try:
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], pass_fds=(fd,))
    finally:
        os.close(fd)
    try:
        assert guide.stalls(con, run) == []
    finally:
        holder.kill()
        holder.wait()
    (stall,) = guide.stalls(con, run)
    assert "worker is gone" in stall
