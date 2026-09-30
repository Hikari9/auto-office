"""The orchestrator's choice after a worker ends: resume that session or start fresh.

Findings never start an executor by themselves (R8). The orchestrator decides:
`office rerun <task> --resume` continues the ended session through the harness's
native resume, and `office rerun <task> --fresh` starts a new session with the
open findings in its brief. `office dismiss` closes the panes of ended dispatches.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from office import adapters, db, dispatch, gates, jobs, state
from office.result import Result
from office.state import Refused, Usage
from office.util import pid_alive


def _fresh_cmd(tid: str) -> str:
    return f"office rerun {tid} --fresh"


def _last_ended_executor(con, run: dict, tid: str) -> dict | None:
    row = con.execute("SELECT id FROM dispatches WHERE run_id=? AND task_id=? AND role='executor' AND ended_at IS NOT NULL "
                      "ORDER BY ended_at DESC, started_at DESC LIMIT 1", (run["id"], tid)).fetchone()
    return state.get_dispatch(con, row["id"]) if row else None


def agent_alive(d: dict) -> bool | None:
    """True, False, or None when liveness cannot be known (herdr unreachable).
    A pane-hosted agent is asked through herdr; a headless one by its pid."""
    if d.get("launcher") != "herdr":
        return bool(d.get("pid") and pid_alive(d["pid"]))
    name = dispatch.herdr_agent_name(d["id"])
    try:
        proc = subprocess.run(["herdr", "agent", "get", name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        try:
            agent = (json.loads(proc.stdout or "{}").get("result") or {}).get("agent")
        except ValueError:
            return None
        return bool(agent)
    body = (proc.stdout or "") + (proc.stderr or "")
    return False if "not_found" in body else None


def _set_resumed_from(con, dispatch_id: str, parent: str) -> None:
    try:
        con.execute("UPDATE dispatches SET resumed_from=? WHERE id=?", (parent, dispatch_id))
    except sqlite3.OperationalError:
        # An older schema without the column: keep the link where inspect finds it.
        row = con.execute("SELECT override_json FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        data = json.loads((row["override_json"] if row else None) or "{}")
        data["resumed_from"] = parent
        con.execute("UPDATE dispatches SET override_json=? WHERE id=?", (json.dumps(data), dispatch_id))


def _findings_text(con, run: dict, tid: str) -> str:
    rows = con.execute("SELECT code, location, summary FROM findings WHERE run_id=? AND task_id=? AND state='open' "
                       "ORDER BY created_at", (run["id"], tid)).fetchall()
    return "; ".join(f"{r['code']} {r['location'] or ''} {r['summary'][:120]}".strip() for r in rows[:8])


def rerun(con, run: dict, tid: str, *, resume: bool, fresh: bool) -> Result:
    if resume == fresh:
        raise Usage("rerun-mode", f"say how to rerun {tid}: --resume continues the ended session with its context "
                    "(native harness resume); --fresh starts a new session with the open findings in its brief",
                    next_step=f"office rerun {tid} --resume | {_fresh_cmd(tid)}")
    task = state.get_task(con, run["id"], tid)
    if task is None:
        raise Usage("unknown-task", f"no task {tid}")
    if task["status"] in ("accepted", "cancelled"):
        raise Refused("task-done", f"{tid} is {task['status']}; nothing to rerun", scope=tid)
    if gates.worker_live(con, task.get("current_dispatch_id")):
        raise Refused("worker-live", f"{tid} still has a live worker ({task['current_dispatch_id']})", scope=tid,
                      next_step=f"office revoke {tid}, or wait for it to end")
    parent = _last_ended_executor(con, run, tid)
    if parent is None:
        raise Refused("no-ended-executor", f"{tid} has no ended executor session to rerun", scope=tid,
                      next_step=f"office dispatch {tid}")
    extra = None
    if resume:
        why = None
        session = parent.get("session_id")
        adapter = adapters.load_all().get(parent.get("adapter_id") or parent.get("harness") or "")
        argv = None
        if not session:
            why = f"{parent['id']} has no stored harness session id"
        elif adapter is None:
            why = f"no adapter {parent.get('adapter_id') or parent.get('harness')} for {parent['id']}"
        else:
            argv = adapters.resume_argv(adapter, "worker", session_id=session, model=parent.get("model") or "",
                                        effort=parent.get("effort") or "", cwd=Path(parent.get("worktree") or "."))
            if argv is None:
                why = f"adapter {adapter.get('id')} declares no resume form"
        if why is None:
            alive = agent_alive(parent)
            if alive is None:
                why = f"cannot tell whether {parent['id']}'s agent is still running (herdr unreachable)"
            elif alive:
                why = f"{parent['id']}'s agent is still running; resuming it would start a second writer"
        if why:
            raise Refused("resume-impossible", f"cannot resume {tid}: {why}", scope=tid, preserved="the task worktree",
                          next_step=_fresh_cmd(tid))
        found = _findings_text(con, run, tid)
        extra = {"resume": {"parent": parent["id"], "session_id": session, "argv": argv[0], "herdr_kind": argv[1],
                            "findings": found}}
    with db.transaction(con):
        did = dispatch.request_launch(con, run, tid, role="executor", fix_of=task.get("current_revision_id"),
                                      extra=extra)
        if resume:
            _set_resumed_from(con, did, parent["id"])
        state.emit(con, run, "task.rerun", f"{tid} rerun {'--resume from ' + parent['id'] if resume else '--fresh'} "
                   f"as {did}", task_id=tid, dispatch_id=did)
    jobs.kick(con, run["id"])
    res = Result(lines=[f"{tid} -> {did} executor {'resuming ' + parent['id'] if resume else 'fresh session'} launching"])
    res.lines.extend(f"  {line}" for line in dispatch.launch_instructions(run, state.get_dispatch(con, did)))
    res.next = "exceptions only; office status"
    return res


def _reclaim():
    fn = getattr(dispatch, "reclaim_pane", None)
    if fn is None:
        raise Refused("reclaim-unavailable", "this runtime has no pane reclaim; close the pane by hand")
    return fn


def dismiss(con, run: dict, target: str | None, *, all_: bool = False) -> Result:
    if not all_ and not target:
        raise Usage("dismiss-target", "name what to dismiss", next_step="office dismiss <task|dispatch|--all>")
    q = "SELECT id FROM dispatches WHERE run_id=? AND launcher='herdr' AND pane_id IS NOT NULL"
    args: tuple = (run["id"],)
    single = bool(target and target.upper().startswith("D"))
    if single:
        q += " AND id=?"
        args += (target[0].upper() + target[1:],)
    elif target:
        q += " AND task_id=?"
        args += (target.upper(),)
    rows = [state.get_dispatch(con, r["id"]) for r in con.execute(q + " ORDER BY started_at", args).fetchall()]
    if single and not rows:
        raise Refused("no-pane", f"{target} has no Office pane", scope=target)
    live = [d["id"] for d in rows if d["status"] in ("launching", "running") and not d.get("ended_at")]
    if single and live:
        raise Refused("dispatch-live", f"{target} is still running; office revoke its task first", scope=target)
    reclaim = _reclaim()
    lines = []
    for d in rows:
        if d["id"] in live:
            continue
        lines.append(f"{d['id']} pane {d['pane_id']}: {reclaim(run, d['id'], explicit=True)}")
    if live:
        lines.append(f"left running: {', '.join(live)}")
    return Result(lines=lines or ["no ended dispatch panes to dismiss"], next="exceptions only; office status")


def reclaim_all(run: dict) -> None:
    """Snapshot then close every Office pane of the run (close, abandon)."""
    fn = getattr(dispatch, "reclaim_pane", None)
    if fn is None:
        return
    con = db.connect()
    try:
        ids = [r["id"] for r in con.execute("SELECT id FROM dispatches WHERE run_id=? AND launcher='herdr' "
                                            "AND pane_id IS NOT NULL", (run["id"],)).fetchall()]
    finally:
        con.close()
    for did in ids:
        try:
            fn(run, did, explicit=True)
        except Exception:  # noqa: BLE001 - closing a run never fails on one pane
            pass
