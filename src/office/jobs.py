"""Durable outbox execution without a daemon.

A transition that needs outside work queues an outbox row in its own
transaction. `kick` starts one short-lived `office _job <id>` process per queued
row; the process claims the row atomically, does the work, and records the
result in a second transaction. A crashed job leaves a claim whose process is
dead; the next `office` command reclaims it. Nothing needs to stay running.

OFFICE_JOBS=inline runs jobs synchronously (tests); OFFICE_JOBS=manual queues
without starting anything.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office import db, frontdoor, paths, state, version
from office.util import dumps, now_iso, pid_alive

KICK_THROTTLE_SECONDS = 10


def _handlers():
    from office import dispatch, gates, plans, prs, visual, integration
    return {
        "pr_sync": prs.job_pr_sync,
        "launch_agent": dispatch.job_launch_agent,
        "notify_worker": dispatch.job_notify_worker,
        "run_checks": gates.job_run_checks,
        "review": gates.job_review,
        "plan_review": plans.job_plan_review,
        "visual_capture": visual.job_capture,
        "visual_review": visual.job_visual_review,
        "integrate": integration.job_integrate,
    }


def mode() -> str:
    return os.environ.get("OFFICE_JOBS", "spawn")


def reclaim(con, run_id: str | None = None) -> int:
    """Requeue claims whose process died. Caller holds the transaction."""
    q = "SELECT id, claimed_pid, attempts, max_attempts FROM outbox WHERE status='claimed'"
    args: tuple = ()
    if run_id:
        q += " AND run_id=?"
        args = (run_id,)
    n = 0
    for row in con.execute(q, args).fetchall():
        if row["claimed_pid"] and pid_alive(row["claimed_pid"]):
            continue
        if row["attempts"] >= row["max_attempts"]:
            con.execute("UPDATE outbox SET status='failed', error=COALESCE(error,'worker process died'), finished_at=? "
                        "WHERE id=?", (now_iso(), row["id"]))
        else:
            con.execute("UPDATE outbox SET status='queued', claimed_pid=NULL, kicked_at=NULL WHERE id=?", (row["id"],))
        n += 1
    return n


def kick(con, run_id: str | None = None) -> int:
    """Start processes for runnable queued jobs. Safe to call from any command."""
    if mode() == "manual":
        return 0
    if mode() == "inline":
        return run_pending(con, run_id)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=KICK_THROTTLE_SECONDS)).isoformat()
    with db.transaction(con):
        reclaim(con, run_id)
        q = ("SELECT id, run_id FROM outbox WHERE status='queued' AND (not_before IS NULL OR not_before<=?) "
             "AND (kicked_at IS NULL OR kicked_at<?)")
        args = [now.isoformat(), cutoff]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        rows = con.execute(q, args).fetchall()
        for row in rows:
            con.execute("UPDATE outbox SET kicked_at=? WHERE id=?", (now.isoformat(), row["id"]))
    for row in rows:
        spawn(row["id"], row["run_id"])
    return len(rows)


def spawn(job_id: str, run_id: str) -> None:
    argv, extra_env = frontdoor.current_argv()
    env = dict(os.environ)
    env.update(extra_env)
    env.pop(frontdoor.HOP_ENV, None)
    log_dir = paths.run_dir(run_id) / "jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"{job_id}.log", "ab")
    try:
        subprocess.Popen(argv + ["_job", job_id], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         env=env, start_new_session=True, close_fds=True)
    finally:
        log.close()


def run_pending(con, run_id: str | None = None, limit: int = 200) -> int:
    """Synchronously drain runnable jobs (inline mode and tests)."""
    done = 0
    for _ in range(limit):
        with db.transaction(con):
            reclaim(con, run_id)
        q = "SELECT id FROM outbox WHERE status='queued' AND (not_before IS NULL OR not_before<=?)"
        args = [now_iso()]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        row = con.execute(q + " ORDER BY created_at LIMIT 1", args).fetchone()
        if row is None:
            break
        execute(con, row["id"])
        done += 1
    return done


def execute(con, job_id: str) -> int:
    """Claim and run one job. Returns a process exit code."""
    with db.transaction(con):
        cur = con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, claimed_at=?, "
                          "attempts=attempts+1 WHERE id=? AND status='queued'",
                          (os.getpid(), f"{os.uname().nodename}:{os.getpid()}", now_iso(), job_id))
        if cur.rowcount == 0:
            return 0
    job = state.get_job(con, job_id)
    run = state.get_run(con, job["run_id"])
    try:
        if not (version.same_line(job["office_version"], run["office_version"])
                and version.same_line(job["payload"].get("office_version"), run["office_version"])):
            raise state.Refused("packet-version-mismatch",
                                f"job {job_id} carries {job['payload'].get('office_version')} but run is pinned to "
                                f"{run['office_version']}")
        if not version.same_line(run["office_version"], version.current()):
            raise state.Refused("runtime-version-mismatch",
                                f"runtime {version.current()} will not execute a job for a run pinned to "
                                f"{run['office_version']}")
        handler = _handlers()[job["kind"]]
        result = handler(con, run, job) or {}
    except Exception as exc:  # recorded, never swallowed
        err = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=8)
        with db.transaction(con):
            row = con.execute("SELECT attempts, max_attempts FROM outbox WHERE id=?", (job_id,)).fetchone()
            permanent = isinstance(exc, state.Refused) or row["attempts"] >= row["max_attempts"]
            if permanent:
                con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, claimed_pid=NULL WHERE id=?",
                            (err[:2000], now_iso(), job_id))
                state.emit(con, run, "job.failed", f"{job['kind']} failed: {err[:160]}",
                           payload={"job": job_id, "traceback": tb})
                _on_permanent_failure(con, run, job, err)
            else:
                backoff = (datetime.now(timezone.utc) + timedelta(seconds=15 * row["attempts"])).isoformat()
                con.execute("UPDATE outbox SET status='queued', error=?, not_before=?, claimed_pid=NULL, kicked_at=NULL "
                            "WHERE id=?", (err[:2000], backoff, job_id))
        sys.stderr.write(tb)
        return 1
    with db.transaction(con):
        con.execute("UPDATE outbox SET status='done', result_json=?, finished_at=?, claimed_pid=NULL WHERE id=?",
                    (dumps(result), now_iso(), job_id))
    state.write_projection(con, run["id"])
    if mode() != "inline":
        kick(con, run["id"])
    return 0


def _on_permanent_failure(con, run: dict, job: dict, err: str) -> None:
    """A job that cannot complete must leave a visible blocker, never a pass."""
    from office import gates
    gate_id = job["payload"].get("gate_id")
    if gate_id:
        gates.mark_unavailable(con, run, gate_id, f"{job['kind']} could not run: {err[:200]}")
    task_id = job["payload"].get("task_id")
    if job["kind"] == "launch_agent" and task_id:
        state.update_task(con, run["id"], task_id, status="blocked", pause_reason=f"launch failed: {err[:200]}")
        state.emit(con, run, "task.blocked", f"{task_id} could not launch: {err[:120]}", task_id=task_id)


def main_job(job_id: str) -> int:
    con = db.connect()
    try:
        return execute(con, job_id)
    finally:
        con.close()
