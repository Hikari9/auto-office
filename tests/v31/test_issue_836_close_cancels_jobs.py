"""#836: `office close --landed-externally` leaves the run's integration-check
`_job` running, restarting the full suite.

A check job a live `office _job` claims still holds its claim after the close
(it saw only queued rows), so the run ended and validate.sh kept running under
the orphaned job — CPU for a result nothing reads — and when a check died by
hand, the job pushed on into the next suite stage. The reproductions run an
isolated home and Office's fake harness binaries, with a claimed run_checks
job whose long check command counts its own concurrency and how many times it
ever ran.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, approved_run

# The probe test_job_ownership uses, plus the pid it runs as: a stopped check
# is proven by its pid answering nothing — "now" stays 1 in its state file
# when it was stopped instead of finishing.
PROBE = r'''
import fcntl, json, os, sys, time
d, secs = sys.argv[1], float(sys.argv[2])
os.makedirs(d, exist_ok=True)
lock = open(os.path.join(d, "probe.lock"), "a")

def bump(delta, pid=0):
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = os.path.join(d, "state.json")
    st = json.load(open(path)) if os.path.exists(path) else {"now": 0, "max": 0, "runs": 0, "pid": None}
    st["now"] += delta
    st["max"] = max(st["max"], st["now"])
    st["runs"] += 1 if delta > 0 else 0
    if pid:
        st["pid"] = pid
    json.dump(st, open(path, "w"))
    fcntl.flock(lock, fcntl.LOCK_UN)

bump(1, os.getpid())
time.sleep(secs)
bump(-1)
'''

_EMPTY_STATE = {"now": 0, "max": 0, "runs": 0, "pid": None}


def _probe_plan(env, secs: float) -> tuple[str, Path]:
    probe = env.tmp / "probe.py"
    probe.write_text(PROBE)
    out = env.tmp / "probe"
    command = " ".join(["python3", str(probe), str(out), str(secs)])
    return PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {command}"), out


def _probe_state(out: Path) -> dict:
    path = out / "state.json"
    for _ in range(6):  # the probe rewrites the file in place; a torn read retries
        try:
            return json.loads(path.read_text()) if path.exists() else dict(_EMPTY_STATE)
        except ValueError:
            time.sleep(0.05)
    return dict(_EMPTY_STATE)


def _until(pred, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _check_gone(out: Path, started: bool = False) -> bool:
    """The check command is provably gone: nothing lives at the pid it ran as."""
    from office.util import pid_state
    pid = _probe_state(out).get("pid")
    return (started or pid is not None) and pid_state(pid) == "dead"


def _row(con, job_id) -> dict:
    return dict(con.execute("SELECT * FROM outbox WHERE id=?", (job_id,)).fetchone())


def _pending(env, run_id: str) -> list:
    rows = env.con().execute("SELECT status FROM outbox WHERE run_id=? AND status IN ('queued','claimed')",
                             (run_id,)).fetchall()
    return sorted(r["status"] for r in rows)


def _gate(env, gate_id: str) -> dict:
    return dict(env.con().execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())


def _job_process(env, job_id: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "office", "_job", job_id], cwd=str(env.repo),
                            env={**os.environ, "OFFICE_JOBS": "manual"}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)


def _gh(env, oid: str, pr: str = "https://github.com/o/r/pull/7"):
    """A fake gh that reports PR 7 merged, its merge commit already made."""
    (env.bin / "gh").write_text("#!/bin/sh\necho '%s'\n" % json.dumps(
        {"state": "MERGED", "url": pr, "number": 7, "mergeCommit": {"oid": oid},
         "baseRefName": "main", "headRefOid": oid, "headRefName": "external"}))
    (env.bin / "gh").chmod(0o755)


def _close_terminal_run(con, run_id: str) -> None:
    """The runs row the close transaction leaves behind: terminal."""
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-29T00:00:00+00:00', "
                "terminal_reason='landed externally via https://github.com/o/r/pull/7' WHERE id=?", (run_id,))


def _queued(env, monkeypatch, secs: float = 6.0) -> tuple[dict, Path]:
    """T1 submitted; its run_checks job (the long probe command) queued and not
    started. Runs with OFFICE_JOBS=manual, so nothing kicks by itself."""
    plan, out = _probe_plan(env, secs)
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}] * 3)
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    env.office("dispatch", "T1", check=0)
    from office import jobs
    con = env.con()
    jobs.execute(con, con.execute("SELECT id FROM outbox WHERE kind='launch_agent' AND status='queued'").fetchone()[0])
    job = con.execute("SELECT * FROM outbox WHERE kind='run_checks' AND status='queued'").fetchone()
    assert job is not None
    return dict(job), out


def _claimed(env, monkeypatch, secs: float = 6.0) -> tuple[dict, Path, subprocess.Popen]:
    """`_queued` with the job claimed by a live `office _job` subprocess whose
    check command is running."""
    job, out = _queued(env, monkeypatch, secs)
    proc = _job_process(env, job["id"])
    try:
        assert _until(lambda: _probe_state(out)["now"] == 1), "the check command did not start"
        row = _row(env.con(), job["id"])
        assert row["status"] == "claimed", row  # the `_job` subprocess owns the claim
    except BaseException:
        proc.terminate()
        proc.wait(timeout=30)
        raise
    return job, out, proc


def test_landed_externally_close_cancels_a_claimed_check_job_and_never_restarts_it(env, monkeypatch):
    """The #836 report: while the job's check command (validate.sh, here the
    probe) runs, the PRs merge by hand and the run closes landed-externally.
    The close settles the claimed job, stops the running suite, and no later
    command reclaims the claim or starts the job again."""
    job, out, proc = _claimed(env, monkeypatch, secs=6.0)
    run_id, gate_id = job["run_id"], json.loads(job["payload_json"])["gate_id"]
    env.git("commit", "-qa", "-m", "external PR lands the run's work by hand", "--allow-empty")
    _gh(env, env.git("rev-parse", "HEAD").strip())
    code, closed = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7",
                              "--quote", "PR 7 landed this run's work")
    assert code == 0, closed
    assert "landed externally via" in closed and "closed on the user's word" in closed
    [(kinds, tail)] = re.findall(r"^cancelled \d+ outstanding outbox job\(s\) \(([^)]+)\)(.*)$", closed, flags=re.M)
    assert "run_checks" in kinds and "stopped 1 live job process(es)" in tail, closed

    # every pending and claimed row of the closed run is terminal, right with the close
    assert _pending(env, run_id) == []
    row = _row(env.con(), job["id"])
    assert row["status"] == "cancelled" and row["result_json"] is None and row["error"] is None, row

    # the claim process sees the close and exits without recording a result;
    # the suite it was running was stopped mid-way, not completed
    assert _until(lambda: _check_gone(out, started=True)), f"the running check was not stopped: {_probe_state(out)}"
    proc.wait(timeout=30)
    assert _probe_state(out)["now"] == 1, _probe_state(out)
    row = _row(env.con(), job["id"])
    assert row["status"] == "cancelled" and row["result_json"] is None, row
    gate = _gate(env, gate_id)
    assert gate["status"] == "cancelled" and gate["verdict"] is None, gate  # no verdict for a dead run

    # no later office command reclaims the claim or restarts the job: the close
    # cannot be replayed into work, and a fresh `_job` for the id never claims
    code, retry_out = env.office("close", "--run", run_id[:8], "--landed-externally",
                                 "https://github.com/o/r/pull/7", "--quote", "PR 7 landed this run's work", check=0)
    assert code == 0 and "external landing verified" in retry_out, retry_out
    assert "cancelled" not in retry_out  # the outstanding work was already terminal
    from office import db, jobs
    with db.transaction(env.con()):
        assert jobs.reclaim(env.con(), run_id) == 0  # nothing reclaimable for a terminal run
    again = _job_process(env, job["id"])
    assert again.wait(timeout=30) == 0
    assert _pending(env, run_id) == [] and _row(env.con(), job["id"])["status"] == "cancelled"
    st = _probe_state(out)
    assert st["runs"] == 1 and st["max"] == 1, f"the check suite ran again for a closed run: {st}"
    events = [dict(e) for e in env.con().execute(
        "SELECT * FROM events WHERE run_id=? AND kind='job.cancelled' ORDER BY seq", (run_id,))]
    assert len(events) == 1 and json.loads(events[0]["payload_json"])["job"] == job["id"], events


def test_a_job_attempt_whose_run_is_closed_cancels_itself_and_records_nothing(env, monkeypatch):
    """A start that beat its close back to it: the attempt sees the run is
    terminal as soon as it holds its claim, settles the row as cancelled and
    records nothing — no verdict for a dead run, no check command started."""
    job, out = _queued(env, monkeypatch, secs=2.0)
    gate_id = json.loads(job["payload_json"])["gate_id"]
    _close_terminal_run(env.con(), job["run_id"])
    env.con().commit()
    from office import jobs
    assert jobs.execute(env.con(), job["id"]) == 0
    con = env.con()
    row = _row(con, job["id"])
    assert row["status"] == "cancelled" and row["result_json"] is None and row["claimed_pid"] is None, row
    assert _gate(env, gate_id)["status"] == "queued"  # job_run_checks never began: the gate is untouched
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] != "accepted"
    assert _probe_state(out)["runs"] == 0, f"checks ran for a closed run: {_probe_state(out)}"
    events = [dict(e) for e in con.execute("SELECT * FROM events WHERE run_id=? AND kind='job.cancelled'",
                                           (job["run_id"],))]
    assert len(events) == 1 and "cancelled" in events[0]["summary"], events


def test_a_dead_claim_of_a_closed_run_is_reclaimed_into_a_cancel_not_a_retry(env, monkeypatch):
    """Reclaiming a proven-dead claim on a closed run must not requeue the job
    (a later kick would restart its suite): the claim is cancelled, the gate
    stays untouched, and neither job.retry nor job.failed is recorded."""
    job, out = _queued(env, monkeypatch, secs=2.0)
    run_id, gate_id = job["run_id"], json.loads(job["payload_json"])["gate_id"]
    con = env.con()
    con.execute("UPDATE outbox SET status='claimed', attempts=1, claim_token='Adead', claimed_pid=999999, "
                "claimed_by='host:999999@then', claimed_at='2026-10-07T00:00:00+00:00' WHERE id=?", (job["id"],))
    from office import jobs
    lock = jobs.lock_path(run_id, job["id"])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()  # nothing holds the execution lock, so the claim is proven dead
    _close_terminal_run(con, run_id)
    con.commit()
    assert _pending(env, run_id) == ["claimed"]
    assert jobs.claim_state(_row(env.con(), job["id"]))[0] == "dead"
    from office import db
    with db.transaction(con):
        assert jobs.reclaim(con, run_id) == 1
    con = env.con()
    row = _row(con, job["id"])
    assert row["status"] == "cancelled" and row["result_json"] is None, row
    kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE run_id=? AND kind IN ('job.retry','job.failed')",
                                       (run_id,))]
    assert kinds == [], kinds
    gate = _gate(env, gate_id)
    assert gate["status"] == "queued" and gate["verdict"] is None, gate  # no UNAVAILABLE blocker either
    assert con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='job.cancelled'",
                       (run_id,)).fetchone()[0] == 1


def test_cancel_run_jobs_is_idempotent_and_reports_what_it_cancelled(env, monkeypatch):
    """The closeout helper stops at terminal: a second call for the same closed
    run cancels nothing and reports the empty list."""
    job, out = _queued(env, monkeypatch, secs=1.0)
    _close_terminal_run(env.con(), job["run_id"])
    env.con().commit()
    from office import jobs
    first = jobs.cancel_run_jobs(env.con(), job["run_id"])
    assert first == {"cancelled": 1, "kinds": ["run_checks"], "stopped": []}, first
    second = jobs.cancel_run_jobs(env.con(), job["run_id"])
    assert second["cancelled"] == 0 and second["kinds"] == [] and second["stopped"] == [], second
    assert _pending(env, job["run_id"]) == []
