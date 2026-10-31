"""Measure an executor's session context; rotate only on the orchestrator's decision.

Crossing the threshold records one signal per episode and wakes the orchestrator.
Office never rotates by itself. `office executor-context <task>` records the
orchestrator's choice: `rotate` (durable handoff, then a fresh session in the same
worktree), `continue` (acknowledge this episode), or `threshold` (this task's limit).
A rotation is not a task failure or an accepted submission. The handoff is durable
in runs.db before the old writer is stopped, and the replacement launches only
after that writer is verified gone. Unmeasurable harnesses are never guessed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from office import ctx_alert, db, paths, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_json, dumps, loads, now_iso

DEFAULT_THRESHOLD = 200_000
POLL_SECONDS = 15
SIGNALLED, ACKNOWLEDGED, REQUESTED, RESTARTED, BLOCKED = "signaled", "acknowledged", "requested", "restarted", "blocked"
_EPISODE_PHASES = (SIGNALLED, ACKNOWLEDGED)  # cleared when the session drops below the cap (rearm)
_LIVE_TASK = ("running", "launching")
_MAX_HANDOFF_TEXT = 1500
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _policy(run: dict) -> dict:
    return (run.get("policy") or {}).get("executor_context") or {}


def enabled(run: dict) -> bool:
    return _policy(run).get("enabled", True) is not False


def policy_warning(run: dict) -> str | None:
    """An invalid pinned threshold falls back to the default, and says so."""
    value = _policy(run).get("threshold_tokens", DEFAULT_THRESHOLD)
    if enabled(run) and not (type(value) is int and value >= 0):
        return f"executor_context.threshold_tokens {value!r} is invalid; using {DEFAULT_THRESHOLD:,}"
    return None


def limit(con, run: dict, task_id: str) -> int | None:
    """Effective cap for one task; None means disabled.

    Precedence: `enabled: false` > this task's recorded threshold decision > the
    run's pinned policy.
    """
    if not enabled(run):
        return None
    row = con.execute("SELECT payload_json FROM events WHERE run_id=? AND task_id=? AND kind='executor.threshold' "
                      "ORDER BY seq DESC LIMIT 1", (run["id"], task_id)).fetchone()
    if row:
        value = loads(row[0], {}).get("threshold_tokens")
        return value or None
    value = _policy(run).get("threshold_tokens", DEFAULT_THRESHOLD)
    if type(value) is int and value >= 0:
        return value or None
    return DEFAULT_THRESHOLD


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


def _load(dispatch: dict) -> dict:
    value = loads(dispatch.get("context_handoff_json"), None)
    return value if isinstance(value, dict) else {}


def _handoff(con, run: dict, dispatch: dict, tokens: int | None, cap: int) -> dict:
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
        "phase": REQUESTED, "created_at": now_iso(),
        "from_dispatch_id": dispatch["id"], "task_id": dispatch["task_id"],
        "tokens": tokens, "threshold_tokens": cap,
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


def _current(con, dispatch_id: str):
    """(run, dispatch, task) when this executor is the task's live, unended writer; else None."""
    dispatch = state.get_dispatch(con, dispatch_id)
    if not dispatch or dispatch.get("role") != "executor" or dispatch.get("ended_at"):
        return None
    run = state.get_run(con, dispatch["run_id"])
    if run is None or state.is_terminal(run):
        return None
    task = state.get_task(con, run["id"], dispatch["task_id"])
    if not task or task.get("current_dispatch_id") != dispatch_id or task["status"] not in _LIVE_TASK:
        return None
    return run, dispatch, task


def check(dispatch_id: str) -> bool:
    """Record this episode's signal once. Never rotates. True only when this call signalled."""
    con = db.connect()
    try:
        current = _current(con, dispatch_id)
        if current is None:
            return False
        cap = limit(con, current[0], current[2]["id"])
        if not cap:
            return False
        tokens = session_tokens(current[1])
        if tokens is None:
            return False
        with db.transaction(con):
            current = _current(con, dispatch_id)
            if current is None:
                return False
            run, dispatch, task = current
            handoff = _load(dispatch)
            if tokens < cap:
                if handoff.get("phase") in _EPISODE_PHASES:
                    con.execute("UPDATE dispatches SET context_handoff_json=NULL WHERE id=?", (dispatch_id,))
                return False
            if handoff:  # this episode already has a signal, a decision, or a rotation
                return False
            signal = {"phase": SIGNALLED, "created_at": now_iso(), "from_dispatch_id": dispatch_id,
                      "task_id": task["id"], "tokens": tokens, "threshold_tokens": cap}
            con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(signal), dispatch_id))
            state.emit(con, run, "executor.context_cap",
                       f"{task['id']} executor {dispatch_id} at {tokens:,}/{cap:,} context tokens; choose: "
                       f"office executor-context {task['id']} rotate | continue | threshold <tokens>",
                       task_id=task["id"], dispatch_id=dispatch_id,
                       payload={"tokens": tokens, "threshold": cap})
            return True
    finally:
        con.close()


def monitor(dispatch_id: str, stop: threading.Event) -> None:
    """Supervisor side: covers headless workers and harnesses without hooks."""
    while not stop.wait(POLL_SECONDS):
        try:
            check(dispatch_id)
        except Exception:
            # Monitoring must not abort a worker; the next poll can retry.
            continue


def rotation_requested(dispatch: dict) -> bool:
    """True while the rotation job owns this executor's end; its exit is not a failure."""
    return _load(dispatch).get("phase") == REQUESTED


def _writer(con, run: dict, task_id: str):
    task = state.get_task(con, run["id"], task_id)
    current = _current(con, task["current_dispatch_id"]) if task and task.get("current_dispatch_id") else None
    if current is None:
        raise Refused("no-live-executor", f"{task_id} has no live executor session", scope=task_id)
    return current


def decide(con, run: dict, task_id: str, action: str, value: str | None = None) -> Result:
    """Record the orchestrator's choice for one task's executor context."""
    if state.get_task(con, run["id"], task_id) is None:
        raise Refused("no-task", f"{task_id} is not a task of this run", scope=task_id)
    if action == "threshold":
        return _set_threshold(con, run, task_id, value)
    if action == "continue":
        return _acknowledge(con, run, task_id)
    if action == "rotate":
        return _rotate(con, run, task_id)
    raise Usage("executor-context-action", "action is rotate, continue, or threshold",
                next_step=f"office executor-context {task_id} rotate|continue|threshold <tokens>")


def _set_threshold(con, run: dict, task_id: str, value: str | None) -> Result:
    try:
        tokens = int(value) if value is not None else -1
    except ValueError:
        tokens = -1
    if tokens < 0:
        raise Usage("executor-context-threshold", "threshold needs a whole number of tokens; 0 disables",
                    next_step=f"office executor-context {task_id} threshold <tokens>")
    if not enabled(run):
        raise Refused("context-disabled", "executor_context.enabled is false for this run; set it true in config first",
                      scope=task_id)
    note = " (rotation disabled for this task)" if not tokens else ""
    with db.transaction(con):
        state.emit(con, run, "executor.threshold", f"{task_id} executor context threshold {tokens:,} tokens{note}",
                   task_id=task_id, payload={"threshold_tokens": tokens})
    return _with_next(Result().add(f"{task_id} executor context threshold: {tokens:,} tokens{note}"))


def _with_next(res: Result) -> Result:
    res.next = "office status"
    return res


def _acknowledge(con, run: dict, task_id: str) -> Result:
    with db.transaction(con):
        _, dispatch, _ = _writer(con, run, task_id)
        handoff = _load(dispatch)
        if handoff.get("phase") != SIGNALLED:
            raise Refused("no-pending-signal", f"{task_id} has no pending context signal to continue", scope=task_id)
        handoff.update(phase=ACKNOWLEDGED, acknowledged_at=now_iso())
        con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(handoff), dispatch["id"]))
        state.emit(con, run, "executor.context_ack",
                   f"{task_id} executor {dispatch['id']} kept going at {_count(handoff.get('tokens'))} context tokens; "
                   "not repeated until the next episode", task_id=task_id, dispatch_id=dispatch["id"])
    return _with_next(Result().add(f"{task_id} kept going; signal acknowledged"))


def _rotate(con, run: dict, task_id: str) -> Result:
    cap = limit(con, run, task_id)
    if not cap:
        raise Refused("context-disabled", f"rotation is disabled for {task_id} (threshold 0 or executor_context disabled)",
                      scope=task_id)
    _, dispatch, _ = _writer(con, run, task_id)
    if _load(dispatch).get("phase") == REQUESTED:
        raise Refused("rotation-in-flight", f"{task_id} rotation is already requested", scope=task_id)
    snapshot = _handoff(con, run, dispatch, session_tokens(dispatch), cap)
    did = dispatch["id"]
    with db.transaction(con):
        _, dispatch, _ = _writer(con, run, task_id)
        if _load(dispatch).get("phase") == REQUESTED:
            raise Refused("rotation-in-flight", f"{task_id} rotation is already requested", scope=task_id)
        con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(snapshot), did))
        state.emit(con, run, "executor.context_rotate",
                   f"{task_id} executor {did} rotation requested by orchestrator; fresh session on the same worktree "
                   "after the old one is verified stopped", task_id=task_id, dispatch_id=did,
                   payload={"tokens": snapshot["tokens"], "threshold": cap})
        state.enqueue(con, run, "rotate_executor", {"dispatch_id": did}, dedup_key=f"rotate:{did}", max_attempts=1)
    try:
        path = handoff_path(run, did)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, snapshot, mode=0o600)
    except OSError:
        pass  # the DB row is the handoff; the file is a projection
    from office import jobs
    jobs.kick(con, run["id"])
    return _with_next(Result().add(f"{task_id} rotation requested: {did} -> fresh session after verified stop"))


def job_rotate(con, run: dict, job: dict) -> dict:
    """Stop, verify, and replace; never allow concurrent writers of a worktree."""
    from office import dispatch
    did = job["payload"]["dispatch_id"]
    prior = state.get_dispatch(con, did)
    if not prior or prior["run_id"] != run["id"] or prior["role"] != "executor":
        return {"skipped": "unknown dispatch"}
    handoff = _load(prior)
    if handoff.get("phase") != REQUESTED:
        return {"skipped": "no pending context handoff"}
    task = state.get_task(con, run["id"], prior["task_id"])
    if not task or task["current_dispatch_id"] != did or task["status"] not in _LIVE_TASK:
        return {"skipped": "task has moved"}
    # Even a recorded supervisor exit does not prove its child processes are
    # gone (a detached helper may still hold the worktree). Always verify.
    confirmed, reason = dispatch.stop_worker_tree(run, prior)
    if not confirmed:
        with db.transaction(con):
            state.update_task(con, run["id"], task["id"], status="blocked",
                              pause_reason="executor context handoff blocked: " + reason[:180])
            handoff["phase"], handoff["error"] = BLOCKED, reason[:300]
            con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id=?", (dumps(handoff), did))
            state.emit(con, run, "executor.handoff_blocked",
                       f"{task['id']} context rotation: old executor could not be confirmed stopped; "
                       "replacement NOT launched", task_id=task["id"], dispatch_id=did)
        return {"blocked": reason}
    # If its supervisor has not recorded the end, record it now; _finish
    # deliberately skips the normal failed-worker relaunch for a requested rotation.
    if not state.get_dispatch(con, did).get("ended_at"):
        dispatch._finish(did, None, None, "context_cap", 0, only_if_open=True)
    with db.transaction(con):
        prior = state.get_dispatch(con, did)
        task = state.get_task(con, run["id"], prior["task_id"])
        if task["current_dispatch_id"] != did or task["status"] not in _LIVE_TASK:
            return {"skipped": "task changed during stop"}
        if con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (did,)).fetchone():
            return {"skipped": "executor already submitted"}
        handoff = _load(prior)
        replacement = dispatch._launch_or_block(con, run, task["id"], role="executor",
                                                 extra={"context_handoff": handoff})
        if replacement:
            handoff["phase"], handoff["new_dispatch_id"] = RESTARTED, replacement
            state.emit(con, run, "executor.handoff",
                       f"{task['id']} context handoff {did} -> {replacement} "
                       f"({handoff['tokens']} tokens); fresh session, same worktree",
                       task_id=task["id"], dispatch_id=did,
                       payload={"previous": did, "replacement": replacement,
                                "tokens": handoff["tokens"], "path": str(handoff_path(run, did))})
        else:
            handoff["phase"], handoff["error"] = BLOCKED, "relaunch refused; inspect task blocker"
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
    """Durable status view of each task's latest executor context state.

    An acknowledged episode is deliberately absent: the orchestrator already chose.
    """
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
        if isinstance(handoff, dict) and handoff.get("phase") != ACKNOWLEDGED:
            result.append({k: handoff.get(k) for k in
                           ("task_id", "from_dispatch_id", "new_dispatch_id", "phase",
                            "tokens", "threshold_tokens", "created_at", "error")})
    return result


def _count(value) -> str:
    return f"{value:,}" if type(value) is int else "unmeasured"


def status_lines(run: dict, handoffs: list[dict]) -> list[str]:
    warning = policy_warning(run)
    lines = [warning] if warning else []
    for h in handoffs:
        if h["phase"] == SIGNALLED:
            lines.append(f"executor context: {h['task_id']} at {_count(h['tokens'])}/{_count(h['threshold_tokens'])} "
                         f"tokens; choose: office executor-context {h['task_id']} rotate | continue | threshold <tokens>")
        else:
            lines.append(f"executor handoff: {h['task_id']} {h['phase']} ({_count(h['tokens'])}/"
                         f"{_count(h['threshold_tokens'])} context tokens) {h['from_dispatch_id']} -> "
                         f"{h.get('new_dispatch_id') or '-'}" + (f" BLOCKED: {h['error']}" if h.get("error") else ""))
    return lines
