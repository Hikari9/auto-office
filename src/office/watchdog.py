"""Passive, progress-based stall watchdog for running agents (#701, #722).

Never revokes, relaunches, trusts a verdict or spends a review round. In particular,
a busy spinner is not evidence of progress and missing Herdr telemetry is unknown,
not proof that an agent has stalled. State is durable across `office wait` calls.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from office import db, dispatch
from office.util import now_iso, parse_iso, pid_alive, sha256_obj

EXCLUDED = {".git", ".venv", "node_modules", ".next", "dist", "build", "__pycache__"}
MAX_PATHS = 4000


def threshold(kind: str) -> float:
    defaults = {"executor": 600.0, "reviewer": 900.0, "review_wall": 3600.0}
    key = "OFFICE_REVIEW_WALL_S" if kind == "review_wall" else ("OFFICE_EXECUTOR_PROGRESS_STALL_S" if kind == "executor" else "OFFICE_REVIEW_NO_PROGRESS_S")
    try:
        return max(1.0, float(os.environ.get(key, defaults[kind])))
    except ValueError:
        return defaults[kind]


def file_fingerprint(directory: str | None) -> str | None:
    """A bounded file-stat checksum; an oversized/unreadable tree is unknown."""
    if not directory or not Path(directory).is_dir():
        return None
    root = Path(directory)
    digest = hashlib.sha256()
    count = 0
    try:
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
            for name in sorted(files):
                path = Path(base) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                digest.update(str(path.relative_to(root)).encode())
                digest.update(f"|{info.st_size}|{info.st_mtime_ns}\n".encode())
                count += 1
                if count > MAX_PATHS:
                    return None
    except OSError:
        return None
    return digest.hexdigest()


def progress(d: dict, activity: dict | None = None) -> str | None:
    """Normalize spinner ticks, but preserve substantive text and tree changes."""
    if d.get("launcher") == "herdr":
        if activity is None or activity.get("text") is None or not activity.get("alive"):
            return None
        if activity.get("status") == "blocked":
            return None  # likely a question, a distinct signal
        lines = [dispatch._LIMIT_NOISE.sub("", line).rstrip() for line in activity["text"].splitlines()]
        pane = sha256_obj("\n".join(lines[-80:]))
    elif d.get("launcher") in ("process", "process-fallback"):
        if not d.get("pid") or not pid_alive(d["pid"]):
            return None
        pane = None
    else:
        return None
    wt = file_fingerprint(d.get("worktree"))
    if d.get("worktree") and wt is None:
        return None  # cannot exclude genuine progress in an oversized/unreadable tree
    log = d.get("log_path")
    try:
        info = Path(log).stat() if log else None
        logsig = (info.st_size, info.st_mtime_ns) if info else None
    except OSError:
        logsig = None
    return sha256_obj((pane, wt, logsig))


def child_state(d: dict) -> bool | None:
    """Whether the executor's agent process has a live child or group member, such as a background monitor or
    `office wait`: True or False, or None when the process table cannot be read. Unknown is never a stall (#895).
    A pane agent is verified by its recorded start time, so a reused pid never counts as the agent."""
    if d.get("launcher") == "herdr":
        rec = dispatch._recorded_identity({"id": d["run_id"]}, d["id"], "agent") or {}
        root, started = rec.get("pid"), rec.get("c_start")
        if not started:
            return False  # no verifiable agent, so no child to wait on
    else:
        root, started = d.get("pid"), None
    if not root:
        return False
    table = dispatch._process_table()
    if table is None:
        return None
    # A zombie has exited and cannot be waiting on anything.
    live = {pid: (ppid, pgid, began) for pid, ppid, pgid, state_, began, _ in table if not state_.startswith("Z")}
    if root not in live or (started and live[root][2] != started):
        return False
    return any(pid != root and (ppid == root or pgid == root) for pid, (ppid, pgid, _) in live.items())


def check(con, run: dict, d: dict, *, role: str, activity: dict | None = None) -> str | None:
    """Inspect and sample progress; return a reason if the hard threshold passed.

    Sampling can run in `office status` and a separate wait watchdog. There is
    no authority transition and no assumption about the meaning of spinner UI.
    """
    if d.get("ended_at") or d.get("status") != "running":
        return None
    sample = progress(d, activity)
    if sample is None:
        return None
    now = now_iso()
    old = d.get("watchdog_progress_hash")
    since = d.get("watchdog_progress_since")
    if sample != old or not since:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET watchdog_progress_hash=?, watchdog_progress_since=? "
                        "WHERE id=? AND status='running' AND ended_at IS NULL",
                        (sample, now, d["id"]))
        since = now
    seconds = (parse_iso(now) - parse_iso(since)).total_seconds()
    age = (parse_iso(now) - parse_iso(d["started_at"])).total_seconds() if d.get("started_at") else 0
    if role == "reviewer" and age >= threshold("review_wall"):
        return (f"{d['role']} reviewer {d['id']}: {int(age)}s elapsed past review wall budget "
                f"{int(threshold('review_wall'))}s; preserve the attempt, inspect its output and retry through Office review policy")
    if seconds < threshold(role):
        return None
    if role == "executor" and child_state(d) is not False:
        # A live child (a background monitor, `office wait`) is work in flight: restart the clock.
        with db.transaction(con):
            con.execute("UPDATE dispatches SET watchdog_progress_since=? WHERE id=? AND status='running' "
                        "AND ended_at IS NULL", (now, d["id"]))
        return None
    if role == "executor":
        return (f"{d['task_id']} executor {d['id']}: no pane, worktree or log progress for {int(seconds)}s "
                f"despite a busy agent; next: inspect the pane and worktree, then office revoke {d['task_id']} "
                f"and office rerun {d['task_id']} --fresh if confirmed stuck")
    return (f"{d.get('task_id') or 'plan'} {d['role']} reviewer {d['id']}: no output or worktree "
            f"progress for {int(seconds)}s; next: inspect/stop the attempt, then office review "
            f"{d.get('task_id') or 'plan'} through a valid fallback (no verdict or round was minted)")


def reviewer_stalls(con, run: dict) -> list[str]:
    """Reviewers have an independent watchdog: executor state is irrelevant. A running self-review subagent
    owns its gate, so it is never reported here (#895)."""
    from office import rerun, self_review_subagents
    owned = self_review_subagents.running_dispatch_ids(con, run["id"])
    out = []
    for row in con.execute("SELECT * FROM dispatches WHERE run_id=? AND role IN "
                           "('code_reviewer','plan_reviewer','integration_reviewer','visual_reviewer','convergence_reviewer','visual_verifier','browser_verifier','closeout_verifier') "
                           "AND status='running' AND ended_at IS NULL", (run["id"],)).fetchall():
        d = dict(row)
        if d["id"] in owned:
            continue
        act = rerun.agent_activity(d) if d.get("launcher") == "herdr" else None
        note = check(con, run, d, role="reviewer", activity=act)
        if note:
            out.append(note)
    return out
