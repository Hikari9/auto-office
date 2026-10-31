"""Bound an executor's measured session context; rotate through the fenced outbox.

A context rotation is not a task failure, an accepted submission, or a resume.
The handoff is durable in runs.db before the old writer is stopped. A separate
job verifies that writer has gone before creating a fresh dispatch in its
unchanged worktree. Unsupported/unobservable harnesses are never guessed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from office import ctx_alert, db, paths, state
from office.util import atomic_write_json, dumps, loads, now_iso

DEFAULT_THRESHOLD = 200_000
POLL_SECONDS = 15
_MAX_HANDOFF_TEXT = 1500
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def threshold(run: dict) -> int:
    policy = (run.get("policy") or {}).get("executor_context") or {}
    if policy.get("enabled", True) is False:
        return 0
    value = policy.get("threshold_tokens", DEFAULT_THRESHOLD)
    return value if type(value) is int and value >= 0 else DEFAULT_THRESHOLD


def _codex_tokens(tail: str) -> int | None:
    """Use the last turn's context, never cumulative account/session usage."""
    for line in reversed(tail.splitlines()):
        if '"token_count"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        value = usage.get("input_tokens")
        if type(value) is int and value >= 0:
            return value + max(0, usage.get("output_tokens", 0)) if type(usage.get("output_tokens", 0)) is int else value
    return None


def session_tokens(dispatch: dict) -> int | None:
    """Measured tokens of this *specific dispatch's* recorded harness session."""
    harness, session = dispatch.get("harness"), dispatch.get("session_id")
    if not isinstance(session, str) or not _SAFE_SESSION.fullmatch(session):
        return None
    try:
        if harness == "claude":
            path = ctx_alert._transcript_path(harness, session)
            return ctx_alert.context_tokens(ctx_alert._read_tail(path)) if path else None
        if harness == "codex":
            base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
            matches = list(base.glob("????/??/??/rollout-*" + session + ".jsonl"))
            if len(matches) != 1:
                return None
            path = matches[0].resolve(strict=True)
            path.relative_to(base.resolve())
            return _codex_tokens(ctx_alert._read_tail(path))
    except (OSError, ValueError):
        return None
    return None


def _git(wt: str | None, *args: str) -> str:
    if not wt or not (Path(wt) / ".git").exists():
        return ""
    try:
        result = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=5)
        return result.stdout[:_MAX_HANDOFF_TEXT] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _clip(text: object) -> str:
    return " ".join(str(text or "").split())[:_MAX_HANDOFF_TEXT]


def _handoff(con, run: dict, dispatch: dict, tokens: int, limit: int) -> dict:
    """A truthful, bounded snapshot: don't invent which acceptance steps are done."""
    task = state.get_task(con, run["id"], dispatch["task_id"]) or {}
    wt = dispatch.get("worktree")
    commits = _git(wt, "log", "--format=%h %s", "-6", (dispatch.get("base_commit") or "HEAD") + "..HEAD")
    work = _git(wt, "status", "--short", "--untracked-files=normal")
    diff = _git(wt, "diff", "--stat", "HEAD")
    findings = con.execute(
        "SELECT code, severity, summary, action FROM findings WHERE run_id=? AND task_id=? AND state='open' "
        "ORDER BY created_at LIMIT 20", (run["id"], dispatch["task_id"])).fetchall()
    return {
        "phase": "requested", "created_at": now_iso(),
        "from_dispatch_id": dispatch["id"], "task_id": dispatch["task_id"],
        "tokens": tokens, "threshold_tokens": limit,
        "worktree": wt, "branch": dispatch.get("branch"),
        "state": {
            "task": _clip(task.get("title")), "status": task.get("status"),
            "plan_version": run.get("plan_version"), "contract_version": task.get("contract_version"),
            "head": _git(wt, "rev-parse", "HEAD").strip(),
            "working_tree": work or "(no uncommitted changes reported)",
            "uncommitted_diff_stat": diff or "(none reported)",
        },
        "done": [f"Existing task commits (verify content): {_clip(commits)}"] if commits.strip()
                else ["No completed acceptance criteria independently verified; inspect the preserved worktree."],
        "remaining": [_clip(a) for a in (task.get("accept") or [])[:15]]
                     + ["Inspect committed and uncommitted work against ACCEPT; finish checks and office submit."],
        "open_findings": [{"code": f["code"], "severity": f["severity"],
                           "summary": _clip(f["summary"]), "action": _clip(f["action"])}
                          for f in findings],
    }


def handoff_path(run: dict, dispatch_id: str) -> Path:
    return paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "context-handoff.json"


def check(dispatch_id: str) -> bool:
    """At a tool/turn boundary or supervisor poll, enqueue exactly one rotation.

    No process termination runs in a hook/worker. The outbox job independently
    verifies the previous writer has stopped before launching its replacement.
    """
    con = db.connect()
    queued = False
    try:
        dispatch = state.get_dispatch(con, dispatch_id)
        if not dispatch or dispatch.get("role") != "executor" or dispatch.get("ended_at"):
            return False
        run = state.get_run(con, dispatch["run_id"])
        if run is None or state.is_terminal(run):
            return False
        limit = threshold(run)
        if not limit:
            return False
        task = state.get_task(con, run["id"], dispatch["task_id"])
        if not task or task.get("current_dispatch_id") != dispatch_id or task["status"] not in ("running", "launching"):
            return False
        if dispatch.get("context_handoff_json"):
            return False
        tokens = session_tokens(dispatch)
        if tokens is None or tokens < limit:
            return False
        handoff = _handoff(con, run, dispatch, tokens, limit)
        with db.transaction(con):
            latest = state.get_dispatch(con, dispatch_id)
            current = state.get_task(con, run["id"], dispatch["task_id"])
            if (latest.get("ended_at") or latest.get("context_handoff_json") or
                    current.get("current_dispatch_id") != dispatch_id or
                    current["status"] not in ("running", "launching")):
                return False
            con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?",
                        (dumps(handoff), dispatch_id))
            state.emit(con, run, "executor.context_cap",
                       f"{dispatch['task_id']} executor {dispatch_id} reached {tokens:,}/{limit:,} context tokens; "
                       "preserving work and rotating to a fresh session",
                       task_id=dispatch["task_id"], dispatch_id=dispatch_id,
                       payload={"tokens": tokens, "threshold": limit})
            state.enqueue(con, run, "rotate_executor", {"dispatch_id": dispatch_id},
                          dedup_key=f"rotate:{dispatch_id}", max_attempts=1)
            queued = True
        path = handoff_path(run, dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, handoff, mode=0o600)
    finally:
        con.close()
    if queued:
        from office import jobs
        con = db.connect()
        try:
            jobs.kick(con, run["id"])
        finally:
            con.close()
    return queued


def monitor(dispatch_id: str, stop: threading.Event) -> None:
    """Supervisor side: covers headless workers and harnesses without hooks."""
    while not stop.wait(POLL_SECONDS):
        try:
            if check(dispatch_id):
                return
        except Exception:
            # Monitoring must not abort a worker; the next poll can retry.
            continue


def job_rotate(con, run: dict, job: dict) -> dict:
    """Stop, verify, and replace; never allow concurrent writers of a worktree."""
    from office import dispatch
    did = job["payload"]["dispatch_id"]
    prior = state.get_dispatch(con, did)
    if not prior or prior["run_id"] != run["id"] or prior["role"] != "executor":
        return {"skipped": "unknown dispatch"}
    handoff = loads(prior.get("context_handoff_json"), None)
    if not isinstance(handoff, dict) or handoff.get("phase") != "requested":
        return {"skipped": "no pending context handoff"}
    task = state.get_task(con, run["id"], prior["task_id"])
    if not task or task["current_dispatch_id"] != did or task["status"] not in ("running", "launching"):
        return {"skipped": "task has moved"}
    # Even a recorded supervisor exit does not prove its child processes are
    # gone (a detached helper may still hold the worktree). Always verify.
    confirmed, reason = dispatch.stop_worker_tree(run, prior)
    if not confirmed:
        with db.transaction(con):
            state.update_task(con, run["id"], task["id"], status="blocked",
                              pause_reason="executor context handoff blocked: " + reason[:180])
            handoff["phase"], handoff["error"] = "blocked", reason[:300]
            con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(handoff), did))
            state.emit(con, run, "executor.handoff_blocked",
                       f"{task['id']} context cap: old executor could not be confirmed stopped; "
                       "replacement NOT launched", task_id=task["id"], dispatch_id=did)
        return {"blocked": reason}
    # If its supervisor has not recorded the end, record it now; _finish
    # deliberately skips the normal failed-worker relaunch for handoffs.
    if not state.get_dispatch(con, did).get("ended_at"):
        dispatch._finish(did, None, None, "context_cap", 0, only_if_open=True)
    with db.transaction(con):
        prior = state.get_dispatch(con, did)
        task = state.get_task(con, run["id"], prior["task_id"])
        if task["current_dispatch_id"] != did or task["status"] not in ("running", "launching"):
            return {"skipped": "task changed during stop"}
        if con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (did,)).fetchone():
            return {"skipped": "executor already submitted"}
        handoff = loads(prior["context_handoff_json"], {})
        replacement = dispatch._launch_or_block(con, run, task["id"], role="executor",
                                                 extra={"context_handoff": handoff})
        if replacement:
            handoff["phase"], handoff["new_dispatch_id"] = "restarted", replacement
            state.emit(con, run, "executor.handoff",
                       f"{task['id']} context handoff {did} -> {replacement} "
                       f"({handoff['tokens']:,} tokens); fresh session, same worktree",
                       task_id=task["id"], dispatch_id=did,
                       payload={"previous": did, "replacement": replacement,
                                "tokens": handoff["tokens"], "path": str(handoff_path(run, did))})
        else:
            handoff["phase"], handoff["error"] = "blocked", "relaunch refused; inspect task blocker"
        con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(handoff), did))
    path = handoff_path(run, did)
    # The DB transition already committed. A missing report file must never
    # trigger a second launch; the replacement brief carries the same report.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, handoff, mode=0o600)
    except OSError:
        pass
    return {"previous": did, "replacement": replacement, "path": str(path)}


def recent(con, run: dict) -> list[dict]:
    """Durable status view, not the unread-event cursor."""
    rows = con.execute(
        "SELECT d.id, d.task_id, d.context_handoff_json FROM dispatches d "
        "WHERE d.run_id=? AND d.role='executor' AND d.context_handoff_json IS NOT NULL "
        "ORDER BY d.started_at DESC, d.rowid DESC", (run["id"],)).fetchall()
    seen, result = set(), []
    for row in rows:
        if row["task_id"] in seen:
            continue
        seen.add(row["task_id"])
        handoff = loads(row["context_handoff_json"], {})
        if handoff:
            result.append({k: handoff.get(k) for k in
                           ("task_id", "from_dispatch_id", "new_dispatch_id", "phase",
                            "tokens", "threshold_tokens", "created_at", "error")})
    return result
