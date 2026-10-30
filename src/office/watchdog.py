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
# kind -> (env knob, default seconds)
_KNOBS = {
    "executor": ("OFFICE_EXECUTOR_PROGRESS_STALL_S", 600.0),
    "reviewer": ("OFFICE_REVIEW_NO_PROGRESS_S", 900.0),
    "review_wall": ("OFFICE_REVIEW_WALL_S", 3600.0),
    "queued_review": ("OFFICE_REVIEW_QUEUED_S", 900.0),
    "closed_unread": ("OFFICE_REVIEW_UNREAD_S", 900.0),
}
# Gate kinds that are reviews (their queue, closure and verdict need an orchestrator decision).
REVIEW_GATE_KINDS = ("lane", "plan_review", "convergence_review", "visual", "integration")
# Review-gate episode kinds; each is an orchestrator event and a root-wake trigger.
REVIEW_EVENT_KINDS = ("review.queued_too_long", "review.stalled", "review.closed_unread")
# Per-gate cursor consumer `review-read:<gate id>`: the orchestrator was shown that gate's verdict up to this seq.
REVIEW_READ = "review-read"


def threshold(kind: str) -> float:
    key, default = _KNOBS[kind]
    try:
        return max(1.0, float(os.environ.get(key, default)))
    except ValueError:
        return default


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


_SHELLS = {"sh", "bash", "zsh", "dash", "fish"}
MAX_TREE = 512  # processes walked under one agent; a larger tree is unknown, never a stall


def child_state(d: dict) -> bool | None:
    """Whether the executor's agent is waiting on a command it started, such as a background monitor or `office wait`:
    True or False, or None when the process table cannot be read. Unknown is never a stall (#895).

    A shell anywhere under the agent that has a live child of its own is a command in flight: the agent's tools run
    that way, possibly beneath a harness wrapper. Other processes, such as MCP servers, live as long as the session
    and say nothing about the executor. A command that detaches (setsid, a double fork) leaves the agent's tree and is
    not seen. The agent is verified by its recorded start time, so a reused pid never counts; with no verifiable agent
    there is nothing to wait on. The walk is bounded: a tree larger than MAX_TREE is unknown.
    Limits: a shell that wraps a long-lived MCP server, or a command that never ends, keeps the executor exempt. The
    process table does not tell these apart from a monitor."""
    rec = dispatch._recorded_identity({"id": d["run_id"]}, d["id"], "agent") or {}
    root, started = rec.get("pid"), rec.get("c_start")
    if not root or not started:
        return False
    table = dispatch._process_table()
    if table is None:
        return None
    # A zombie has exited and cannot be waiting on anything.
    live = {pid: (ppid, command, began) for pid, ppid, _pgid, state_, began, command in table if not state_.startswith("Z")}
    if root not in live or live[root][2] != started:
        return False
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _command, _began) in live.items():
        kids.setdefault(ppid, []).append(pid)
    seen, frontier = {root}, [root]
    while frontier:
        if len(seen) > MAX_TREE:
            return None
        nxt = []
        for pid in frontier:
            for kid in kids.get(pid, []):
                if kid not in seen:
                    seen.add(kid)
                    if _is_shell(live[kid][1]) and kids.get(kid):
                        return True
                    nxt.append(kid)
        frontier = nxt
    return False


def _is_shell(command: str) -> bool:
    first = (command.split() or [""])[0]
    return first.rsplit("/", 1)[-1].lstrip("-") in _SHELLS


def check(con, run: dict, d: dict, *, role: str, activity: dict | None = None, budget_only: bool = False) -> str | None:
    """Inspect and sample progress; return a reason if the hard threshold passed.

    Sampling can run in `office status` and a separate wait watchdog. There is
    no authority transition and no assumption about the meaning of spinner UI.
    The review wall budget does not need a progress sample, so an unreadable pane
    cannot hide it. `budget_only` (a running self-review subagent) evaluates only
    that budget: the no-progress clock is not reported.
    """
    if d.get("ended_at") or d.get("status") != "running":
        return None
    now = now_iso()
    age = (parse_iso(now) - parse_iso(d["started_at"])).total_seconds() if d.get("started_at") else 0
    if role == "reviewer" and age >= threshold("review_wall"):
        if budget_only:
            return (f"self-review subagent {d['id']} is still running {int(age)}s past review wall budget "
                    f"{int(threshold('review_wall'))}s; next: inspect or stop it before any retry")
        return (f"{d['role']} reviewer {d['id']}: {int(age)}s elapsed past review wall budget "
                f"{int(threshold('review_wall'))}s; preserve the attempt; next: inspect its output, then "
                f"office review {d.get('task_id') or 'plan'} to retry under review policy")
    sample = progress(d, activity)
    if sample is None or budget_only:
        return None
    old = d.get("watchdog_progress_hash")
    since = d.get("watchdog_progress_since")
    if sample != old or not since:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET watchdog_progress_hash=?, watchdog_progress_since=? "
                        "WHERE id=? AND status='running' AND ended_at IS NULL",
                        (sample, now, d["id"]))
        since = now
    seconds = (parse_iso(now) - parse_iso(since)).total_seconds()
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


def reviewer_stall_items(con, run: dict) -> list[tuple[str, str]]:
    """(dispatch id, reason) for each running reviewer past a bound. Reads panes for Herdr launches.

    Reviewers have an independent watchdog: executor state is irrelevant. A running self-review subagent
    owns its no-progress clock, so only its review_wall budget is reported (#895, A1)."""
    from office import rerun, self_review_subagents
    owned = self_review_subagents.running_dispatch_ids(con, run["id"])
    out = []
    for row in con.execute("SELECT * FROM dispatches WHERE run_id=? AND role IN "
                           "('code_reviewer','plan_reviewer','integration_reviewer','visual_reviewer','convergence_reviewer','visual_verifier','browser_verifier','closeout_verifier') "
                           "AND status='running' AND ended_at IS NULL", (run["id"],)).fetchall():
        d = dict(row)
        act = rerun.agent_activity(d) if d.get("launcher") == "herdr" else None
        note = check(con, run, d, role="reviewer", activity=act, budget_only=d["id"] in owned)
        if note:
            out.append((d["id"], note))
    return out


def reviewer_stalls(con, run: dict) -> list[str]:
    return [note for _, note in reviewer_stall_items(con, run)]


def _age(ts: str, now: str) -> float:
    return (parse_iso(now) - parse_iso(ts)).total_seconds()


def review_episodes(con, run: dict, *, probe: bool) -> list[dict]:
    """Review gates past a bound, one dict per episode: `kind` (its event), `episode` (the dedupe key),
    `text` (the blocker line) and `next` (the concrete step). `probe` adds running reviewers, which
    read panes. A review that is running and within its bounds matches nothing.

    queued-too-long: a task review gate still queued past its bound with a live or unknown owner
    (a dead owner is already a stall in guide.stalls). stalled: a running reviewer past its bound.
    closed-unread: a closed review gate whose verdict event no orchestrator read has followed."""
    from office import gates as gates_mod
    now = now_iso()
    marks = ",".join("?" for _ in REVIEW_GATE_KINDS)
    out = []
    for g in con.execute(f"SELECT * FROM gates WHERE run_id=? AND kind IN ({marks}) AND status='queued' "
                         "AND task_id IS NOT NULL ORDER BY created_at", (run["id"], *REVIEW_GATE_KINDS)).fetchall():
        age = _age(g["created_at"], now)
        owner, detail = gates_mod.owner_state(con, run, g)
        if age < threshold("queued_review") or owner not in ("queued", "alive", "unknown"):
            continue
        task = g["task_id"]
        # Revoke only a queue nothing will drain; a live or unknown job is inspected first.
        nxt = (f"office revoke {task}, then office review {task} to relaunch on the same revision" if owner == "queued"
               else f"office status; if the review is confirmed stuck, office revoke {task}, then office review {task} "
                    "on the same revision")
        out.append({"kind": "review.queued_too_long", "episode": g["id"],
                    "text": f"{task} {g['kind']} review {g['id']} queued {int(age)}s past its bound ({detail})",
                    "next": nxt})
    if probe:
        for dispatch_id, note in reviewer_stall_items(con, run):
            # A stall that recovered and stalled again is a new episode: key on the progress it restarted from.
            since = con.execute("SELECT watchdog_progress_since FROM dispatches WHERE id=?",
                                (dispatch_id,)).fetchone()[0]
            _, sep, action = note.partition("; next: ")
            out.append({"kind": "review.stalled", "episode": f"{dispatch_id}:{since}", "text": note,
                        "next": action if sep else "office status"})
    for g in con.execute(f"SELECT * FROM gates WHERE run_id=? AND kind IN ({marks}) AND status='done' "
                         "AND finished_at IS NOT NULL ORDER BY finished_at", (run["id"], *REVIEW_GATE_KINDS)).fetchall():
        age = _age(g["finished_at"], now)
        if age < threshold("closed_unread"):
            continue
        # The verdict is the first verdict-kind event for this gate's task after it closed. It is unread
        # until status has shown this episode (the per-gate read cursor); other events never clear it.
        cursor = con.execute("SELECT last_seq FROM cursors WHERE run_id=? AND consumer=?",
                             (run["id"], f"{REVIEW_READ}:{g['id']}")).fetchone()
        verdict = con.execute("SELECT seq FROM events WHERE run_id=? AND task_id IS ? AND created_at>=? AND seq>? AND "
                              "(kind LIKE 'gate.%' OR kind LIKE 'plan.%' OR kind LIKE 'convergence.%') "
                              "ORDER BY seq LIMIT 1", (run["id"], g["task_id"], g["finished_at"],
                                                       cursor[0] if cursor else 0)).fetchone()
        if verdict is None:
            continue
        scope = g["task_id"] or "plan"
        out.append({"kind": "review.closed_unread", "episode": g["id"], "read_seq": verdict[0],
                    "text": f"{scope} {g['kind']} review {g['id']} closed {int(age)}s ago and its verdict is unread",
                    "next": f"office context to read the {scope} verdict, then act on it"})
    return out


def mark_review_episodes_read(con, run: dict, episodes: list[dict]) -> None:
    """Called once status has shown these episodes: their closed verdicts count as read. Caller holds the transaction."""
    from office import state
    for ep in episodes:
        if ep["kind"] == "review.closed_unread":
            state.advance_cursor(con, run["id"], f"{REVIEW_READ}:{ep['episode']}", ep["read_seq"])


def record_review_episodes(con, run: dict, episodes: list[dict]) -> None:
    """One orchestrator event per episode, deduplicated on its payload key like watchdog.stall.
    Caller holds the transaction."""
    from office import state
    for ep in episodes:
        seen = con.execute("SELECT 1 FROM events WHERE run_id=? AND kind=? "
                           "AND json_extract(payload_json, '$.episode')=? LIMIT 1",
                           (run["id"], ep["kind"], ep["episode"])).fetchone()
        if not seen:
            state.emit(con, run, ep["kind"], f"{ep['text']}; next: {ep['next']}"[:300],
                       audience="orchestrator", payload={"episode": ep["episode"]})
