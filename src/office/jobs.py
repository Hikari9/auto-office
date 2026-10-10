"""Durable outbox execution without a daemon.

A transition that needs outside work queues an outbox row in its own
transaction. `kick` starts one short-lived `office _job <id>` per queued row.
Nothing needs to stay running.

Ownership (#403, #404). One job id never has two attempts doing its work:

- The execution lock. `office _job <id>` (the supervisor) takes an exclusive
  flock on `jobs/<id>.lock` before anything else and holds it until the
  attempt is settled. The worker it starts, and every check command the worker
  runs, inherit that lock, so the kernel holds it while any of them lives. A
  spawn that finds the lock held exits without touching the row: a replacement
  check suite can never start beside a previous one that is still running.
- The claim token. The worker claims the row (`queued` -> `claimed`) with a
  fresh token. Every transaction the attempt opens first proves, under the
  write lock, that the row is still `claimed` with its token (db.fenced), so an
  attempt that lost ownership can commit no verdict, finding, task status or
  job result. Its result is recorded as a `job.stale_attempt` audit event.
- Liveness is tri-state (alive, dead, unknown). A claim is reclaimed only on
  proof of death: for a token claim, nothing holds its lock; for a claim made
  by an older runtime, the pid is gone or now names another process. Anything
  that cannot be read (a failed `ps`, an agent sandbox, another host) is
  unknown and is never reclaimed.
- Recovery is deterministic. The supervisor records how its worker ended (exit
  code or signal, stderr tail). A worker that died without settling its claim
  is retried once more if attempts remain; otherwise the job fails and its gate
  becomes UNAVAILABLE with a next step (a checks gate blocks its task so
  `office resume` re-runs it). A dead supervisor is found by any later command
  through the free lock, with the last known ownership facts as diagnostics.

OFFICE_JOBS=inline runs jobs synchronously (tests); OFFICE_JOBS=manual queues
without starting anything.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office import db, frontdoor, paths, state, version
from office.util import ALIVE, DEAD, UNKNOWN, claim_identity, claim_liveness, dumps, now_iso, process_start

KICK_THROTTLE_SECONDS = 10
LOCK_FD_ENV = "OFFICE_JOB_LOCK_FD"
# A spawn waits this long for the lock: a liveness probe holds it for an instant.
LOCK_WAIT_SECONDS = 2.0


def _handlers():
    from office import convergence, dispatch, gates, plans, prs, visual, integration, land
    return {
        "converge": convergence.job_converge,
        "convergence_review": convergence.job_convergence_review,
        "lane_visual": convergence.job_lane_visual,
        "pr_sync": prs.job_pr_sync,
        "launch_agent": dispatch.job_launch_agent,
        "trial_recovery": dispatch.job_trial_recovery,
        "notify_worker": dispatch.job_notify_worker,
        "run_checks": gates.job_run_checks,
        "review": gates.job_review,
        "plan_review": plans.job_plan_review,
        "visual_capture": visual.job_capture,
        "visual_review": visual.job_visual_review,
        "integrate": integration.job_integrate,
        "auto_rebase": land.job_auto_rebase,
    }


def mode() -> str:
    return os.environ.get("OFFICE_JOBS", "spawn")


# ------------------------------------------------------------------ the execution lock

def job_dir(run_id: str) -> Path:
    return paths.run_dir(run_id) / "jobs"


def lock_path(run_id: str, job_id: str) -> Path:
    return job_dir(run_id) / f"{job_id}.lock"


def log_path(run_id: str, job_id: str) -> Path:
    return job_dir(run_id) / f"{job_id}.log"


def _children_path(run_id: str, job_id: str) -> Path:
    return job_dir(run_id) / f"{job_id}.children"


def try_lock(run_id: str, job_id: str, *, wait: float = 0.0) -> int | None:
    """An fd holding the job's execution lock, or None when another process holds it."""
    path = lock_path(run_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) or time.time() >= deadline:
                os.close(fd)
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    return None
                raise
        time.sleep(0.1)


def _lock_state(run_id: str, job_id: str) -> tuple[str, str]:
    try:
        fd = os.open(lock_path(run_id, job_id), os.O_RDONLY)
    except FileNotFoundError:
        return UNKNOWN, "its execution lock file is missing"
    except OSError as exc:
        return UNKNOWN, f"its execution lock cannot be opened ({exc.strerror})"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return ALIVE, "a live process holds its execution lock"
        return UNKNOWN, f"its execution lock cannot be probed ({exc.strerror})"
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return DEAD, "no process holds its execution lock (worker, supervisor and checks are gone)"
    finally:
        os.close(fd)


def claim_state(row) -> tuple[str, str]:
    """(ALIVE | DEAD | UNKNOWN, why) for the owner of a claimed outbox row."""
    row = dict(row)
    if row.get("claim_token"):
        return _lock_state(row["run_id"], row["id"])
    if not row.get("claimed_pid"):
        return DEAD, "the claim names no process"
    return claim_liveness(row["claimed_pid"], row.get("claimed_by"))


def claim_live(row) -> bool:
    """Anything but proof of death: the claim keeps its job."""
    return claim_state(row)[0] != DEAD


# ------------------------------------------------------------------ the current attempt

# (run id, job id, claim token, lock fd) of the attempt this process runs, innermost last.
_ATTEMPTS: list[tuple[str, str, str, int | None]] = []


def attempt_lock_fds() -> tuple[int, ...]:
    """The execution lock a child process of this attempt must inherit, so the
    lock outlives the worker for as long as its check commands run."""
    return tuple(fd for *_, fd in _ATTEMPTS[-1:] if fd is not None)


def register_child(pid: int) -> None:
    """Record a check command's process group, so a supervisor whose worker died
    stops exactly that group (identified by pid and start time) and nothing else."""
    if not _ATTEMPTS:
        return
    run_id, job_id, token, _ = _ATTEMPTS[-1]
    with _children_path(run_id, job_id).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pgid": pid, "start": process_start(pid), "attempt": token}) + "\n")


@contextlib.contextmanager
def _attempt(run_id: str, job_id: str, token: str, lock_fd: int | None):
    _ATTEMPTS.append((run_id, job_id, token, lock_fd))
    try:
        with db.fenced(job_id, token):
            yield
    finally:
        _ATTEMPTS.pop()


# ------------------------------------------------------------------ recovery

def _note(run_id: str, job_id: str, text: str) -> None:
    """Append one line to the job log; diagnostics never fail the caller."""
    try:
        path = log_path(run_id, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} office: {text}\n")
    except OSError:
        pass


def _log_tail(run_id: str, job_id: str, since: int = 0, limit: int = 1500) -> str:
    try:
        with log_path(run_id, job_id).open("rb") as fh:
            fh.seek(max(since, 0))
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")[-limit:].strip()


def recover(con, row, reason: str, diag: dict) -> str:
    """Settle a claim whose owner is proven dead: retry it once more while
    attempts remain, else fail the job and make its gate unavailable. Applies
    only while the row still holds that claim. Caller holds the tx."""
    row = dict(row)
    cur = con.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
    if cur is None or cur["status"] != "claimed" or cur["claim_token"] != row.get("claim_token"):
        return "settled"
    run = state.get_run(con, cur["run_id"])
    job = state.get_job(con, cur["id"])
    if cur["attempts"] >= cur["max_attempts"]:
        con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, claimed_pid=NULL WHERE id=?",
                    (reason[:2000], now_iso(), cur["id"]))
        state.emit(con, run, "job.failed", f"{cur['kind']} failed: {reason[:160]}", payload={"job": cur["id"], **diag})
        on_permanent_failure(con, run, job, reason)
        outcome = "failed"
    else:
        con.execute("UPDATE outbox SET status='queued', error=?, claimed_pid=NULL, claim_token=NULL, kicked_at=NULL "
                    "WHERE id=?", (reason[:2000], cur["id"]))
        state.emit(con, run, "job.retry", f"{cur['kind']} job {cur['id']} retried ({cur['attempts']}/"
                   f"{cur['max_attempts']} attempts used): {reason[:160]}", audience="runtime",
                   payload={"job": cur["id"], **diag})
        outcome = "requeued"
    _note(cur["run_id"], cur["id"], f"{outcome}: {reason} | " + dumps(diag))
    return outcome


def reclaim(con, run_id: str | None = None) -> int:
    """Recover claims whose owner is proven dead. A live or unknown owner keeps
    its claim. Caller holds the transaction."""
    q = "SELECT * FROM outbox WHERE status='claimed'"
    args: tuple = ()
    if run_id:
        q += " AND run_id=?"
        args = (run_id,)
    n = 0
    for row in con.execute(q, args).fetchall():
        liveness, why = claim_state(row)
        if liveness != DEAD:
            continue
        diag = {"claim": row["claim_token"], "claimed_by": row["claimed_by"], "pid": row["claimed_pid"],
                "claimed_at": row["claimed_at"], "attempts": row["attempts"], "liveness": why,
                "exit": "not observed: its supervisor is gone too, so no process saw how it ended",
                "log_tail": _log_tail(row["run_id"], row["id"])[-600:] or "(empty)"}
        recover(con, row, f"worker process died: {why}", diag)
        n += 1
    return n


def on_permanent_failure(con, run: dict, job: dict, err: str) -> None:
    """A job that cannot complete must leave a visible blocker, never a pass."""
    from office import gates
    gate_id = job["payload"].get("gate_id")
    if gate_id and job["kind"] == "run_checks":
        # Through the gate's own ingestion, so the task is blocked with the
        # reason `office resume` recognizes and re-runs (#404).
        gates.ingest_task_gate(con, run, gate_id, {
            "verdict": "UNAVAILABLE",
            "summary": f"checks could not run ({err[:200]}); office resume re-runs them"})
    elif gate_id:
        gates.mark_unavailable(con, run, gate_id, f"{job['kind']} could not run: {err[:200]}")
    task_id = job["payload"].get("task_id")
    if job["kind"] == "trial_recovery":
        from office import dispatch
        dispatch.trial_recovery_failed(con, run, job, err)
        return
    if job["kind"] == "launch_agent" and task_id:
        from office import dispatch
        if dispatch.trial_launch_failed(con, run, job["payload"].get("dispatch_id"), f"launch failed: {err}"):
            return  # a trial's failed launch is recovered (fallback, or a blocker with a next step), not just blocked
        state.update_task(con, run["id"], task_id, status="blocked", pause_reason=f"launch failed: {err[:200]}")
        state.emit(con, run, "task.blocked", f"{task_id} could not launch: {err[:120]}", task_id=task_id)


# ------------------------------------------------------------------ starting jobs

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
    env.pop(LOCK_FD_ENV, None)
    path = log_path(run_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, "ab")
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


# ------------------------------------------------------------------ one attempt

def new_token() -> str:
    return "A" + uuid.uuid4().hex[:12]


def execute(con, job_id: str, *, token: str | None = None, lock_fd: int | None = None) -> int:
    """Claim and run one job as one fenced attempt. Returns a process exit code.
    Without `lock_fd` (inline mode, tests) the attempt takes the lock itself."""
    row = con.execute("SELECT run_id, status FROM outbox WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "queued":
        return 0
    own = lock_fd is None
    if own:
        lock_fd = try_lock(row["run_id"], job_id, wait=LOCK_WAIT_SECONDS)
        if lock_fd is None:
            _note(row["run_id"], job_id, "not started: an earlier attempt's processes still hold the execution lock")
            return 0
    token = token or new_token()
    try:
        claimant = claim_identity(os.getpid())  # the start time tells this process from a later one with its pid
        with db.transaction(con):
            cur = con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, claimed_at=?, claim_token=?, "
                              "attempts=attempts+1 WHERE id=? AND status='queued'",
                              (os.getpid(), claimant, now_iso(), token, job_id))
            if cur.rowcount == 0:
                return 0
        with _attempt(row["run_id"], job_id, token, lock_fd):
            return _run_attempt(con, job_id, token)
    finally:
        if own:
            os.close(lock_fd)


def _run_attempt(con, job_id: str, token: str) -> int:
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
        with db.transaction(con):
            con.execute("UPDATE outbox SET status='done', result_json=?, finished_at=?, claimed_pid=NULL WHERE id=? "
                        "AND claim_token=?", (dumps(result), now_iso(), job_id, token))
    except db.StaleAttempt as exc:
        _stale(con, run, job, token, str(exc))
        return 1
    except Exception as exc:  # recorded, never swallowed
        err = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=8)
        try:
            with db.transaction(con):
                row = con.execute("SELECT attempts, max_attempts FROM outbox WHERE id=?", (job_id,)).fetchone()
                permanent = isinstance(exc, state.Refused) or row["attempts"] >= row["max_attempts"]
                if permanent:
                    con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, claimed_pid=NULL WHERE id=?",
                                (err[:2000], now_iso(), job_id))
                    state.emit(con, run, "job.failed", f"{job['kind']} failed: {err[:160]}",
                               payload={"job": job_id, "claim": token, "traceback": tb})
                    on_permanent_failure(con, run, job, err)
                else:
                    backoff = (datetime.now(timezone.utc) + timedelta(seconds=15 * row["attempts"])).isoformat()
                    con.execute("UPDATE outbox SET status='queued', error=?, not_before=?, claimed_pid=NULL, "
                                "claim_token=NULL, kicked_at=NULL WHERE id=?", (err[:2000], backoff, job_id))
        except db.StaleAttempt as stale:
            _stale(con, run, job, token, f"{stale} (its own failure was {err[:200]})")
        sys.stderr.write(tb)
        if mode() != "inline":
            with db.unfenced():
                kick(con, run["id"])  # a failure may have queued its own recovery
        return 1
    state.write_projection(con, run["id"])
    if mode() != "inline":
        with db.unfenced():
            kick(con, run["id"])
    return 0


def _stale(con, run: dict, job: dict, token: str, why: str) -> None:
    """An attempt that lost its claim: its result is audit evidence only."""
    with db.unfenced():
        with db.transaction(con):
            state.emit(con, run, "job.stale_attempt", f"{job['kind']} job {job['id']} attempt {token} lost its claim; "
                       f"its result was discarded: {why[:200]}", audience="runtime",
                       payload={"job": job["id"], "claim": token, "why": why})
    _note(run["id"], job["id"], f"attempt {token} discarded: {why}")
    sys.stderr.write(f"office: {why}\n")


# ------------------------------------------------------------------ supervisor and worker

def main_job(job_id: str, attempt: str | None = None) -> int:
    if attempt:
        return _worker(job_id, attempt)
    return _supervise(job_id)


def _worker(job_id: str, token: str) -> int:
    fd = os.environ.get(LOCK_FD_ENV)
    lock_fd = int(fd) if fd and fd.isdigit() else None
    con = db.connect()
    try:
        if lock_fd is None:
            return execute(con, job_id, token=token)
        return execute(con, job_id, token=token, lock_fd=lock_fd)
    finally:
        con.close()


def _describe_exit(code: int) -> str:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"killed by {name}"
    return f"exited with code {code}"


def _supervise(job_id: str) -> int:
    """Hold the job's execution lock, run one worker attempt under it, and
    settle a claim the worker left behind when it died (#404)."""
    con = db.connect()
    try:
        row = con.execute("SELECT run_id, status FROM outbox WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != "queued":
            return 0
        run_id = row["run_id"]
        lock_fd = try_lock(run_id, job_id, wait=LOCK_WAIT_SECONDS)
        if lock_fd is None:
            _note(run_id, job_id, f"supervisor {os.getpid()} not started: an earlier attempt's processes still hold "
                                  "the execution lock; the job stays queued")
            return 0
        try:
            return _supervise_locked(con, run_id, job_id, lock_fd)
        finally:
            os.close(lock_fd)
    finally:
        con.close()


def _supervise_locked(con, run_id: str, job_id: str, lock_fd: int) -> int:
    token = new_token()
    argv, extra_env = frontdoor.current_argv()
    env = dict(os.environ)
    env.update(extra_env)
    env.pop(frontdoor.HOP_ENV, None)
    env[LOCK_FD_ENV] = str(lock_fd)
    os.set_inheritable(lock_fd, True)
    log = log_path(run_id, job_id)
    offset = log.stat().st_size if log.exists() else 0
    started = now_iso()
    _note(run_id, job_id, f"attempt {token}: supervisor pid {os.getpid()} starts a worker")
    try:
        proc = subprocess.Popen(argv + ["_job", job_id, "--attempt", token], stdin=subprocess.DEVNULL, env=env,
                                pass_fds=(lock_fd,))
    except OSError as exc:
        _note(run_id, job_id, f"attempt {token}: the worker could not start: {exc}")
        return 1
    code = proc.wait()
    how = _describe_exit(code)
    _note(run_id, job_id, f"attempt {token}: worker pid {proc.pid} {how}")
    # A worker that ended normally settled its claim (or never claimed). One
    # that still holds it died mid-attempt, whatever its exit code says (an
    # uncaught crash also exits 1): settle it now, while the lock still says
    # no other attempt can run.
    row = con.execute("SELECT status, claim_token FROM outbox WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "claimed" or row["claim_token"] != token:
        return code
    stopped = _stop_children(run_id, job_id, token)
    with db.transaction(con):
        row = con.execute("SELECT * FROM outbox WHERE id=?", (job_id,)).fetchone()
        if row is not None and row["status"] == "claimed" and row["claim_token"] == token:
            diag = {"claim": token, "pid": proc.pid, "claimed_by": row["claimed_by"], "started_at": started,
                    "exit": how, "exit_code": code, "children_stopped": stopped,
                    "stderr_tail": _log_tail(run_id, job_id, offset)[-1200:] or "(the worker wrote nothing)"}
            recover(con, row, f"worker process died ({how})", diag)
    return code or 1


def _stop_children(run_id: str, job_id: str, token: str) -> list[int]:
    """Stop the check process groups this attempt's dead worker left running.
    Only a group whose leader still has its recorded start time is signalled."""
    from office.util import process_is
    stopped = []
    try:
        lines = _children_path(run_id, job_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return stopped
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        pgid = rec.get("pgid")
        if rec.get("attempt") != token or not process_is(pgid, rec.get("start")):
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            stopped.append(pgid)
        except OSError:
            continue
    return stopped
