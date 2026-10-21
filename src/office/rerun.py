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
import tempfile
from pathlib import Path

from office import adapters, candidates, contract, db, dispatch, gates, jobs, paths, routing, state
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


def _set_resumed_from(con, dispatch_id: str, parent: str, session: str | None = None) -> None:
    """Link a resumed dispatch to its parent. It continues the parent's harness
    session (the resume argv names it), so it records that id too: a later
    resume of this dispatch then still finds a session to continue."""
    try:
        con.execute("UPDATE dispatches SET resumed_from=?, session_id=COALESCE(session_id, ?) WHERE id=?",
                    (parent, session, dispatch_id))
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
    executor is told to make it. A dependency reopened for changes is not merged
    and is reported. Returns {"base", "merged", "conflict", "reopened", "line"}
    where `base` is every accepted dependency revision combined (the base the
    new dispatch records), or None when a merge conflicted or they cannot combine."""
    if not worktree or not (Path(worktree) / ".git").exists():
        return None
    wt = Path(worktree)
    head = paths.git(wt, "rev-parse", "HEAD")
    from office import integration
    onto = integration.compose_base(run)
    rebased = onto != run["base_sha"]
    accepted, pending, reopened = [], [], []
    if rebased:
        accepted.append(("rebased default branch", onto))
        if not gates._is_ancestor(run, onto, head):
            pending.append(("rebased default branch", "base", onto))
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep)
        rev_id = (dt or {}).get("accepted_revision_id")
        if dt and dt["status"] == "accepted" and rev_id:
            row = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone()
            if row:
                accepted.append((dep, row["commit_sha"]))
                if not gates._is_ancestor(run, row["commit_sha"], head):
                    pending.append((dep, rev_id, row["commit_sha"]))
        elif dt and (rev_id or dt["status"] == "changes_required"):
            reopened.append({"task": dep, "status": dt["status"], "revision": rev_id})
    note = ", ".join(f"{r['task']} is {r['status']} and was not restacked" for r in reopened)
    if not pending:
        if rebased:
            try:
                base = integration.combine(run, accepted)
            except (integration.CombineConflict, paths.GitError) as exc:
                raise Refused("rebase-conflict", f"{task['id']} cannot combine the rebased main with accepted dependencies: {exc}",
                              preserved="task worktree and accepted revisions", next_step="resolve the conflicting base, then rerun") from exc
            return {"base": base, "merged": [], "conflict": None, "reopened": reopened,
                    "line": "; ".join(x for x in ("task base updated to the rebased default branch", note) if x)}
        return {"base": None, "merged": [], "conflict": None, "reopened": reopened, "line": note} if reopened else None
    # Scratch first: conflicting later dependency must never advance the real
    # task HEAD after a successful earlier merge (#580).
    if paths.git(wt, "status", "--porcelain", "--untracked-files=no").strip():
        raise Refused("restack-dirty-worktree", f"{task['id']} has uncommitted changes",
                      preserved="the task worktree", next_step="commit or stash changes first")
    env = {**os.environ, **paths.commit_identity_env(wt)}
    merged = []
    with tempfile.TemporaryDirectory(prefix="office-restack-") as tmp:
        scratch = Path(tmp) / "worktree"
        paths.git(Path(run["repo_root"]), "worktree", "add", "-q", "--detach", str(scratch), head)
        try:
            for dep, rev_id, sha in pending:
                proc = subprocess.run(["git", "-C", str(scratch), "merge", "--no-edit", "-m",
                                       f"office: restack {task['id']} onto {dep} {rev_id}", sha],
                                      capture_output=True, text=True, env=env)
                if proc.returncode != 0:
                    subprocess.run(["git", "-C", str(scratch), "merge", "--abort"], capture_output=True)
                    if rebased:
                        raise Refused("rebase-conflict", f"{task['id']} cannot restack onto {dep} {sha[:12]} on "
                                      "rebased default branch; executor not launched",
                                      preserved="original task HEAD and worktree",
                                      next_step=f"resolve dependency and rerun {task['id']} --fresh")
                    return {"base": None, "merged": [], "conflict": {"task": dep, "revision": rev_id,
                            "commit": sha}, "unmerged": [
                            {"task": t, "revision": r, "commit": c} for t, r, c in pending],
                            "reopened": reopened, "line": f"restack onto {dep} conflicts; task HEAD unchanged"}
                merged.append({"task": dep, "revision": rev_id, "commit": sha})
            try:
                base = integration.combine(run, accepted)
            except (integration.CombineConflict, paths.GitError) as exc:
                if rebased:
                    raise Refused("rebase-conflict", f"{task['id']} cannot combine rebased base: {exc}",
                                  preserved="original task HEAD", next_step="resolve base conflict") from exc
                base = None
            if paths.git(wt, "rev-parse", "HEAD") != head:
                raise Refused("restack-raced", f"{task['id']} branch changed during restack",
                              preserved="task worktree", next_step="retry from current task revision")
            candidate = paths.git(scratch, "rev-parse", "HEAD")
            if candidate != head:
                proc = subprocess.run(["git", "-C", str(wt), "merge", "--ff-only", candidate],
                                      capture_output=True, text=True, env=env)
                if proc.returncode != 0:
                    raise Refused("restack-publish-failed", f"cannot advance task branch: {proc.stderr[:180]}",
                                  preserved="task HEAD unchanged", next_step="inspect worktree and retry")
        finally:
            subprocess.run(["git", "-C", str(run["repo_root"]), "worktree", "remove", "--force", str(scratch)],
                           capture_output=True)
    return {"base": base, "merged": merged, "conflict": None, "reopened": reopened,
            "line": "; ".join(x for x in ("restacked onto " + ", ".join(f"{m['task']} {m['revision']}" for m in merged), note) if x)}


def _sticky_check(con, run: dict, task: dict, parent: dict) -> str | None:
    """Why the parent's route may not run again now, from current evidence."""
    if parent.get("role") != "executor":
        return None
    try:
        dispatch.qualify_recorded_route(con, run, task, parent.get("route") or {}, observe=True)
    except (Refused, Usage) as exc:
        return exc.message
    except sqlite3.Error as exc:
        return f"route evidence could not be read: {exc}"
    return None


def _route_decision(con, run: dict, task: dict, parent: dict, *, as_model: str | None, reroute: bool,
                    resume: bool, quote: str | None = None) -> dict | None:
    """The route this rerun declares or re-evaluates, or None to keep the parent's route.

    `--as` names a route with orchestrator authority unless `--quote` records the user; `--reroute`
    routes from current evidence; a route declared between rounds (`office amend route`)
    is followed. A resumed session keeps its harness, so only `--as` on the same harness resumes."""
    if as_model is not None:
        if resume:
            cand = candidates.declared_decision(as_model, role="executor", config=state.pinned_config(run),
                                                repo_root=run.get("repo_root"))["candidate"]
            if cand["harness"] != (parent.get("adapter_id") or parent.get("harness")):
                raise Refused("resume-impossible", f"cannot resume {task['id']} on {cand['harness']}: "
                              f"{parent['id']}'s session belongs to {parent.get('adapter_id') or parent.get('harness')}",
                              scope=task["id"], preserved="the task worktree", next_step=f"{_fresh_cmd(task['id'])} --as {as_model}")
            if cand.get("invocation_model_id") != parent.get("model") or cand.get("effort") != parent.get("effort"):
                raise Refused("resume-impossible", f"cannot resume {task['id']} on {as_model}: {parent['id']}'s session "
                              f"keeps its model and effort ({parent.get('model')}@{parent.get('effort')})",
                              scope=task["id"], preserved="the task worktree", next_step=f"{_fresh_cmd(task['id'])} --as {as_model}")
        decision = dispatch.named_executor_route(con, run, task, as_model, quote=quote)
        if decision.get("status") != "selected":
            raise Refused("no-route", dispatch._route_failure(task["id"], decision), scope=task["id"],
                          next_step=dispatch._route_next(decision, task["id"], run))
        return decision
    if reroute:
        decision = dispatch.planned_route(con, run, task, reroute=True)
        if decision.get("status") != "selected":
            raise Refused("no-route", dispatch._route_failure(task["id"], decision), scope=task["id"],
                          next_step=dispatch._route_next(decision, task["id"], run))
        return decision
    rec = state.recorded_route(task)
    declared = rec.get("declared") and dispatch._route_quote(rec)
    if declared and routing.candidate_id(rec["candidate"]) != parent.get("triple"):
        # A route declared between rounds (office amend route) is followed, not the parent's.
        if resume:
            raise Refused("resume-impossible", f"{task['id']}'s route was changed since {parent['id']}; a resumed "
                          "session keeps its own", scope=task["id"], next_step=_fresh_cmd(task["id"]))
    route = rec if declared else parent.get("route") or {}
    try:
        decision = dispatch.qualify_recorded_route(con, run, task, route)
    except Refused as exc:
        fix = f"{_fresh_cmd(task['id'])} --reroute routes from current evidence"
        if dispatch._route_quote(route):
            fix += f'; or {_fresh_cmd(task["id"])} --as <harness>/<model>[@effort] --quote "<user\'s words>"'
        if exc.next_step:
            fix += f"; {exc.next_step}"
        original = routing.candidate_id(route["candidate"]) if route.get("candidate") else parent.get("triple")
        raise Refused("route-unavailable",
                      f"{task['id']}'s original route {original} cannot run now: {exc.message}",
                      scope=task["id"], preserved="the task worktree and its route history", next_step=fix) from exc
    cand = decision["candidate"]
    if resume and (cand["harness"], cand.get("invocation_model_id"), cand.get("effort")) != (
            parent.get("adapter_id") or parent.get("harness"), parent.get("model"), parent.get("effort")):
        raise Refused("resume-impossible", f"cannot resume {task['id']}: {parent['id']}'s session keeps its "
                      f"harness, model and effort ({parent.get('harness')}/{parent.get('model')}@{parent.get('effort')})",
                      scope=task["id"], preserved="the task worktree", next_step=_fresh_cmd(task["id"]))
    return decision


def _mode_required(tid: str) -> Usage:
    return Usage("rerun-mode", f"say how to rerun {tid}: --resume continues the ended session with its context "
                 "(native harness resume); --fresh starts a new session with the open findings in its brief",
                 next_step=f"office rerun {tid} --resume | {_fresh_cmd(tid)}")


def rerun(con, run: dict, tid: str, *, resume: bool, fresh: bool, reroute: bool = False,
          as_model: str | None = None, quote: str | None = None, cli: str | None = None, external: bool = False,
          review_as: str | None = None, review_cli: str | None = None, review_external: bool = False) -> Result:
    if resume == fresh and review_as is None:
        raise _mode_required(tid)
    if reroute and resume:
        raise Usage("rerun-mode", "--reroute starts a new session on another route; a resumed session keeps its own",
                    next_step=f"{_fresh_cmd(tid)} --reroute")
    if reroute and as_model is not None:
        raise Usage("invalid-override", "--reroute routes from evidence; --as names the route yourself")
    dispatch.validate_route_quote(quote, as_model)
    if cli and as_model is None:
        raise Usage("invalid-override", "--cli needs --as <harness>/<model>[@effort] so the rerun records what runs")
    if cli and external:
        raise Usage("invalid-override", "a CLI launch and an external launch are mutually exclusive")
    if (review_cli or review_external) and review_as is None:
        raise Usage("invalid-override", "--review-cli/--review-external need --review-as <harness>/<model>[@effort]")
    if review_cli and review_external:
        raise Usage("invalid-override", "a CLI review and an external review are mutually exclusive")
    if review_as is not None:
        # the route's shape and capability, before any pin is written
        candidates.declared_decision(review_as, flag="--review-as", role="code_reviewer", config=state.pinned_config(run))
    task = state.get_task(con, run["id"], tid)
    if task is None:
        raise Usage("unknown-task", f"no task {tid}")
    if task["status"] in ("accepted", "cancelled"):
        raise Refused("task-done", f"{tid} is {task['status']}; nothing to rerun", scope=tid)
    if review_as is not None and gates.unavailable_review_block(con, run, task):
        # The revision is final and only its review is missing: the rerun is the review, never an executor.
        res = gates.review_only(con, run, tid, review_as=review_as, review_cli=review_cli, review_external=review_external)
        ignored = [f"--{f}" for f, on in (("resume", resume), ("fresh", fresh), ("reroute", reroute), ("as", as_model),
                                          ("cli", cli), ("external", external)) if on]
        if ignored:
            res.lines.append(f"{tid} {' '.join(ignored)} ignored: no executor rerun applies to a review-only block")
        return res
    if resume == fresh:
        raise _mode_required(tid)
    live = gates.live_task_session(con, run["id"], tid)
    if live:
        raise Refused("worker-live", f"{tid} still has a live worker ({live})", scope=tid,
                      next_step=f'office prompt {tid} -- "<message>" to reach it (an amendment already tells it to '
                                f"resubmit), or office revoke {tid} to end it first")
    parent = _last_ended_executor(con, run, tid)
    if parent is None:
        raise Refused("no-ended-executor", f"{tid} has no ended executor session to rerun", scope=tid,
                      next_step=f"office dispatch {tid}")
    rec = state.recorded_route(task)
    if (external and as_model is None and not reroute and not (parent.get("route") or {}).get("candidate")
            and not (rec.get("declared") and dispatch._route_quote(rec))):
        raise Refused("no-route", f"{tid} has no route to carry --external; name one with --as", scope=tid,
                      next_step=f"{_fresh_cmd(tid)} --as <harness>/<model>[@effort]")
    decision = _route_decision(con, run, task, parent, as_model=as_model, quote=quote, reroute=reroute, resume=resume)
    extra = None
    if resume:
        why = None
        # An id never seen live (herdr reported none, the banner scrolled away)
        # may still be proven by the session's own transcript (#406).
        session = parent.get("session_id") or dispatch.backfill_session(con, run, parent)
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
    launch_prefs = {k: v for k, v in (("cli", cli), ("external", external)) if v}
    # The route this rerun runs: a declared or re-evaluated one, else the parent's (launch form only).
    parent_route = parent.get("route") or {}
    launch = decision or ({**parent_route, "status": "selected", "selected": parent.get("triple")}
                          if launch_prefs and parent_route.get("candidate") else None)
    if launch_prefs and not launch:
        raise Refused("no-route", f"{tid} has no route to carry --{'cli' if cli else 'external'}; name one with --as",
                      scope=tid, next_step=f"{_fresh_cmd(tid)} --as <harness>/<model>[@effort]")
    if launch and launch_prefs:
        launch = {**launch, "launch": launch_prefs}
    restack = _restack(con, run, task, parent.get("worktree"))
    if restack:
        extra = {**(extra or {}), "restack": {k: restack.get(k) for k in ("merged", "conflict", "unmerged", "reopened")}}
        if resume:
            # A resumed session reads only its prompt pointer first: name the restack there.
            extra["resume"]["findings"] = "; ".join(x for x in (restack["line"], extra["resume"]["findings"]) if x)
    with db.transaction(con):
        if decision:
            dispatch._record_routing(con, run, decision)
            dispatch.note_route(con, run, task, decision)
        if review_as is not None:
            state.update_task(con, run["id"], tid, review_override=gates.review_pin(review_as, review_cli, review_external))
        did = dispatch.request_launch(con, run, tid, role="executor", fix_of=task.get("current_revision_id"),
                                      extra=extra, base=(restack or {}).get("base"), decision=launch)
        if restack and (restack["merged"] or restack["conflict"]):
            state.emit(con, run, "task.restacked", f"{tid} {restack['line']}", task_id=tid, dispatch_id=did)
        if resume:
            _set_resumed_from(con, did, parent["id"], session)
        state.emit(con, run, "task.rerun", f"{tid} rerun {'--resume from ' + parent['id'] if resume else '--fresh'} "
                   f"as {did}", task_id=tid, dispatch_id=did)
    jobs.kick(con, run["id"])
    if reroute:
        note = "rerouted"
    elif ((launch or parent.get("route")) or {}).get("override"):
        note = "user override"
    elif ((launch or parent.get("route")) or {}).get("route_authority", {}).get("actor") == "orchestrator":
        note = "orchestrator choice"
    else:
        note = "original route"
    route = launch["selected"] if launch else parent.get("triple")
    res = Result(lines=[f"{tid} -> {did} executor {'resuming ' + parent['id'] if resume else 'fresh session'}"
                        f" on {route} ({note}) launching"]
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
