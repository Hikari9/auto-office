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

from office import adapters, candidates, contract, db, dispatch, gates, integration, jobs, paths, state
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
    """Bring the task worktree up to its dependencies' revisions.

    A task built on a dependency revision that review later superseded can
    never be accepted (gates.stale_dependency). Office merges each accepted
    (else current) revision the worktree lacks; a merge is never a rebase, so the pushed
    branch needs no force-push. On a conflict the merge is aborted and the
    executor is told to make it. The new dispatch's base is the commit that contains every
    dependency revision (integration.combine); dependencies that conflict with each other
    refuse before anything is merged.
    Returns {"base", "merged", "conflict", "line"}."""
    if not worktree or not (Path(worktree) / ".git").exists():
        return None
    wt = Path(worktree)
    heads = integration.dependency_heads(con, run, task)
    head = paths.git(wt, "rev-parse", "HEAD")
    pending = [h for h in heads if not gates._is_ancestor(run, h["commit"], head)]
    if not pending:
        return None
    onto = ((run.get("landing") or {}).get("rebase") or {}).get("onto")
    held = [{"task": "the new run base", "revision": onto[:12], "commit": onto}] \
        if onto and gates._is_ancestor(run, onto, head) else []
    try:
        # `office rebase` put this worktree on the run's moved base: the new base keeps it. When that base
        # collides with a dependency revision (which `office rebase` allows), the executor's merge settles it.
        try:
            base = integration.combine(run, heads + held, task["id"])
        except Refused as e:
            if not held or e.category != "dependency-conflict":
                raise
            base = integration.combine(run, heads, task["id"])
    except Refused as e:
        e.scope = e.scope or task["id"]
        e.preserved = "the task worktree"
        raise
    env = {**os.environ, **paths.commit_identity_env(wt)}
    merged = []
    for i, h in enumerate(pending):
        proc = subprocess.run(["git", "-C", str(wt), "merge", "--no-edit", "-m",
                               f"office: restack {task['id']} onto {h['task']} {h['revision']}\n\n{paths.office_trailer(run['id'])}",
                               h["commit"]], capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True)
            conflict = {**h, "then": pending[i + 1:]}
            return {"base": base, "merged": merged, "conflict": conflict,
                    "line": f"restack onto {h['task']} {h['revision']} conflicts; the executor merges {h['commit'][:7]} first"}
        merged.append(h)
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


def review_pin(review_as: str | None, review_cli: str | None = None, review_external: bool = False) -> dict | None:
    """The reviewer pin `--review-as` names, in the shape a task or gate stores."""
    return {"as": review_as, "cli": review_cli, "external": bool(review_external), "by": "user"} if review_as else None


def _why_no_review(con, run: dict, task: dict) -> str:
    """Why a v3.1 task has no review that a re-run could replace."""
    if gates.worker_live(con, task.get("current_dispatch_id")):
        return f"{task['id']} still has a live worker; its review comes after it submits"
    rev = task.get("current_revision_id")
    if not rev:
        return f"{task['id']} has no submitted revision to review (office dispatch {task['id']} starts its executor)"
    code = next((g for g in gates.required_gates(con, run, task, rev) if g["kind"] == "code_review"), None)
    if code is None:
        return f"{task['id']} {rev} has no code review gate"
    if code["status"] in ("queued", "running", "waiting"):
        return f"{task['id']} code review is {code['status']}: a reviewer is already on {rev}"
    if code["verdict"] == "UNAVAILABLE" or code["verdict"] == "ATTENTION":
        return f"{task['id']} is {task['status']}, not blocked on that review"
    return (f"{task['id']} code review already has a verdict on {rev} ({code['verdict']}); findings are answered with "
            f"office rerun {task['id']} --resume|--fresh, not a second review")


def review_rerun(con, run: dict, tid: str, pin: dict | None, *, strict: bool = True) -> tuple[list[str], bool]:
    """Re-dispatch only the reviewer of the task's current revision (or, under the
    convergence contract, of its lane's composed revision), after pinning its
    route. Never launches an executor. Returns (lines, queued). A review that
    cannot be re-run (a verdict stands, one is running, no revision yet) raises
    Refused when `strict`; otherwise the pin is kept and the line says why.
    Caller holds the tx."""
    from office import convergence
    parsed = convergence.parse_gate_target(tid)
    task = None if parsed else state.get_task(con, run["id"], tid.upper())
    if parsed is None and (task is None or task["role"] == "planner"):
        raise Usage("unknown-task", f"{tid} is not a task or a lane gate", next_step="office status")
    try:
        if contract.is_convergence(run):
            made, pinned = convergence.rerun_review(con, run, tid, pin=pin)
            return [f"{', '.join(made)} reviewer re-run on its composed revision"
                    + (f" (pinned to {pin['as']}: {', '.join(pinned)})" if pinned else "")
                    + (f" (the {pin['as']} pin applies to a gate named by id; none was)" if pin and not pinned else "")
                    + "; no executor launched"], True
        if parsed:
            raise Usage("lane-gate", f"{tid} is a lane gate; under the v3.1 contract review is per task",
                        next_step="office rerun <task> --review")
        if gates.unavailable_review_block(con, run, task) is None:
            raise Refused("nothing-to-rerun", _why_no_review(con, run, task), scope=task["id"],
                          next_step=f"office status; office rerun {task['id']} --resume|--fresh answers findings")
        if pin:
            convergence.set_review_pin(con, run, task["id"], pin["as"], cli=pin.get("cli"), external=pin.get("external", False))
        rev = task["current_revision_id"]
        gid = gates.rerun_unavailable_review(con, run, state.get_task(con, run["id"], task["id"]))
        return [f"{task['id']} code review re-run on {rev} (gate {gid}" + (f", reviewer {pin['as']}" if pin else "")
                + "); the submission is kept, no executor launched"], True
    except Refused as exc:
        if strict:
            raise
        if pin and task is not None:
            convergence.set_review_pin(con, run, task["id"], pin["as"], cli=pin.get("cli"), external=pin.get("external", False))
        return [f"{tid}: reviewer pinned to {pin['as'] if pin else '(unchanged)'} for its next review; no review to re-run "
                f"now: {exc}"], False


def rerun(con, run: dict, tid: str, *, resume: bool, fresh: bool, reroute: bool = False, review: bool = False,
          as_model: str | None = None, cli: str | None = None, external: bool = False, review_as: str | None = None,
          review_cli: str | None = None, review_external: bool = False) -> Result:
    dispatch.check_route_flags(as_model=as_model, cli=cli, external=external, review_as=review_as,
                               review_cli=review_cli, review_external=review_external)
    pin = review_pin(review_as, review_cli, review_external)
    if review or pin:
        gates.require_orchestrator(con, run, "re-run or pin a reviewer", code="worker-cannot-pin-reviewer")
    if not convergence_gate(tid):
        tid = tid.upper()
    if review and state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}")
    if review and tid.lower() == "plan":
        if resume or fresh or reroute or as_model or cli or external:
            raise Usage("rerun-mode", "--review re-runs only the reviewer and takes none of --resume, --fresh, "
                        "--reroute, --as, --cli, --external", next_step="office rerun plan --review [--review-as <route>]")
        from office import plans

        with db.transaction(con):
            gid = plans.rerun_review(con, run, pin=pin)
        jobs.kick(con, run["id"])
        return Result(lines=[f"plan review re-run (gate {gid}" + (f", reviewer {pin['as']}" if pin else "")
                             + "); no round spent"], next="exceptions only; office status")
    if review:
        if resume or fresh or reroute or as_model or cli or external:
            raise Usage("rerun-mode", "--review re-runs only the reviewer of the current revision and never launches an "
                        "executor, so it takes none of --resume, --fresh, --reroute, --as, --cli, --external",
                        next_step=f"office rerun {tid} --review [--review-as <route>]")
        with db.transaction(con):
            lines, _ = review_rerun(con, run, tid, pin)
        jobs.kick(con, run["id"])
        return Result(lines=lines, next="exceptions only; office status")
    if resume == fresh:
        if review_as and not (resume or fresh) and not (as_model or cli or external or reroute):
            return _pin_only(con, run, tid, pin)
        raise Usage("rerun-mode", f"say how to rerun {tid}: --resume continues the ended session with its context "
                    "(native harness resume); --fresh starts a new session with the open findings in its brief; "
                    "--review re-runs only the reviewer",
                    next_step=f"office rerun {tid} --resume | {_fresh_cmd(tid)} | office rerun {tid} --review")
    if reroute and resume:
        raise Usage("rerun-mode", "--reroute starts a new session on another route; a resumed session keeps its own",
                    next_step=f"{_fresh_cmd(tid)} --reroute")
    if resume and (as_model or cli or external):
        raise Usage("rerun-mode", "--as/--cli/--external start a new session on that route; a resumed session keeps its own",
                    next_step=f"{_fresh_cmd(tid)} --as <route>")
    if reroute and as_model:
        raise Usage("invalid-override", "--reroute routes from evidence; --as names the route yourself")
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
    if as_model:
        decision = candidates.declared_decision(as_model)
        if cli or external:
            decision["launch"] = {k: v for k, v in (("cli", cli), ("external", external)) if v}
    elif reroute:
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
        if pin:
            state.update_task(con, run["id"], tid, review_override=pin)
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
    route_line = (f" on {decision['selected']} ({'your --as route' if as_model else 'rerouted'})" if decision
                  else f" on {parent.get('triple')} (original route)")
    res = Result(lines=[f"{tid} -> {did} executor {'resuming ' + parent['id'] if resume else 'fresh session'}"
                        f"{route_line} launching"]
                 + ([f"{tid} {restack['line']}"] if restack else []))
    res.lines.extend(f"  {line}" for line in dispatch.launch_instructions(run, state.get_dispatch(con, did)))
    res.next = "exceptions only; office status"
    return res


def convergence_gate(target: str) -> bool:
    from office import convergence
    return convergence.parse_gate_target(target) is not None


def _pin_only(con, run: dict, tid: str, pin: dict) -> Result:
    """`office rerun T1 --review-as <route>` with no other mode: change the
    reviewer pin while the worker keeps running. It applies to the next review."""
    from office import convergence
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        what = convergence.set_review_pin(con, run, tid, pin["as"], cli=pin.get("cli"),
                                          external=pin.get("external", False))
    return Result(lines=[f"{what} reviewer pinned to {pin['as']}; it applies to the next review, and no worker or "
                         "running review changes"], next="exceptions only; office status")


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
