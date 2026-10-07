"""The orchestrator's choice after a worker ends: resume that session or start fresh.

Findings never start an executor by themselves (R8). The orchestrator decides:
`office rerun <task> --resume` continues the ended session through the harness's
native resume, and `office rerun <task> --fresh` starts a new session with the
open findings in its brief. `office dismiss` closes the panes of ended dispatches.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from office import adapters, candidates, contract, db, dispatch, gates, jobs, paths, state
from office.result import Result
from office.state import Refused, Usage
from office.util import pid_alive, sha256_obj


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


def agent_activity(d: dict) -> dict | None:
    """What a running agent is doing now, or None when that cannot be known
    (herdr unreachable or its pane unreadable): {"alive", "busy", "hash", "text", "status"}
    (`status` is herdr's agent status; `blocked` means it sees an approval or question UI).
    A headless process has no idle state: it is busy while its pid lives.
    A pane-hosted agent is busy when herdr reports `working` or the pane shows a
    turn in progress (agy reports idle mid-turn, so its pane decides); `hash`
    is the pane content, which changes while the agent is doing anything.
    herdr reports a codex pane `working` long after its turn ended, so for codex
    only the pane markers and the hash decide."""
    alive = agent_alive(d)
    if alive is None:
        return None
    if not alive or d.get("launcher") != "herdr":
        return {"alive": alive, "busy": alive, "hash": None, "text": None}
    name = dispatch.herdr_agent_name(d["id"])
    text = dispatch._herdr_agent_text(name)
    if text is None:
        return None
    try:
        proc = subprocess.run(["herdr", "agent", "get", name], capture_output=True, text=True, timeout=30)
        agent = (json.loads(proc.stdout or "{}").get("result") or {}).get("agent") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    status = agent.get("status") or agent.get("agent_status")
    working = status == "working" and (d.get("adapter_id") or d.get("harness")) != "codex"
    return {"alive": True, "busy": working or dispatch._pane_busy(text), "hash": sha256_obj(text),
            "text": text, "status": status}


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
    rows = con.execute("SELECT code, location, summary FROM findings WHERE run_id=? AND task_id=? AND " + contract.TASK_WORK_FINDINGS + " "
                       "ORDER BY created_at", (run["id"], tid)).fetchall()
    return "; ".join(f"{r['code']} {r['location'] or ''} {r['summary'][:120]}".strip() for r in rows[:8])


def _restack(con, run: dict, task: dict, worktree: str | None) -> dict | None:
    """Bring the task worktree up to its dependencies' accepted revisions.

    A task built on a dependency revision that review later superseded can
    never be accepted (gates.stale_dependency). Office merges each accepted
    revision the worktree lacks; a merge is never a rebase, so the pushed
    branch needs no force-push. On a conflict the merge is aborted and the
    executor is told to make it. Returns {"base", "merged", "conflict", "line"}."""
    if not worktree or not (Path(worktree) / ".git").exists():
        return None
    wt = Path(worktree)
    pending = []
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep)
        if not dt or dt["status"] != "accepted" or not dt.get("accepted_revision_id"):
            continue
        row = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (dt["accepted_revision_id"],)).fetchone()
        if row and not gates._is_ancestor(run, row["commit_sha"], paths.git(wt, "rev-parse", "HEAD")):
            pending.append((dep, dt["accepted_revision_id"], row["commit_sha"]))
    if not pending:
        return None
    env = {**os.environ, **paths.commit_identity_env(wt)}
    merged = []
    for dep, rev_id, sha in pending:
        proc = subprocess.run(["git", "-C", str(wt), "merge", "--no-edit", "-m",
                               f"office: restack {task['id']} onto {dep} {rev_id}\n\n{paths.office_trailer(run['id'])}", sha],
                              capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True)
            return {"base": None, "merged": merged, "conflict": {"task": dep, "revision": rev_id, "commit": sha},
                    "line": f"restack onto {dep} {rev_id} conflicts; the executor merges {sha[:7]} first"}
        merged.append({"task": dep, "revision": rev_id, "commit": sha})
    base = merged[-1]["commit"] if len(task["depends"]) == 1 else None
    return {"base": base, "merged": merged, "conflict": None,
            "line": "restacked onto " + ", ".join(f"{m['task']} {m['revision']}" for m in merged)}


def _sticky_check(con, run: dict, task: dict, parent: dict) -> str | None:
    """Why the original route may not run again now, from fresh evidence, or None.

    A rerun keeps its route (#300); this only reads live quota, trust and learned
    eligibility for that exact route. A route the fresh decision never saw
    (a probe failure, a deduplicated alias) is not refused here."""
    route = parent.get("route") or {}
    if route.get("override") or parent.get("role") != "executor":
        return None
    try:
        fresh = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id=task["id"],
                                      dispatch_kind="fix")
    except Exception:  # noqa: BLE001 - the check never blocks a rerun on its own failure
        return None
    from office import scoring
    triple = scoring.normalize_triple(parent.get("triple") or "")
    for r in fresh.get("rejected") or []:
        # Only evidence that changes while a run lives: trust/quarantine (2), quota (6),
        # learned eligibility (7). Unknown quota is a comparison, not proof the route cannot run.
        if r.get("stage") not in (1, 2, 6, 7) or "quota unknown" in r["reason"]:
            continue
        if scoring.normalize_triple(r["candidate"]) == triple:
            return r["reason"]
    return None


def rerun(con, run: dict, tid: str, *, resume: bool, fresh: bool, reroute: bool = False) -> Result:
    if resume == fresh:
        raise Usage("rerun-mode", f"say how to rerun {tid}: --resume continues the ended session with its context "
                    "(native harness resume); --fresh starts a new session with the open findings in its brief",
                    next_step=f"office rerun {tid} --resume | {_fresh_cmd(tid)}")
    if reroute and resume:
        raise Usage("rerun-mode", "--reroute starts a new session on another route; a resumed session keeps its own",
                    next_step=f"{_fresh_cmd(tid)} --reroute")
    task = state.get_task(con, run["id"], tid)
    if task is None:
        raise Usage("unknown-task", f"no task {tid}")
    if task["status"] in ("accepted", "cancelled"):
        raise Refused("task-done", f"{tid} is {task['status']}; nothing to rerun", scope=tid)
    if gates.worker_live(con, task.get("current_dispatch_id")):
        raise Refused("worker-live", f"{tid} still has a live worker ({task['current_dispatch_id']})", scope=tid,
                      next_step=f'office prompt {tid} -- "<message>" to reach it (an amendment already tells it to '
                                f"resubmit), or office revoke {tid} to end it first")
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
            why = (f"{parent['id']} has no stored harness session id (it ran headless, so there is no session to "
                   f"continue; a fresh session starts from the preserved worktree and the current contract)")
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
    decision = None
    if reroute:
        decision = dispatch.planned_route(con, run, task, reroute=True)
        if decision.get("status") != "selected":
            raise Refused("no-route", dispatch._route_failure(tid, decision), scope=tid,
                          next_step=dispatch._route_next(decision, tid, run))
    else:
        blocked = _sticky_check(con, run, task, parent)
        if blocked:
            raise Refused("route-unavailable", f"{tid}'s original route {parent.get('triple')} cannot run now: {blocked}",
                          scope=tid, preserved="the task worktree and its route history",
                          next_step=f"{_fresh_cmd(tid)} --reroute routes from current evidence")
    restack = _restack(con, run, task, parent.get("worktree"))
    if restack:
        extra = {**(extra or {}), "restack": {k: restack[k] for k in ("merged", "conflict")}}
        if resume:
            # A resumed session reads only its prompt pointer first: name the restack there.
            extra["resume"]["findings"] = "; ".join(x for x in (restack["line"], extra["resume"]["findings"]) if x)
    with db.transaction(con):
        if decision:
            dispatch._record_routing(con, run, decision)
        did = dispatch.request_launch(con, run, tid, role="executor", fix_of=task.get("current_revision_id"),
                                      extra=extra, base=(restack or {}).get("base"), decision=decision)
        if restack:
            state.emit(con, run, "task.restacked", f"{tid} {restack['line']}", task_id=tid, dispatch_id=did)
        if resume:
            _set_resumed_from(con, did, parent["id"])
        state.emit(con, run, "task.rerun", f"{tid} rerun {'--resume from ' + parent['id'] if resume else '--fresh'} "
                   f"as {did}", task_id=tid, dispatch_id=did)
    jobs.kick(con, run["id"])
    route_line = (f" on {decision['selected']} (rerouted)" if decision
                  else f" on {parent.get('triple')} (original route)")
    res = Result(lines=[f"{tid} -> {did} executor {'resuming ' + parent['id'] if resume else 'fresh session'}"
                        f"{route_line} launching"]
                 + ([f"{tid} {restack['line']}"] if restack else []))
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
