"""Strategy-owned dispatch with runtime-owned mechanics.

The orchestrator says which tasks run and whether in parallel. The runtime
routes each role, checks dependencies and scope ownership, takes a fenced lease,
creates the worktree, builds the packet (with the run-pinned office_version),
injects the environment, and launches through a durable outbox job.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import termios
except ImportError:  # not POSIX: a pane's terminal mode cannot be read
    termios = None

from office import adapters, briefs, candidates, db, frontdoor, jobs, paths, planfile, planpath, read_scope, routing, state, version, worktree_setup
from office.result import Result
from office.state import Refused, Usage
from office.util import (atomic_write_json, claim_alive, claim_signalable, dumps, now_iso, parse_iso, pid_alive,
                         process_is, process_start, sha256_obj, short, loads)

LEASE_TTL_SECONDS = 4 * 3600
# A harness session id lands in a resume argv: plain id characters only, never a leading dash.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
IDENTITY_ENV = ("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE", "OFFICE_STATE_DIR",
                "OFFICE_SESSION", "OFFICE_HARNESS", "OFFICE_VERSION", "OFFICE_FRONT_DOOR_HOPS")
PLANNER_TASK = "P1"


# ------------------------------------------------------------------ planner task

def create_planner_task(con, run: dict, *, contract_request: str | None = None, decision: dict | None = None) -> str:
    """Queue the dedicated planner. Caller holds the transaction."""
    now = now_iso()
    existing = state.get_task(con, run["id"], PLANNER_TASK)
    if existing is None:
        con.execute(
            "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
            "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], PLANNER_TASK, "Plan the run", "planner", dumps([planpath.rel(run)]), dumps([]), dumps([]),
             dumps(["a plan that satisfies the frozen requirements"]), dumps([]), None, "queued", 0, 0, 0, now, now))
    else:
        state.update_task(con, run["id"], PLANNER_TASK, status="queued", pause_reason=None)
    return request_launch(con, run, PLANNER_TASK, role="planner", decision=decision,
                          extra={"contract_request": contract_request})


# ------------------------------------------------------------------ dispatch command

def dispatch(con, run: dict, task_ids: list[str], *, parallel: bool = False, route: str | None = None,
             as_model: str | None = None, cli: str | None = None, external: bool = False,
             review_as: str | None = None, review_cli: str | None = None, review_external: bool = False,
             reroute: bool = False) -> Result:
    if not task_ids:
        raise Usage("no-task", "name at least one task", next_step="office dispatch T1 [T2 ...] [--parallel]")
    if as_model and route:
        raise Usage("invalid-override", "use --as or --route, not both")
    if reroute and (as_model or route):
        raise Usage("invalid-override", "--reroute routes from evidence; --as/--route name the route yourself")
    if cli and external or review_cli and review_external:
        raise Usage("invalid-override", "a CLI launch and an external launch are mutually exclusive")
    if cli and not as_model:
        raise Usage("invalid-override", "--cli needs --as <harness>/<model>[@effort] so the dispatch records what runs")
    if (review_cli or review_external) and not review_as:
        raise Usage("invalid-override", "--review-cli/--review-external need --review-as <harness>/<model>[@effort]")
    launch_prefs = {k: v for k, v in (("cli", cli), ("external", external)) if v}
    if review_as:
        candidates.declared_decision(review_as, flag="--review-as")  # validates the route's shape
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}")
    from office import guide, plan_view, plans, prs, queuecmd
    plans.require_dispatchable(con, run)
    for tid in task_ids:
        held = queuecmd.paused_block(con, run["id"], tid)
        if held:
            raise Refused("scheduler-paused", f"{held['id']} is paused by the operator",
                          next_step=queuecmd.resume_command(held))
    prs.settings(con, run)  # detected once, outside the transaction (it asks GitHub)
    # Route before the write transaction: routing reads evidence and probes quota.
    routes = {}
    for tid in task_ids:
        task = state.get_task(con, run["id"], tid)
        if task is None or task["role"] == "planner":
            raise Usage("unknown-task", f"{tid} is not a task in plan p{run['plan_version']}",
                        next_step="office status lists the ready tasks")
        if not (as_model or route or cli or external):
            from office import gates
            block = gates.unavailable_review_block(con, run, task)
            if block:
                continue
        if as_model:
            routes[tid] = candidates.declared_decision(as_model)
        else:
            routes[tid] = planned_route(con, run, task, override=route, reroute=reroute)
        if launch_prefs and routes[tid].get("status") == "selected":
            routes[tid]["launch"] = launch_prefs
    res = Result()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
        previous = None
        for tid in task_ids:
            task = state.get_task(con, run["id"], tid)
            if tid not in routes:
                # A submitted revision blocked only by an UNAVAILABLE code
                # review needs its review re-run, not a fresh executor.
                from office import gates
                if review_as:
                    state.update_task(con, run["id"], tid, review_override={
                        "as": review_as, "cli": review_cli, "external": bool(review_external), "by": "user"})
                gid = gates.rerun_unavailable_review(con, run, state.get_task(con, run["id"], tid))
                if gid is None:
                    raise Refused("state-changed", f"{tid} is no longer blocked on its code review; re-run dispatch",
                                  scope=tid, next_step=f"office dispatch {tid}")
                res.add(f"{tid} code review re-run on {task['current_revision_id']} (gate {gid}"
                        + (f", reviewer {review_as}" if review_as else "") + "); the submission is kept, "
                        "no executor launched")
                previous = tid
                continue
            after = task.get("stack_after")
            if (task["status"] == "queued" and after
                    and (state.get_task(con, run["id"], after) or {}).get("status") == "accepted"):
                # Stacked after a task that was accepted before the stack was
                # recorded: start_stacked already fired, so launch it now.
                state.update_task(con, run["id"], tid, stack_after=None, pause_reason=None)
                decision = routes.get(tid) if (routes.get(tid) or {}).get("status") == "selected" else None
                if decision:
                    _record_routing(con, run, decision)
                did = request_launch(con, run, tid, role="executor", decision=decision,
                                     base=_base_for(con, run, task, graph, after))
                res.add(f"{tid} was stacked after {after}, which is already accepted -> {did} launching")
                previous = tid
                continue
            if task["status"] in ("running", "launching", "submitted", "changes_required", "queued"):
                res.add(f"{tid} already {task['status']}")
                previous = tid
                continue
            if task["status"] == "accepted":
                res.add(f"{tid} already accepted")
                previous = tid
                continue
            plans.require_scope_clear(con, run, tid)
            stack_after = None if parallel or previous is None else previous
            base = _base_for(con, run, task, graph, stack_after)
            if stack_after and (state.get_task(con, run["id"], stack_after) or {}).get("status") == "accepted":
                # Nothing would release a stack on an accepted task: launch now,
                # based on its accepted revision (base above).
                stack_after = None
            decision = routes[tid]
            if decision.get("status") != "selected":
                raise Refused("no-route", _route_failure(tid, decision), scope=tid,
                              preserved="plan and other dispatches", next_step=_route_next(decision, tid, run))
            _record_routing(con, run, decision)
            if review_as:
                state.update_task(con, run["id"], tid, review_override={
                    "as": review_as, "cli": review_cli, "external": bool(review_external), "by": "user"})
            if stack_after:
                state.update_task(con, run["id"], tid, status="queued", stack_after=stack_after,
                                  pause_reason=f"stacked after {stack_after}")
                con.execute("INSERT INTO artifact_versions(id, run_id, kind, version, content_hash, created_at) "
                            "VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], "stack", 0,
                                                     sha256_obj({"task": tid, "after": stack_after, "route": decision["selected"]}), now_iso()))
                _stash_route(con, run, tid, decision)
                res.add(f"{tid} stacked after {stack_after}")
            else:
                # A planned slate reports its own fallback; drift is for legacy previews.
                drift = None if decision.get("route_source") == "plan" else plan_view.drift(con, run, tid, decision)
                if drift:
                    res.add(drift)
                did = request_launch(con, run, tid, role="executor", decision=decision, base=base)
                verb = "prepared for you to start (external; nothing launched)" if external else "launching"
                res.add(f"{tid} -> {did} executor/{decision['selection_disclosure']['triple']} {verb}"
                        + (" (user override)" if decision.get("override") else "")
                        + (f" ({decision['route_note']})" if decision.get("route_note") else ""))
                res.lines.extend(f"  {line}" for line in launch_instructions(run, state.get_dispatch(con, did)))
            previous = tid
        if run["phase"] == "planning":
            state.update_run(con, run["id"], phase="executing")
        state.emit(con, run, "dispatch", f"dispatched {' '.join(task_ids)}{' in parallel' if parallel else ''}",
                   payload={"tasks": task_ids, "parallel": parallel})
    jobs.kick(con, run["id"])
    res.next = "exceptions only; office status"
    return res


def _planned_slate(con, run: dict, tid: str) -> dict | None:
    """The approved plan's route plan for `tid` (#300), or None for a legacy preview."""
    from office import plan_view
    pv = plan_view.load(con, run["id"], run["plan_version"]) if run.get("plan_version") else None
    entry = ((pv or {}).get("tasks") or {}).get(tid) or {}
    plan = entry.get("route_plan")
    return {**plan, "audit_id": entry.get("audit_id"), "decision_hash": entry.get("decision_hash")} \
        if plan and plan.get("primary") else None


def planned_route(con, run: dict, task: dict, *, override: str | None = None, reroute: bool = False) -> dict:
    """Route an executor dispatch: the planner's primary, else its fallbacks in order.

    Live evidence (quota, availability, trust, learned eligibility) is refreshed
    by routing again now; the planned routes are then tried in their recorded
    order and the first one that still qualifies runs. A route is never swapped
    for an unplanned one: when every planned route fails, the result is
    `slate_exhausted` and the orchestrator reroutes (`--reroute`). Without a
    planned slate (`--route`, `--reroute`, a pre-#300 plan) the fresh decision stands."""
    tid = task["id"]
    kind = "fix" if task.get("current_dispatch_id") else "fresh"
    fresh = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id=tid, override=override,
                                  dispatch_kind=kind)
    planned = None if (override or reroute) else _planned_slate(con, run, tid)
    if reroute and fresh.get("status") == "selected":
        fresh["route_source"], fresh["route_note"] = "reroute", "rerouted from current evidence"
    if not planned:
        fresh.setdefault("route_source", "override" if override else "router")
        return fresh
    qualifying = fresh.get("qualifying_candidates") or {}
    reasons = {r["candidate"]: r["reason"] for r in fresh.get("rejected") or []}
    order = [planned["primary"], *(planned.get("fallbacks") or [])]
    taken = []
    for i, rid in enumerate(order):
        if rid in qualifying:
            break
        taken.append({"route": rid, "reason": reasons.get(rid) or "no longer a candidate (harness unavailable, "
                                                                 "excluded, or removed from the catalog)"})
    else:
        return {"status": "slate_exhausted", "selected": None, "rejected": fresh.get("rejected") or [],
                "skipped": fresh.get("skipped") or [], "fallbacks_taken": taken, "fresh_status": fresh.get("status"),
                "request": fresh.get("request"), "routing": fresh.get("routing")}
    cand = qualifying[rid]
    req = fresh.get("request") or {}
    disclosure = routing.selection_disclosure("executor", cand, req.get("preferred_seed"),
                                              (req.get("policy") or {}).get("cost_policy", "balanced"))
    label = "planned primary" if i == 0 else f"planned fallback {i}"
    note = label if i == 0 else f"{label}; " + "; ".join(f"{t['route']}: {t['reason']}" for t in taken)
    disclosure["reason"] = f"{disclosure['reason'].split('; ')[0]}; {note}"
    disclosure["adaptive"] = True
    disclosure["route_plan"] = {k: planned.get(k) for k in ("primary", "fallbacks", "chooser", "audit_id")}
    return {**fresh, "status": "selected", "selected": rid, "candidate": cand, "selection_disclosure": disclosure,
            "route_source": "plan", "route_note": None if i == 0 else note, "fallbacks_taken": taken,
            "planned": planned,
            "decision_hash": sha256_obj({"planned": planned.get("decision_hash"), "fresh": fresh.get("decision_hash"),
                                         "dispatched": rid, "fallbacks_taken": taken})}


def _route_failure(tid: str, decision: dict) -> str:
    status = decision.get("status")
    if status == "slate_exhausted":
        tried = "; ".join(f"{t['route']}: {t['reason']}" for t in decision.get("fallbacks_taken") or [])
        return f"{tid}: every planned route is unavailable now ({tried})"
    rejected = decision.get("rejected") or []
    top = "; ".join(f"{r['candidate']}: {r['reason']}" for r in rejected[:3])
    skipped = "; ".join(f"{s['candidate']}: {s['reason']}" for s in (decision.get("skipped") or [])[:2])
    return f"{tid}: no qualifying executor route ({status}){': ' + top if top else ''}{' | ' + skipped if skipped else ''}"


def _route_next(decision: dict, tid: str, run: dict | None = None) -> str:
    if decision.get("status") == "slate_exhausted":
        return (f"office dispatch {tid} --reroute routes from current evidence; office inspect route {tid} shows "
                "the planned slate")
    for r in decision.get("rejected") or []:
        if r.get("stage") == 2:
            return (f"a user may promote a route: office approve trust {r['candidate']} --quote \"<user's words>\"; "
                    "or office inspect route for details")
    if decision.get("status") == "protected_quota_would_be_consumed":
        remedy = candidates.protected_quota_remedy(run, "executor", tid) if run else ""
        return ("wait for quota, choose a cheaper strategy, or obtain explicit user authority"
                + (f"; {remedy}" if remedy else ""))
    return f"office inspect route {tid}"


def _record_routing(con, run: dict, decision: dict) -> None:
    req = decision.get("request") or {}
    con.execute("INSERT INTO routing_decisions(id, run_id, role, request_hash, selected_triple, decision_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run["id"], req.get("role"), sha256_obj({k: v for k, v in req.items() if k != "candidates"}),
                 decision.get("selected"), decision.get("decision_hash"), now_iso()))
    if decision.get("routing"):
        from office import plan_view
        planned = decision.get("planned") or {}
        source = decision.get("route_source") or "router"
        audit = {**decision["routing"], "phase": "reroute" if source == "reroute" else "dispatch",
                 "task_id": req.get("task_id"), "role": req.get("role"), "decision_hash": decision.get("decision_hash"),
                 "planner": planned or {"chooser": "router", "primary": decision.get("selected")},
                 "dispatch": {"source": source, "dispatched": decision.get("selected"),
                              "fallbacks_taken": decision.get("fallbacks_taken") or [],
                              "planned_audit_id": planned.get("audit_id")}}
        explored = source != "plan" and (audit.get("exploration") or {}).get("picked") == decision.get("selected")
        decision["audit_id"] = plan_view.record_audit(con, run, audit, plan_version=run.get("plan_version"),
                                                      dispatched=decision.get("selected"), explored=explored)


def _stash_route(con, run, tid, decision):
    state.update_task(con, run["id"], tid, route_json=dumps(_route_payload(decision)))


def _route_payload(decision: dict) -> dict:
    """What a dispatch keeps of its decision, so a stacked start or a relaunch
    reproduces the same route, override, and launch form."""
    out = {"candidate": decision.get("candidate"), "selection_disclosure": decision.get("selection_disclosure")}
    for key in ("override", "launch", "benchmark_snapshot", "route_source", "fallbacks_taken", "audit_id"):
        if decision.get(key):
            out[key] = decision[key]
    return out


def launch_instructions(run: dict, d: dict, *, output: str | None = None) -> list[str]:
    """Where a dispatch's brief and identity live, and the herdr commands that
    start any agent on it by hand (SKILL.md "Herdr agents"). `output` makes it
    a reviewer's: it writes its review there instead of submitting."""
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    wt = d.get("worktree") or str(ddir)
    name = herdr_agent_name(d["id"])
    cli = ((d.get("route") or {}).get("launch") or {}).get("cli")
    if cli:
        argv = shlex.split(cli)
        kind, args = Path(argv[0]).name, argv[1:]
    else:
        adapter = adapters.load_all().get(d.get("adapter_id") or "")
        inter = adapters.interactive_argv(adapter, "worker" if output is None else "reviewer", model=d.get("model") or "",
                                          effort=d.get("effort") or "none", cwd=Path(wt),
                                          output=Path(output) if output else None,
                                          session_id=_assigned_session(d)) if adapter and d.get("model") else None
        kind, args = (inter[1], inter[0]) if inter else (d.get("harness") or "<kind>", [])
    if output is None:
        pointer = (f"Read and carry out the brief at {ddir / 'brief.md'} exactly. "
                   "When the work and its checks are complete, run: office submit")
    else:
        pointer = (f"Read and carry out the review brief at {ddir / 'brief.md'} exactly. Write your complete review to "
                   f"{output}. If your tools cannot write files, end your reply with the complete review instead. "
                   "Do not edit anything else.")
    return [f"brief: {ddir / 'brief.md'}", f"env: {ddir / 'agent.env'}", f"worktree: {wt}",
            *([f"output: {output}"] if output else []),
            f"herdr: herdr pane run <pane> {shlex.quote('. ' + str(ddir / 'agent.env') + ' && cd ' + wt)}",
            "       (a new pane's shell drops a line sent before it is ready: confirm it ran with `herdr pane read`, "
            "else clear the line with `herdr pane send-keys <pane> ctrl+u` and run it again)",
            f"       herdr agent start {name} --kind {kind} --pane <pane> -- {shlex.join(args)}".rstrip(),
            "       (wait until `herdr pane get <pane>` shows the agent and its UI is up; answer a codex "
            "'Trust this folder?' with Enter; send the pointer with `agent prompt`, never `pane run`)",
            f"       herdr agent prompt {name} {shlex.quote(pointer)}"]


def _base_for(con, run: dict, task: dict, graph: dict, stack_after: str | None) -> str:
    """Base commit: the run base, or what contains every dependency revision this task builds on
    (accepted, else current): the dependency head that already contains the others, else an
    Office merge commit of them. Dependencies that conflict refuse, naming them and the paths."""
    from office import integration
    heads = integration.dependency_heads(con, run, task, stack_after=stack_after)
    have = {h["task"] for h in heads}
    for dep in task["depends"]:
        if dep not in have and dep != stack_after:
            raise Refused("dependency-not-ready", f"{task['id']} depends on {dep}, which has no submitted revision",
                          scope=task["id"], next_step=f"dispatch {dep} first, or office dispatch {dep} {task['id']} (stacked)")
    if not heads:
        return run["base_sha"]
    try:
        return integration.combine(run, heads, task["id"])
    except Refused as e:
        e.scope = e.scope or task["id"]
        raise


# ------------------------------------------------------------------ launch request

def request_launch(con, run: dict, task_id: str, *, role: str, decision: dict | None = None,
                   base: str | None = None, extra: dict | None = None, fix_of: str | None = None) -> str:
    """Create the dispatch row, lease and launch job. Caller holds the tx.

    Routing for executors happens before the transaction; planner and fix
    rounds route here from pinned state because they have no orchestrator turn.
    """
    task = state.get_task(con, run["id"], task_id)
    prior = None
    if task.get("current_dispatch_id"):
        prior = state.get_dispatch(con, task["current_dispatch_id"])
    if decision is None:
        if prior and prior.get("route"):
            decision = {**prior["route"], "status": "selected", "selected": prior.get("triple")}
        elif task.get("route_json"):
            s = json.loads(task["route_json"])
            decision = {**s, "status": "selected", "selected": routing.candidate_id(s["candidate"])}
    if decision is None:
        raise Refused("no-route", f"{task_id}: no route recorded for relaunch", scope=task_id)
    cand = decision["candidate"]
    dispatch_id = "D" + uuid.uuid4().hex[:8]
    lease = acquire_lease(con, run, task, dispatch_id, role)
    worktree = prior["worktree"] if prior and prior.get("worktree") else str(paths.worktrees_dir() / run["id"][:8] / task_id)
    branch = prior["branch"] if prior and prior.get("branch") else f"office/{run['id'][:8]}/{task_id}"
    applied = task["contract_version"] if role != "planner" else run["plan_version"]
    con.execute(
        "INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, selection_reason, task_shape, "
        "started_at, task_id, kind, office_version, status, worktree, branch, base_commit, lease_id, harness, model, effort, "
        "adapter_id, applied_plan_version, route_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dispatch_id, run["id"], role, dispatch_id, routing.candidate_id(cand), cand.get("invocation_model_id"),
         (decision.get("selection_disclosure") or {}).get("reason"), run.get("playbook"), now_iso(), task_id, role,
         version.current(), "launching", worktree, branch, base or (prior or {}).get("base_commit") or run["base_sha"],
         lease["id"], cand["harness"], cand.get("invocation_model_id"), cand.get("effort"), cand.get("adapter_id"),
         applied, dumps(_route_payload(decision))))
    if decision.get("override") or decision.get("launch"):
        con.execute("UPDATE dispatches SET override_json=? WHERE id=?",
                    (dumps({"by": "user", "declared": bool(decision.get("override")), "triple": routing.candidate_id(cand),
                            **(decision.get("launch") or {})}), dispatch_id))
    # This session starts from the current contract, so an amendment still
    # waiting on an earlier session of the task is already in its brief; left
    # queued, it would hold the new revision at amendment_pending (and its ack
    # would be refused as the wrong holder).
    con.execute("UPDATE deliveries SET status='superseded', superseded_by='relaunch' WHERE run_id=? AND task_id=? "
                "AND status IN ('queued','delivered') AND dispatch_id IS NOT ? AND target_version<=?",
                (run["id"], task_id, dispatch_id, applied))
    # A newer amendment (a requirements change can target a plan version past
    # the task contract) is not in this brief: hand it to this session, or no
    # session could ever ack it and every submit would stay amendment_pending.
    con.execute("UPDATE deliveries SET dispatch_id=?, status='queued' WHERE run_id=? AND task_id=? "
                "AND status IN ('queued','delivered') AND dispatch_id IS NOT ?",
                (dispatch_id, run["id"], task_id, dispatch_id))
    state.update_task(con, run["id"], task_id, status="launching", current_dispatch_id=dispatch_id,
                      pause_reason=None, stack_after=None)
    payload = {"dispatch_id": dispatch_id, "task_id": task_id, "role": role, "fix_of": fix_of,
               **(decision.get("launch") or {}), **(extra or {})}
    state.enqueue(con, run, "launch_agent", payload, dedup_key=f"launch:{dispatch_id}", max_attempts=2)
    return dispatch_id


def acquire_lease(con, run: dict, task: dict, holder: str, role: str) -> dict:
    """One fenced lease per task scope. Overlapping live scopes are refused, except that a dependent's
    live lease never blocks its prerequisite (the plan lets ordered tasks overlap): the dependent is
    restacked or reported stale afterwards."""
    now = datetime.now(timezone.utc)
    graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
    dependants = planfile.dependants(graph, task["id"])
    rows = con.execute("SELECT * FROM leases WHERE run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                       (run["id"],)).fetchall()
    for row in rows:
        if row["task_id"] == task["id"]:
            # Handing the task's lease to a new holder (fix round, relaunch):
            # revoke the old one so a late submit from it is fenced out.
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='superseded' WHERE id=?", (now.isoformat(), row["id"]))
            continue
        if row["task_id"] in dependants:
            continue
        other = state.get_task(con, run["id"], row["task_id"]) if row["task_id"] else None
        other_scope = other["scope"] if other else json.loads(row["scope"] or "[]")
        if planfile.scopes_overlap(task["scope"], other_scope):
            raise Refused("scope-held", f"{task['id']} scope overlaps {row['task_id']}, which holds a live lease",
                          scope=task["id"], preserved="both tasks' work",
                          next_step=f"wait for {row['task_id']} to be accepted, or office dispatch {row['task_id']} {task['id']} (stacked)")
    fencing = (con.execute("SELECT COALESCE(MAX(fencing),0) FROM leases WHERE run_id=?", (run["id"],)).fetchone()[0] or 0) + 1
    lease_id = "L" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, task_id, fencing, dispatch_id, renewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lease_id, run["id"], role, dumps(task["scope"]), holder, now.isoformat(),
                 (now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(), task["id"], fencing, holder, now.isoformat()))
    con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], role, task["id"], None, holder, "acquire", now.isoformat()))
    return {"id": lease_id, "fencing": fencing}


def live_lease(con, run_id: str, lease_id: str) -> dict | None:
    row = con.execute("SELECT * FROM leases WHERE id=? AND run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                      (lease_id, run_id)).fetchone()
    return dict(row) if row else None


def renew_lease(con, lease_id: str) -> None:
    now = datetime.now(timezone.utc)
    con.execute("UPDATE leases SET expires_at=?, renewed_at=? WHERE id=?",
                ((now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(), now.isoformat(), lease_id))


def revoke(con, run: dict, target: str, reason: str) -> Result:
    """Revoke a task's lease, or end one dispatch (`D...`), or the composed
    result's reviews (`integration`)."""
    task_id = target.upper()
    if task_id == "INTEGRATION":
        return revoke_integration(con, run, reason)
    if state.get_task(con, run["id"], task_id) is None:
        did = target[:1].upper() + target[1:]
        d = state.get_dispatch(con, did)
        if d is not None and d["run_id"] == run["id"]:
            return revoke_dispatch(con, run, d, reason)
        raise Usage("unknown-task", f"no task, dispatch, or scope {target}",
                    next_step="office revoke <task id | dispatch id | integration>")
    return _revoke_task(con, run, task_id, reason)


def _revoke_task(con, run: dict, task_id: str, reason: str) -> Result:
    with db.transaction(con):
        task = state.get_task(con, run["id"], task_id)
        if task is None:
            raise Usage("unknown-task", f"no task {task_id}")
        # An accepted task whose relaunched worker never got a newer revision in (its submit was
        # refused, or it never submitted) still stands on its accepted revision: the revoke only
        # releases the lease.
        keeps = (task["status"] != "cancelled" and bool(task.get("accepted_revision_id"))
                 and task.get("current_revision_id") == task["accepted_revision_id"])
        if keeps:
            con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND task_id=? AND released_at IS NULL "
                        "AND revoked_at IS NULL", (now_iso(), run["id"], task_id))
            state.update_task(con, run["id"], task_id, status="accepted", pause_reason=None)
            state.emit(con, run, "lease.released", f"{task_id} worker revoked; lease released, "
                       f"{task_id} stays accepted on {task['accepted_revision_id']}", task_id=task_id)
        else:
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason=? WHERE run_id=? AND task_id=? AND released_at IS NULL "
                        "AND revoked_at IS NULL", (now_iso(), reason, run["id"], task_id))
            if task["status"] != "cancelled":  # a task the plan removed stays removed
                state.update_task(con, run["id"], task_id, status="paused", pause_reason=f"lease revoked: {reason}")
            state.emit(con, run, "lease.revoked", f"{task_id} lease revoked", task_id=task_id)
    live = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE run_id=? AND task_id=? AND ended_at IS NULL "
                                         "AND status IN ('launching', 'running')", (run["id"], task_id)).fetchall()]
    notes: list[str] = []
    stopped = [d["id"] for d in live if stop_dispatch(run, d, notes=notes)]
    lines = [f"{task_id} lease {'released' if keeps else 'revoked'} | later submits from its holder are rejected"]
    if keeps:
        lines.append(f"{task_id} stays accepted on {task['accepted_revision_id']}")
        unapplied = [r["amendment_id"] for r in con.execute(
            "SELECT DISTINCT amendment_id FROM deliveries WHERE run_id=? AND task_id=? AND status IN ('queued','delivered')",
            (run["id"], task_id)).fetchall()]
        if unapplied:
            lines.append(f"{', '.join(unapplied)} was not applied to {task_id}; office amend {task_id} -- \"<change>\" "
                         "delivers it again")
    if stopped:
        lines.append(f"stopped {', '.join(stopped)} (SIGTERM)")
    lines += notes
    return Result(lines=lines, next=(f'office amend {task_id} -- "<change>" to reopen it' if keeps
                                     else f"office dispatch {task_id} to relaunch"))


def _end_dispatch(con, run: dict, d: dict, classification: str, why: str, *, stop: bool = True,
                  notes: list[str] | None = None) -> bool:
    """Record the end of a dispatch Office can no longer wait on. Closes its
    gate when nothing else can finish it. Returns False when it had ended."""
    if d.get("ended_at") or d["status"] not in ("launching", "running"):
        return False
    if stop and d.get("launcher") not in (None, "external", "sync"):
        stop_dispatch(run, d, notes=notes)  # stops what is still alive and records the end itself
    if d.get("kind") in ("planner", "executor") and not state.get_dispatch(con, d["id"]).get("ended_at"):
        _finish(d["id"], None, None, classification, 0.0)  # a worker end has its own after-exit steps
    with db.transaction(con):
        # The end and the gate close commit together: a crash between them would
        # leave a running gate with nothing to finish it. `repair_orphaned_gates`
        # covers an end recorded elsewhere (stop_dispatch, an older Office).
        if not state.get_dispatch(con, d["id"]).get("ended_at"):
            con.execute("UPDATE dispatches SET status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END, "
                        "terminal_classification=?, ended_at=?, wall_clock_seconds=COALESCE(wall_clock_seconds, 0) "
                        "WHERE id=? AND ended_at IS NULL", (classification, now_iso(), d["id"]))
            state.emit(con, run, "dispatch.ended", f"{d.get('task_id') or d['role']} {d['role']} ended: {classification}",
                       audience="runtime", task_id=d.get("task_id"), dispatch_id=d["id"],
                       payload={"exit_code": None, "signal": None, "classification": classification})
        state.emit(con, run, "dispatch.reaped", f"{d['id']} {d['role']} ended: {why}", task_id=d.get("task_id"),
                   dispatch_id=d["id"])
        _close_orphaned_gate(con, run, d.get("gate_id"), why)
    return True


def repair_orphaned_gates(con, run: dict) -> list[str]:
    """Close open review gates whose reviewers have all ended and whose own job
    can no longer finish them. Caller holds the transaction."""
    rows = con.execute("SELECT id FROM gates g WHERE run_id=? AND status='running' "
                       "AND EXISTS (SELECT 1 FROM dispatches WHERE gate_id=g.id) "
                       "AND NOT EXISTS (SELECT 1 FROM dispatches WHERE gate_id=g.id AND ended_at IS NULL)",
                       (run["id"],)).fetchall()
    closed = []
    for r in rows:
        _close_orphaned_gate(con, run, r["id"], "its reviewer ended and no job is left to finish it")
        if con.execute("SELECT status FROM gates WHERE id=?", (r["id"],)).fetchone()[0] == "done":
            closed.append(r["id"])
    return closed


def _close_orphaned_gate(con, run: dict, gate_id: str | None, why: str) -> None:
    """A review gate whose reviewer ended and whose job is dead has nothing
    left to finish it. Caller holds the transaction."""
    if not gate_id:
        return
    from office import gates
    if con.execute("SELECT 1 FROM dispatches WHERE gate_id=? AND ended_at IS NULL", (gate_id,)).fetchone():
        return
    gate = con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
    if gate is None:
        return
    for job in gates.owning_jobs(con, run, gate):
        if job["status"] == "queued" or claim_alive(job["claimed_pid"], job["claimed_by"]):
            return  # the gate's own job may still run or retry the review
    gates.mark_unavailable(con, run, gate_id, why)


def revoke_dispatch(con, run: dict, d: dict, reason: str) -> Result:
    if d.get("task_id") and d.get("role") in ("executor", "planner"):
        task = state.get_task(con, run["id"], d["task_id"])
        live = not d.get("ended_at") and d["status"] in ("launching", "running")
        if task is None or task.get("current_dispatch_id") != d["id"] or not live:
            why = "ended" if not live else f"not {d['task_id']}'s current dispatch ({task and task.get('current_dispatch_id')})"
            return Result(lines=[f"{d['id']} is {why}; nothing revoked"],
                          next=f"office revoke {d['task_id']} to revoke the task itself")
        return _revoke_task(con, run, d["task_id"], reason)
    notes: list[str] = []
    ended = _end_dispatch(con, run, d, "revoked", f"revoked: {reason}", notes=notes)
    line = f"{d['id']} {'ended' if ended else 'had already ended'} ({reason})"
    return Result(lines=[line, *notes], next="office status")


def _fence_integrate_jobs(con, run: dict, reason: str) -> list[str]:
    """Stop and cancel the integrate jobs that own the integration reviews, so
    ending a reviewer cannot make a live job start a fallback or retry."""
    with db.transaction(con):
        jobs_ = [dict(r) for r in con.execute("SELECT id, status, claimed_pid, claimed_by FROM outbox WHERE run_id=? "
                                              "AND kind='integrate' AND status IN ('queued','claimed')",
                                              (run["id"],)).fetchall()]
        for j in jobs_:
            con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, claimed_pid=NULL, max_attempts=attempts "
                        "WHERE id=?", (f"revoked: {reason}"[:200], now_iso(), j["id"]))
    unstopped = []
    for j in jobs_:
        if j["status"] != "claimed" or not claim_alive(j["claimed_pid"], j["claimed_by"]):
            continue
        if not claim_signalable(j["claimed_pid"], j["claimed_by"]):
            unstopped.append(f"{j['id']} (pid {j['claimed_pid']})")  # no start time: the pid may be reused
            continue
        _killpg(j["claimed_pid"])
        deadline = time.time() + 5
        while claim_alive(j["claimed_pid"], j["claimed_by"]) and time.time() < deadline:
            time.sleep(0.05)
    return [j["id"] for j in jobs_], unstopped


def revoke_integration(con, run: dict, reason: str) -> Result:
    fenced, unstopped = _fence_integrate_jobs(con, run, reason)
    rows = [dict(r) for r in con.execute(
        "SELECT d.* FROM dispatches d JOIN gates g ON g.id=d.gate_id WHERE d.run_id=? AND g.subject='integration' "
        "AND d.ended_at IS NULL AND d.status IN ('launching','running')", (run["id"],)).fetchall()]
    notes: list[str] = []
    ended = [d["id"] for d in rows if _end_dispatch(con, run, d, "revoked", f"integration revoked: {reason}", notes=notes)]
    from office import gates
    with db.transaction(con):
        stale = [r["id"] for r in con.execute("SELECT id FROM gates WHERE run_id=? AND subject='integration' "
                                              "AND status IN ('queued','running','waiting')", (run["id"],)).fetchall()]
        for gid in stale:
            _close_orphaned_gate(con, run, gid, f"integration revoked: {reason}")
        if fenced:  # a compose was cut short; a finished integration keeps its verdict
            from office import integration
            integration._set_integration(con, state.get_run(con, run["id"]), status="blocked",
                                         detail=f"integration revoked: {reason}; office resume re-runs it")
        state.emit(con, run, "integration.revoked", f"integration reviews revoked: {reason}")
    lines = [f"integration revoked | ended {', '.join(ended) if ended else 'no live dispatch'}"
             + (f" | cancelled job {', '.join(fenced)}" if fenced else "")]
    if unstopped:
        lines.append(f"not signalled (claimed by an older Office, so the pid may belong to another process now): "
                     f"{', '.join(unstopped)}; stop it by hand if it is still the integrate job")
    lines += notes
    return Result(lines=lines, next="office status (office resume re-runs a blocked integration)")


# Reap only a reviewer that is certainly gone: its supervisor is dead, herdr
# says its agent does not exist, and it left no reply. Unknown is never gone.
REAP_GRACE_SECONDS = 60


def reap_orphans(con, run: dict) -> list[str]:
    """End review dispatches whose process and herdr agent are gone (a host
    reboot leaves them `running` forever). Probes herdr, so call it outside a
    transaction. Returns one note per dispatch reaped."""
    from office import rerun
    notes = []
    for row in con.execute("SELECT * FROM dispatches WHERE run_id=? AND launcher='herdr' AND gate_id IS NOT NULL "
                           "AND ended_at IS NULL AND status='running'", (run["id"],)).fetchall():
        d = dict(row)
        if pid_alive(d.get("pid")):
            continue
        started = parse_iso(d.get("launched_at") or d["started_at"])
        if (datetime.now(timezone.utc) - started).total_seconds() < REAP_GRACE_SECONDS:
            continue
        reply = paths.run_dir(run["id"]) / "dispatches" / d["id"] / "reply.txt"
        if reply.is_file() and reply.stat().st_size > 0:
            continue  # the review finished; its reader will record it
        if rerun.agent_alive(d) is not False:
            continue  # alive, or herdr could not say
        if _end_dispatch(con, run, d, "lost", "its process and herdr agent are gone (no reply)", stop=False):
            notes.append(f"{d['id']} reviewer is gone")
    with db.transaction(con):
        notes += [f"gate {g} closed: its reviewer had ended" for g in repair_orphaned_gates(con, run)]
    return notes


def _agent_pgid_file(run: dict, dispatch_id: str) -> Path:
    return paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "agent.pgid"


def _identity_file(run_id: str, dispatch_id: str, which: str) -> Path:
    return paths.run_dir(run_id) / "dispatches" / dispatch_id / f"{which}.identity"


def _record_identity(run_id: str, dispatch_id: str, which: str, pid: int) -> None:
    """Record which process instance `pid` is (`supervisor` or `agent`), so a
    later stop never signals another process that reused the pid."""
    path = _identity_file(run_id, dispatch_id, which)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"pid": pid, "start": process_start(pid)})


def _verified(run: dict, dispatch_id: str, which: str, pid: int | None) -> bool:
    """Whether `pid` is provably the recorded `which` process of this dispatch.
    No record (an older Office) or another start time means no."""
    try:
        rec = json.loads(_identity_file(run["id"], dispatch_id, which).read_text())
    except (OSError, ValueError):
        return False
    return rec.get("pid") == pid and process_is(pid, rec.get("start"))


def _killpg(pid, sig=signal.SIGTERM) -> bool:
    if not pid or pid <= 0 or pid == os.getpid() or pid == os.getpgrp():
        return False
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            return False
    return True


def stop_dispatch(run: dict, d: dict, *, wait: float = 5.0, notes: list[str] | None = None) -> bool:
    """SIGTERM a live dispatch's supervisor and agent process groups, close its
    herdr pane, and make sure the end is recorded as `signal`. A saved pid is
    signalled only when its recorded start time proves it is still that
    process; otherwise nothing is sent and `notes` names the pid to recover
    by hand."""
    if d.get("launcher") in (None, "external", "sync"):
        return False  # not under Office's process control
    sup = d.get("pid") if pid_alive(d.get("pid")) else None
    pgid_file = _agent_pgid_file(run, d["id"])
    agent = None
    if pgid_file.is_file():
        try:
            agent = int(pgid_file.read_text().strip())
        except ValueError:
            agent = None
    if agent is not None and not pid_alive(agent):
        agent = None
    unverified = []
    if sup and not _verified(run, d["id"], "supervisor", sup):
        unverified.append(f"supervisor pid {sup}")
        sup = None
    if agent and not _verified(run, d["id"], "agent", agent):
        unverified.append(f"agent process group {agent}")
        agent = None
    if unverified:
        text = (f"not signalled: {', '.join(unverified)} cannot be proven to still be this dispatch's process (no or "
                "another recorded start time); manual recovery needed: check it with ps and stop it by hand")
        _launch_notice(run, d, text)
        if notes is not None:
            notes.append(f"{d['id']} {text}")
    signalled = _killpg(sup)
    signalled = _killpg(agent) or signalled
    deadline = time.time() + wait
    while sup and pid_alive(sup) and time.time() < deadline:
        time.sleep(0.05)  # let the supervisor record its own `signal` end
    if d.get("launcher") == "herdr" and d.get("pane_id") and shutil.which("herdr"):
        reclaim_pane(run, d["id"], explicit=True)  # snapshot, then close
        signalled = True
    con = db.connect()
    try:
        ended = state.get_dispatch(con, d["id"]).get("ended_at")
    finally:
        con.close()
    if not ended:
        # The supervisor was already gone or did not get to record the end.
        _finish(d["id"], None, int(signal.SIGTERM), "signal", 0.0)
    return signalled or not ended


# ------------------------------------------------------------------ launch job

def ensure_worktree(run: dict, dispatch: dict) -> Path:
    wt = Path(dispatch["worktree"])
    repo = Path(run["repo_root"])
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    branches = paths.git(repo, "branch", "--list", dispatch["branch"])
    if branches.strip():
        paths.git(repo, "worktree", "add", str(wt), dispatch["branch"])
    else:
        paths.git(repo, "worktree", "add", "-b", dispatch["branch"], str(wt), dispatch["base_commit"])
    exclude = Path(paths.git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if ".office/" not in text.split():
            with exclude.open("a") as fh:
                fh.write("\n.office/\n")
    except OSError:
        pass
    return wt


def build_packet(con, run: dict, dispatch: dict, role: str, extra: dict) -> dict:
    task = state.get_task(con, run["id"], dispatch["task_id"])
    plan = state.current_plan(con, run["id"])
    req = state.current_requirements(con, run["id"])
    body = {
        "role": role,
        "dispatch_id": dispatch["id"],
        "task_id": task["id"],
        "title": task["title"],
        "scope": task["scope"],
        "depends": task["depends"],
        "accept": task["accept"],
        "checks": task["checks"],
        "visual": task["visual"],
        "requirements_version": run["requirements_version"],
        "plan_version": plan["version"] if plan else 0,
        "contract_version": task["contract_version"],
        "lease_id": dispatch["lease_id"],
        "base_commit": dispatch["base_commit"],
        "base_merge": _base_merge_of(run, dispatch["base_commit"]),
        "worktree": dispatch["worktree"],
        "route": dispatch["route"].get("selection_disclosure"),
        "branch": dispatch.get("branch"),
        "pr": _pr_packet(con, run, task, dispatch) if role == "executor" else None,
        "requirements": req["frozen"],
        "fix_of": extra.get("fix_of"),
        "contract_request": extra.get("contract_request"),
        "restack": extra.get("restack"),
    }
    return state.packet_envelope(run, f"{role}-dispatch", body)


def _base_merge_of(run: dict, base: str) -> str | None:
    """The tasks an Office merge commit base combines ("T1, T2"), or None for any other base."""
    out = paths.git(run["repo_root"], "log", "-1", "--format=%P%x00%s", base, check=False)
    parents, _, subject = out.partition("\0")
    m = re.fullmatch(r"office: base of T\d+, a merge of (T\d+(?:, T\d+)*)", subject)
    return m.group(1) if m and len(parents.split()) == 2 else None


def _pr_packet(con, run: dict, task: dict, dispatch: dict) -> dict | None:
    """What the executor needs to push and open its draft PR (3.2), or None."""
    from office import prs
    if not prs.enabled(run) or not prs.has_pr(task):
        return None
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    body_path = ddir / "pr-body.md"
    body_path.write_text(prs.body(con, run, task, dispatch), encoding="utf-8")
    return {"push": f"git push -u origin HEAD:refs/heads/{dispatch['branch']}",
            "open": None if (task.get("pr") or {}).get("number") else prs.create_command(con, run, task, dispatch, body_path),
            "number": (task.get("pr") or {}).get("number")}


def _set_aside_evidence(con, run: dict, dispatch: dict, wt: Path, ddir: Path) -> None:
    """A scope-none task's worktree is reused across dispatches. Any evidence file
    already there (left by a submitted round or a crashed one) is moved out before
    the agent starts, so a file present at submit was written by this dispatch.
    The file is renamed, never read: it may be a link to anything."""
    task = state.get_task(con, run["id"], dispatch["task_id"])
    src = wt / briefs.EVIDENCE_FILE
    if task is None or task["scope"] or not (src.exists() or src.is_symlink()):
        return
    if paths.git(wt, "ls-files", "--", briefs.EVIDENCE_FILE).strip():
        return  # a tracked file is repo content (submit never ingests it); moving it would delete it
    dest = ddir / "stale-evidence.md"
    n = 1
    while dest.exists() or dest.is_symlink():  # a retried launch job keeps every copy it moved
        n += 1
        dest = ddir / f"stale-evidence-{n}.md"
    os.rename(src, dest)


def job_launch_agent(con, run: dict, job: dict) -> dict:
    payload = job["payload"]
    dispatch = state.get_dispatch(con, payload["dispatch_id"])
    if dispatch["status"] not in ("launching",):
        return {"skipped": dispatch["status"]}
    created = not (Path(dispatch["worktree"]) / ".git").exists()
    wt = ensure_worktree(run, dispatch)
    role = payload["role"]
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    setup = None
    if role == "executor":
        _set_aside_evidence(con, run, dispatch, wt, ddir)
        # The repo's own install, before the agent's first prompt; a failure is surfaced, never fatal.
        setup = worktree_setup.prepare(run, wt, "task", ddir / "setup.log", created=created,
                                       task_id=dispatch["task_id"], dispatch_id=dispatch["id"])
        if setup:
            atomic_write_json(ddir / "setup.json", setup)
            if setup["exit"] != 0:
                _launch_notice(run, dispatch, worktree_setup.failure_text(setup))
    packet = build_packet(con, run, dispatch, role, payload)
    state.check_packet(run, packet)
    atomic_write_json(ddir / "packet.json", packet)
    brief = briefs.worker_brief(con, run, packet, setup=setup)
    (ddir / "brief.md").write_text(brief, encoding="utf-8")
    with db.transaction(con):
        con.execute("UPDATE dispatches SET packet_hash=?, packet_path=?, log_path=? WHERE id=?",
                    (packet["packet_hash"], str(ddir / "packet.json"), str(ddir / "output.log"), dispatch["id"]))
    launcher = launch(run, dispatch, "worker", ddir, cwd=wt, cli=payload.get("cli"),
                      external=bool(payload.get("external")), resume=payload.get("resume"))
    return {"dispatch_id": dispatch["id"], **launcher}


def worker_env(run: dict, dispatch: dict, role: str) -> dict:
    env = dict(os.environ)
    # Identity is always re-injected for this dispatch; configuration switches
    # (data homes, job/launcher modes, quota fixtures) carry through unchanged.
    for key in IDENTITY_ENV:
        env.pop(key, None)
    env.update({
        "OFFICE_RUN_ID": run["id"],
        "OFFICE_TASK_ID": dispatch["task_id"] or "",
        "OFFICE_DISPATCH_ID": dispatch["id"],
        "OFFICE_ROLE": role,
        "OFFICE_VERSION": version.current(),
    })
    argv, extra = frontdoor.current_argv()
    env.update(extra)
    shim = paths.data_home() / "bin"
    if (shim / "office").exists():
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    return env


def herdr_usable() -> bool:
    """Whether launches here open Herdr panes (a native resume needs one)."""
    return bool(os.environ.get("OFFICE_LAUNCHER", "auto") in ("auto", "herdr")
                and os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr"))


def launch(run: dict, dispatch: dict, kind: str, ddir: Path, *, cwd: Path, wait: bool = False,
           output: Path | None = None, images: list[Path] | None = None, include_dirs: list[Path] | None = None,
           prompt_file: Path | None = None, cli: str | None = None, external: bool = False,
           resume: dict | None = None) -> dict:
    """Start `office _supervise` for a dispatch, in a Herdr pane when running
    inside Herdr (visible delegation), else as a detached process."""
    spec = {"dispatch_id": dispatch["id"], "kind": kind, "cwd": str(cwd), "output": str(output) if output else None,
            "images": [str(i) for i in images or []], "include_dirs": [str(d) for d in include_dirs or []],
            "prompt_file": str(prompt_file or ddir / "brief.md"), "log_path": str(ddir / "output.log")}
    # The supervisor always finds its spec here, whatever directory holds the brief.
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET log_path=COALESCE(log_path, ?) WHERE id=?", (spec["log_path"], dispatch["id"]))
    finally:
        con.close()
    if not (cli or resume):
        _assign_session(dispatch)
    argv, extra = frontdoor.current_argv()
    sup = argv + ["_supervise", dispatch["id"]]
    env = dict(os.environ)
    env.update(extra)
    env.pop(frontdoor.HOP_ENV, None)
    launcher = os.environ.get("OFFICE_LAUNCHER", "auto")
    use_herdr = herdr_usable()
    if cli and not use_herdr:
        _launch_notice(run, dispatch, f"--cli needs a herdr session; left external instead. Start it by hand: {cli}")
        external = True
    if resume and output:
        resume = _resume_with_reply_access(dispatch, kind, resume, cwd, include_dirs, output)
    if resume and not use_herdr:
        # A native resume reopens the session in a pane; never fall back to a
        # fresh headless session under the resume's name (R7).
        _launch_notice(run, dispatch, "resume needs a herdr session; left external. Start it by hand: "
                       f"{resume.get('herdr_kind')} {shlex.join(resume.get('argv') or [])}")
        external = True
    if resume:
        spec["resume_findings"] = resume.get("findings") or ""
    if external or (kind == "worker" and os.environ.get("OFFICE_WORKER_LAUNCHER") == "external"):
        # Hosted outside Office's process control (an interactive session the
        # user or a test drives). A worker is live until it submits; a reviewer
        # ends when its output file is written.
        return _launch_external(run, dispatch, kind, spec, ddir, sup, env, cwd, wait=wait, announce=external)
    if launcher == "sync":
        # Deterministic mode for tests and fixtures: supervise in the foreground.
        _record_launch(run, dispatch["id"], launcher="sync", pid=os.getpid())
        _supervise_in_process(dispatch["id"], cwd, extra)
        return _wait_terminal(dispatch["id"], timeout=5) if wait else {"launcher": "sync"}
    headless = "process"
    if use_herdr:
        if resume:
            # office rerun --resume: the harness's own resume argv for the parent's session.
            inter = (list(resume.get("argv") or []), resume.get("herdr_kind") or kind)
        elif cli:
            # The user's exact argv; herdr supplies the executable from --kind.
            argv_cli = shlex.split(cli)
            inter = (argv_cli[1:], Path(argv_cli[0]).name)
        else:
            inter = _interactive(dispatch, kind, cwd, include_dirs, output=output)
        label = pane_label(run, dispatch, kind)
        pane = _herdr_pane(run, cwd, label=label, dispatch_id=dispatch["id"]) if inter else None
        if inter and not pane:
            _launch_notice(run, dispatch, "no herdr pane could be opened; running headless instead")
        if pane:
            started = _herdr_agent_start(run, dispatch, spec, env, inter, pane, cwd, ddir, label=label)
            if started:
                if wait:
                    return _wait_terminal(dispatch["id"])
                return started
            _release_panes(run, dispatch["id"])
            if cli:
                # Running the adapter's own argv headless would not be what the user asked for.
                _launch_notice(run, dispatch, f"--cli agent did not start in herdr; left external. Start it by hand: {cli}")
                return _launch_external(run, dispatch, kind, spec, ddir, sup, env, cwd, wait=wait, announce=False)
            # The agent never came up in the pane: fall back to a plain process,
            # and keep that visible on the dispatch (its notice says why).
            headless = "process-fallback"
    log = open(ddir / "supervisor.log", "ab")
    try:
        proc = subprocess.Popen(sup, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    finally:
        log.close()
    _record_launch(run, dispatch["id"], launcher=headless, pid=proc.pid)
    if wait:
        proc.wait()
        return _wait_terminal(dispatch["id"])
    return {"launcher": headless, "pid": proc.pid}


def _supervise_in_process(dispatch_id: str, cwd: Path, extra: dict) -> None:
    """Run the supervisor here, under the environment and directory the
    `office _supervise` child would have had, and put both back afterwards."""
    from unittest import mock
    with mock.patch.dict(os.environ, extra), contextlib.chdir(cwd):
        os.environ.pop(frontdoor.HOP_ENV, None)
        supervise(dispatch_id)


def _spawn_agent(argv: list[str], cwd: str, env: dict, stdin: int) -> subprocess.Popen:
    """Start the harness process in its own session, its output piped back.
    The one place a harness child is created, so tests can replace it."""
    return subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)


def _launch_external(run: dict, dispatch: dict, kind: str, spec: dict, ddir: Path, sup: list[str], env: dict,
                     cwd: Path, *, wait: bool, announce: bool) -> dict:
    worker = kind == "worker"
    write_agent_env(run, dispatch, ddir, worker=worker)
    _record_launch(run, dispatch["id"], launcher="external")
    if announce:
        lines = launch_instructions(run, state_dispatch(dispatch["id"]), output=None if worker else spec.get("output"))
        _launch_notice(run, dispatch, "external " + ("executor" if worker else "reviewer") + ": " + " | ".join(lines))
    if worker:
        return {"launcher": "external"}
    # A reviewer: a detached watcher ends the dispatch once the output file is written.
    spec["external"] = True
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    log = open(ddir / "supervisor.log", "ab")
    try:
        watcher = subprocess.Popen(sup, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                   start_new_session=True, close_fds=True)
    finally:
        log.close()
    if wait:
        return _wait_terminal(dispatch["id"])
    return {"launcher": "external", "watcher_pid": watcher.pid}


def state_dispatch(dispatch_id: str) -> dict:
    con = db.connect()
    try:
        return state.get_dispatch(con, dispatch_id)
    finally:
        con.close()


def watch_external_output(dispatch_id: str, spec: dict, *, poll: float | None = None) -> tuple:
    """(exit_code, classification) for an externally hosted reviewer: done once
    its output file is written and stops growing, or when the dispatch is
    cancelled (revoke). It waits on a person, so it has no timeout."""
    poll = float(os.environ.get("OFFICE_HERDR_POLL", "5")) if poll is None else poll
    output = Path(spec["output"])
    last = None
    while True:
        d = state_dispatch(dispatch_id)
        if d["status"] in ("exited", "failed", "cancelled"):
            return None, "cancelled" if d["status"] == "cancelled" else "duplicate_ignored"
        size = output.stat().st_size if output.is_file() else 0
        if size and size == last:
            return 0, "success"
        if not size:
            # A read-only reviewer (codex --sandbox read-only, claude without
            # Write) cannot create the file its brief names; the pointer tells
            # it to end its reply with the review, and its session transcript
            # keeps that reply. The person may start it in any directory, so
            # the brief path alone identifies the session.
            reply = _external_transcript_reply(d, spec)
            if reply:
                output.write_text(reply, encoding="utf-8")
                return 0, "success"
        last = size or None
        time.sleep(poll)


_VERDICT_LINE = re.compile(r"^\W*VERDICT\s*:", re.M)


def _external_transcript_reply(d: dict, spec: dict) -> str | None:
    """An external reviewer's final reply from its transcript, once it holds a
    VERDICT line (an earlier progress message is not the review)."""
    reply = transcript_reply(d, {**spec, "cwd": None})
    return reply if reply and _VERDICT_LINE.search(reply) else None


def _wall_cap_seconds(prof: dict) -> float | None:
    """The hard wall-clock limit for a headless agent: OFFICE_WORKER_MAX_MINUTES,
    else the profile's max_minutes, else none (headless agents are often silent
    until they finish, so there is no idle rule)."""
    raw = os.environ.get("OFFICE_WORKER_MAX_MINUTES") or prof.get("max_minutes")
    try:
        return float(raw) * 60 if raw else None
    except (TypeError, ValueError):
        return None


def _interactive(dispatch: dict, kind: str, cwd: Path, include_dirs: list[Path] | None = None,
                 output: Path | None = None) -> tuple[list[str], str] | None:
    """The pane-hosted form of this dispatch's harness, or None (headless)."""
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or "")
    if not adapter or not dispatch.get("model"):
        return None
    return adapters.interactive_argv(adapter, kind, model=dispatch["model"], effort=dispatch.get("effort") or "none",
                                     cwd=cwd, include_dirs=include_dirs, output=output,
                                     session_id=_assigned_session(dispatch))


def write_agent_env(run: dict, dispatch: dict, ddir: Path, *, worker: bool = True) -> Path:
    """agent.env: the dispatch identity a pane-hosted agent sources before it
    starts, whether Office or the user launches it."""
    wenv = worker_env(run, dispatch, dispatch.get("role") if worker else "reviewer")
    if not worker:
        # Reviewers get no Office authority: they cannot submit or ack.
        for k in ("OFFICE_TASK_ID", "OFFICE_DISPATCH_ID"):
            wenv.pop(k, None)
    carry = {k: v for k, v in wenv.items() if k.startswith(("OFFICE_", "AUTO_OFFICE_", "XDG_")) or k in ("PYTHONPATH", "PATH")}
    env_file = ddir / "agent.env"
    env_file.write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(carry.items())))
    env_file.chmod(0o600)
    return env_file


def cwd_owner(con, run_id: str, cwd: str) -> dict | None:
    """The dispatch of this run whose worktree holds `cwd` (the deepest worktree wins)."""
    here, best = os.path.realpath(cwd), None
    for r in con.execute("SELECT id, task_id, role, worktree FROM dispatches WHERE run_id=? AND worktree IS NOT NULL "
                         "ORDER BY started_at", (run_id,)).fetchall():
        wt = os.path.realpath(r["worktree"])
        if (here == wt or here.startswith(wt.rstrip(os.sep) + os.sep)) and (best is None or len(wt) >= best[0]):
            best = (len(wt), dict(r))
    return best[1] if best else None


def printable(text: str) -> str:
    """A herdr-reported path as one safe line for a notice or message (control characters and
    newlines collapsed, length capped); comparisons use the raw path."""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:200]


def _who(dispatch_id: str, task_id: str | None = None) -> str:
    """`D1 (T1)`: a dispatch with its task, looked up when not given."""
    if task_id is None:
        try:
            con = db.connect()
            try:
                row = con.execute("SELECT task_id FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            finally:
                con.close()
            task_id = row["task_id"] if row else None
        except Exception:  # a name in a notice must never fail a launch
            task_id = None
    return f"{dispatch_id} ({task_id})" if task_id else dispatch_id


def _pane_mismatch(run: dict, dispatch: dict, pane: str, cwd: Path, *, check_cwd: bool,
                   agent: str | None = None) -> str | None:
    """Why `pane` is not this dispatch's, or None. The run's layout says which dispatch the
    pane is reserved for; herdr says where it actually is and which agent runs in it (`agent`,
    the name launched for this dispatch, is checked when given). A pane herdr cannot describe
    (or one that reports no cwd or agent) cannot be disproved, so only a stated mismatch refuses."""
    me = _who(dispatch["id"], dispatch.get("task_id"))
    holder = _reserved_by(run, pane)
    if holder and holder != dispatch["id"]:
        return f"pane {pane} is reserved for dispatch {_who(holder)}, not for {me}"
    if not check_cwd and not agent:
        return None
    info = _herdr_json(["pane", "get", pane]).get("pane") or {}
    actual = info.get("cwd") if check_cwd else None
    if actual:
        here, want = os.path.realpath(actual), os.path.realpath(cwd)
        if not (here == want or here.startswith(want.rstrip(os.sep) + os.sep)):
            owner = None
            try:
                con = db.connect()
                try:
                    owner = cwd_owner(con, run["id"], actual) if run.get("id") else None
                finally:
                    con.close()
            except Exception:
                pass
            other = f"dispatch {_who(owner['id'], owner['task_id'])}" if owner else "no dispatch worktree of this run"
            return f"pane {pane} is in {printable(actual)}, which belongs to {other}, not {me}'s worktree {cwd}"
    running = info.get("agent")
    running = running.get("name") if isinstance(running, dict) else running
    if agent and running and running != agent:
        return f"pane {pane} runs agent {printable(str(running))}, not {agent} launched for {me}"
    return None


def _herdr_agent_start(run: dict, dispatch: dict, spec: dict, env: dict, inter: tuple[list[str], str], pane: str,
                       cwd: Path, ddir: Path, *, retried: bool = False, label: str | None = None) -> dict | None:
    """Start the real harness in the pane with `herdr agent start`, hand it a
    one-line brief pointer, and leave a detached watcher to record the end.
    The pane runs the agent itself, never a shell wrapper around it."""
    args, herdr_kind = inter
    name = herdr_agent_name(dispatch["id"])
    worker = spec["kind"] == "worker"
    mismatch = _pane_mismatch(run, dispatch, pane, cwd, check_cwd=False)
    if mismatch:  # before anything is typed into it
        _launch_notice(run, dispatch, f"{mismatch}; nothing was typed into it; running headless instead")
        return None
    # The pane's shell does not inherit this process's environment: source the
    # dispatch identity into it first, so the agent's own `office submit` works.
    env_file = write_agent_env(run, dispatch, ddir, worker=worker)
    setup = f". {shlex.quote(str(env_file))} && cd {shlex.quote(str(cwd))}"
    if not _shell_run(pane, setup, ddir / "shell-ready"):
        _launch_notice(run, dispatch, f"the shell in pane {pane} never ran Office's setup line (env and cd) within "
                                      f"{_shell_timeout():g}s; running headless instead")
        return None
    try:
        proc = subprocess.run(["herdr", "agent", "start", name, "--kind", herdr_kind, "--pane", pane, "--", *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        _launch_notice(run, dispatch, _agent_start_failure(herdr_kind, pane, str(exc)))
        return None
    if proc.returncode != 0:
        why = (proc.stdout or proc.stderr or "").strip()[:200]
        if "agent_pane_busy" in why and not retried:
            # The pane still holds an agent (a finished session herdr keeps):
            # split a fresh one and try once more before going headless (#200 B7).
            fresh = _herdr_fresh_pane(run, cwd, pane, dispatch["id"])
            if fresh:
                return _herdr_agent_start(run, dispatch, spec, env, inter, fresh, cwd, ddir, retried=True, label=label)
        _launch_notice(run, dispatch, _agent_start_failure(herdr_kind, pane, why))
        return None
    spec.update({"herdr_agent": name, "pane": pane})
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    _record_launch(run, dispatch["id"], launcher="herdr", pane_id=pane)
    # A PR recorded between the first label and the pane id landing is missed by relabel_task_panes.
    fresh_label = pane_label(run, dispatch, dispatch.get("kind") or "")
    if fresh_label != label or retried:
        _herdr_rename(pane, fresh_label)
    # An id Office assigned at launch is already recorded; only a harness that
    # takes none needs herdr to report the one it started.
    session = dispatch.get("session_id")
    if not session:
        session = _started_session(proc.stdout) or _capture_session(name)
        if session:
            _record_session(run, dispatch, session, source="herdr")
    if not session:
        # Not a launch failure: the agent runs. Recorded where `office inspect` shows it.
        con = db.connect()
        try:
            with db.transaction(con):
                state.emit(con, run, "launch.session", f"{dispatch.get('task_id') or dispatch['id']}: herdr reported "
                           f"no session id for {name}; office rerun --resume will refuse it", audience="runtime",
                           task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"])
        finally:
            con.close()
    _pane_ledger(run, dispatch, pane, agent=name, kind=herdr_kind, worktree=cwd, session_id=session)
    # A long brief pasted as the prompt does not land; a one-line pointer does.
    if worker and spec.get("resume_findings") is not None:
        # A resumed session already knows the task; hand it the findings and the updated brief.
        pointer = (f"Office resumed this session. Open findings: {spec['resume_findings'][:600]} "
                   f"The updated brief is at {spec['prompt_file']}. Fix them, then run: office submit")
    elif worker:
        pointer = (f"Read and carry out the brief at {spec['prompt_file']} exactly. "
                   "When the work and its checks are complete, run: office submit")
    else:
        images = f" Inspect each evidence image: {' '.join(spec['images'])}." if spec.get("images") else ""
        pointer = (f"Read and carry out the review brief at {spec['prompt_file']} exactly.{images} "
                   f"Write your complete review to {spec['output']}; Office reads only that file, never "
                   "your terminal. Do not edit anything else.")
    from office import transcripts
    sent_at = time.time()

    def seen() -> bool:
        try:
            return transcripts.prompt_seen(dispatch.get("harness") or herdr_kind, marker=spec["prompt_file"],
                                           cwd=_session_cwd(spec, dispatch.get("harness") or herdr_kind, cwd), since=sent_at)
        except Exception:  # a landing probe must never abort the launch
            return False

    mismatch = _pane_mismatch(run, dispatch, pane, cwd, check_cwd=True, agent=name)
    if mismatch:
        # The pane is not the one reserved for this dispatch: typing the pointer could brief another task's agent.
        landed = False
        _launch_notice(run, dispatch, f"brief pointer NOT sent: {mismatch}. Nothing was typed into the pane; "
                                      f"the agent started in it is still running: revoke it with "
                                      f"office revoke {dispatch.get('task_id') or dispatch['id']}, or once it is "
                                      f"sorted out: office prompt {dispatch['id']} -- {shlex.quote(pointer)}")
    else:
        landed = _deliver_prompt(name, pane, pointer, answer_trust=_office_owned(run, cwd), seen=seen)
    if not landed and not mismatch:
        # The agent is up in a pane the user can see; a second headless copy
        # would race it. Say so and leave the pane for a manual re-prompt.
        view = _pane_view(name)
        if _composer_holds(view, pointer):
            # Prompting again would submit the pointer twice.
            _launch_notice(run, dispatch, f"brief pointer is typed but unsubmitted in herdr agent {name} (pane "
                                          f"{pane}); submit it: herdr pane send-keys {pane} Enter")
        else:
            screen = _startup_screen(view)
            title = "folder-trust dialog" if screen == "Trust this folder" else f"'{screen}' screen"
            why = (f" the {title} holds the composer; review or skip it in the pane and"
                   if screen else "")
            _launch_notice(run, dispatch, f"brief pointer did not land in herdr agent {name} (pane {pane});{why} "
                                          f"re-prompt it: office prompt {dispatch['id']} -- {shlex.quote(pointer)}")
    spec["prompt_landed"] = landed
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    log = open(ddir / "supervisor.log", "ab")
    try:
        watcher = subprocess.Popen(frontdoor.current_argv()[0] + ["_supervise", dispatch["id"]], cwd=str(cwd),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                   start_new_session=True, close_fds=True)
    finally:
        log.close()
    return {"launcher": "herdr", "pane": pane, "agent": name, "watcher_pid": watcher.pid, "prompt_landed": landed}


def _herdr_agent_sample(name: str) -> dict | None:
    """One `herdr agent get` sample: {"status", "content_hash"}, or None when
    the agent is gone (its process exited)."""
    try:
        proc = subprocess.run(["herdr", "agent", "get", name], capture_output=True, text=True, timeout=30)
        res = json.loads(proc.stdout or "{}").get("result") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        # Unreadable is not idle: busy stays unknown (None), which never settles.
        return {"status": "unknown", "content_hash": None, "busy": None}
    agent = res.get("agent") or res
    if proc.returncode != 0 or not agent:
        return None
    read = _herdr_agent_text(name)
    status = agent.get("status") or agent.get("agent_status")
    text_busy = None if read is None else _pane_busy(read)
    busy = None if read is None else (status == "working" or text_busy)
    return {"status": status, "content_hash": sha256_obj(read), "busy": busy, "text_busy": text_busy}


def _herdr_agent_text(name: str, *extra: str) -> str | None:
    """Pane text, or None when it could not be read."""
    try:
        proc = subprocess.run(["herdr", "agent", "read", name, *extra], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


# Footer text a harness shows only while a turn is running. agy's reported
# status is not a liveness signal (it reads idle mid-turn); its pane is.
BUSY_MARKERS = ("esc to cancel", "esc to interrupt")
# Current Claude Code shows no footer while working, only a spinner status
# line with an elapsed time: "✽ Harmonizing… (1m 2s)". Without this every
# working Claude pane read as idle (#200 B14).
_SPINNER = re.compile(r"(?:…|\.\.\.)\s*\(\s*(?:\d+h\s*)?(?:\d+m\s*)?\d+s\b")


# A Claude footer segment counting the pane's own background work:
# "· 1 shell, 3 monitors still running", "2 shells", "3 monitors". The pane is
# waiting on them, not stalled. Bare counts must be a whole segment so prose
# like "ran 1 shell command" does not match.
_BG_COUNTS = r"\d+ (?:shells?|monitors?)(?:, \d+ (?:shells?|monitors?))*"
_BACKGROUND = re.compile(
    rf"(?:^|[·•|])\s*{_BG_COUNTS}(?:\s+still running\s*$|\s*(?:[·•|]|$))", re.I | re.M)
# Only the footer region: a count scrolled up in the history is stale.
BACKGROUND_TAIL_LINES = 8


def _background_running(text: str) -> bool:
    tail = [ln for ln in (text or "").splitlines() if ln.strip()][-BACKGROUND_TAIL_LINES:]
    return bool(_BACKGROUND.search("\n".join(tail)))


def _pane_busy(text: str) -> bool:
    low = (text or "").lower()
    return (any(m in low for m in BUSY_MARKERS) or bool(_SPINNER.search(text or ""))
            or _background_running(text))


# Claude Code stops on its session limit and sits on a screen like
# "You've hit your session limit · resets 9:30pm (Asia/Manila)". The weekly
# "You've used 95% of your weekly limit" line is a warning, not a stop, and is
# deliberately not matched (#253).
_LIMIT_HIT = re.compile(r"(?:usage|session|5-hour) limit reached|hit your (?:session|usage) limit", re.I)
# "resets 9:30pm (Asia/Manila)" or the older "limit will reset at 9:30pm".
_LIMIT_RESETS = re.compile(r"\breset(?:s|\s+at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", re.I)
LIMIT_TAIL_LINES = 40
# Lines that show the turn moved past the limit: an echoed prompt ("> continue",
# not the composer box, which starts with a border) or new assistant output.
_LIMIT_ACTIVITY = re.compile(r"^\s*(?:[>❯]\s+\S|[●⏺]\s+\S)")
# How many non-blank lines above the limit line its fingerprint hashes, and what
# is stripped from them first: spinner glyphs and running timers or token
# counters, which tick on an otherwise unchanged screen.
LIMIT_CONTEXT_LINES = 15
_LIMIT_NOISE = re.compile(
    r"[·✢✳✶✻✽*⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
    r"|\(\s*(?:\d+h\s*)?(?:\d+m\s*)?\d+s\b[^)]*\)"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|h|m|s)\b"
    r"|[↑↓]\s*[\d.,]+\s*k?\s*tokens")


def _limit_fingerprint(lines: list[str]) -> str:
    """sha256 of the up to LIMIT_CONTEXT_LINES non-blank lines just above the
    limit line, normalized. A new limit after a `continue` has the echo and new
    output above it, so it hashes differently from the screen it replaced."""
    import hashlib
    kept = [ln for ln in (_LIMIT_NOISE.sub("", ln).rstrip() for ln in lines) if ln.strip()]
    return hashlib.sha256("\n".join(kept[-LIMIT_CONTEXT_LINES:]).encode()).hexdigest()


def _wall_to_utc(day, hour: int, minute: int, tz) -> datetime:
    """A wall-clock time on `day` in `tz` as UTC. `tz` None is the host's zone,
    resolved for that date (time.mktime with tm_isdst=-1), so its DST applies."""
    if tz is not None:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
    naive = datetime(day.year, day.month, day.day, hour, minute)
    return datetime.fromtimestamp(time.mktime((*naive.timetuple()[:8], -1)), timezone.utc)


def _usage_limit(text: str | None, now: datetime | None = None) -> dict | None:
    """The session-limit stop shown in the last pane lines, or None.
    {"resets_at": aware UTC datetime or None, "label": "9:30pm (Asia/Manila)",
    "tz": zone name, "local": "21:30", "fingerprint": hash of the screen above
    the limit line}. A reset without a zone is in the host's zone; a reset time
    already past rolls to the next day."""
    all_lines = (text or "").splitlines()
    offset = max(len(all_lines) - LIMIT_TAIL_LINES, 0)
    lines = all_lines[offset:]
    last = max((i for i, ln in enumerate(lines) if _LIMIT_HIT.search(ln)), default=None)
    if last is None:
        return None
    # A limit counts only while it is the latest state on screen: a prompt echo,
    # spinner, busy footer or new assistant output below it means the agent went on.
    if any(_LIMIT_ACTIVITY.search(ln) or _pane_busy(ln) for ln in lines[last + 1:]):
        return None
    out = {"resets_at": None, "label": None, "tz": None, "local": None,
           "fingerprint": _limit_fingerprint(all_lines[:offset + last])}
    found = next((m for m in map(_LIMIT_RESETS.search, lines[last:last + 3]) if m), None)
    if found is None:
        return out
    out["label"] = found.string[found.start(1):found.end()].strip()
    hour, minute, meridiem, zone = int(found.group(1)), int(found.group(2) or 0), found.group(3).lower(), found.group(4)
    if not 1 <= hour <= 12 or minute > 59:
        return out
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = None
    if zone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone.strip())
        except Exception:
            # An explicit zone that cannot be resolved is not the host's: never guess a time.
            return out
    today = now.astimezone(tz).date() if tz is not None else datetime.fromtimestamp(now.timestamp()).date()
    at = _wall_to_utc(today, hour, minute, tz)
    if at <= now:
        at = _wall_to_utc(today + timedelta(days=1), hour, minute, tz)
    local = at.astimezone(tz) if tz is not None else datetime.fromtimestamp(at.timestamp()).astimezone()
    out.update(resets_at=at, tz=getattr(tz, "key", None) or local.tzname() or "local",
               local=local.strftime("%H:%M"))
    return out


def herdr_agent_name(dispatch_id: str) -> str:
    """Herdr agent names must match [a-z][a-z0-9_-]{0,31}; dispatch ids start
    with an uppercase D, which herdr rejects."""
    return re.sub(r"[^a-z0-9_-]", "-", f"office-{dispatch_id}".lower())[:32]


# Codex opens a folder it has not been told to trust on a "Trust this folder?"
# dialog. Until it is answered the composer is empty, so a prompt sent then is
# lost while `agent prompt` succeeds and `agent get` can read `working`.
TRUST_DIALOG_MARKERS = ("trust this folder",)
# The ctx figure in a Claude status line ("ctx: 12k · $0.03"): it rises from
# 0k once a prompt reaches the model, which is a landed signal in a pane too
# narrow to show the busy footer.
_CTX_RE = re.compile(r"\bctx:\s*(\d+(?:\.\d+)?)\s*k\b", re.I)


def _trust_dialog(text: str | None) -> bool:
    low = (text or "").lower()
    return any(m in low for m in TRUST_DIALOG_MARKERS)


def _startup_screen(text: str | None) -> str | None:
    low = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text or "").lower()
    for marker, title in (("hooks need review", "Hooks need review"),
                          ("trust this folder", "Trust this folder"),
                          ("update available", "Update available")):
        if marker in low:
            return title
    return None


def _agent_start_failure(kind: str, pane: str, why: str) -> str:
    """Inspect the failed startup's pane without accepting any trust prompt."""
    screen = None
    if kind == "codex":
        try:
            proc = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "40"],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                screen = _startup_screen(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    detail = (f"; Codex is waiting on '{screen}' in pane {pane}. Review or skip that screen in the pane "
              "before the next Office launch" if screen else f"; inspect pane {pane}")
    return f"herdr agent start failed ({why[:200]}){detail}; running headless instead"


def _ctx_k(text: str | None) -> float | None:
    found = _CTX_RE.findall(text or "")
    return float(found[-1]) if found else None


def _pane_view(name: str) -> str | None:
    return _herdr_agent_text(name, "--source", "visible", "--lines", "40")


def _agent_up(pane: str, name: str | None = None) -> bool:
    """Herdr detects an agent in the pane, or reports the named agent in a
    known state: its UI is up, so typed text goes to the agent's composer and
    not to the pane's shell."""
    if (_herdr_json(["pane", "get", pane]).get("pane") or {}).get("agent"):
        return True
    if not name:
        return False
    res = _herdr_json(["agent", "get", name])
    agent = res.get("agent") or res
    status = (agent.get("status") or agent.get("agent_status")) if isinstance(agent, dict) else None
    return status in ("idle", "working", "blocked", "done")


def _landed_in(text: str | None, baseline_ctx: float | None) -> bool:
    if text is None or _startup_screen(text):
        return False
    if _pane_busy(text):
        return True
    ctx = _ctx_k(text)
    return ctx is not None and ctx > (baseline_ctx or 0)


def _prompt_landed(name: str, timeout: float, *, baseline_ctx: float | None = None, seen=None) -> str:
    """'landed' once the pane shows a busy footer, the Claude status line's ctx
    rises above its pre-prompt value, or the harness transcript logs the
    prompt (`seen`); 'trust' when a folder-trust dialog holds the composer;
    else '' at the deadline. `agent prompt` returning without error proves
    nothing, and neither does a `working` status: codex reports it while the
    trust dialog holds the composer empty, and agy reads `idle` mid-turn."""
    deadline = time.time() + timeout
    while True:
        text = _pane_view(name)
        if _startup_screen(text):
            return "trust"
        if _landed_in(text, baseline_ctx):
            return "landed"
        if seen is not None and seen():
            return "landed"
        if time.time() >= deadline:
            return ""
        time.sleep(1)


def _await_agent_ui(name: str, pane: str, timeout: float, *, answer_trust: bool, herdr) -> str:
    """Wait for the agent's UI before anything is sent. 'ready' once herdr sees
    the agent in the pane; 'trust' when a folder-trust dialog is up that Office
    may not answer; 'down' when no agent appeared by the deadline. A dialog on
    an Office-owned directory is answered with option 1 (Trust and continue)."""
    deadline = time.time() + timeout
    answered = False
    while True:
        text = _pane_view(name)
        if _startup_screen(text) and not _trust_dialog(text):
            return "trust"  # hook trust and updates always need a user decision
        if _trust_dialog(text):
            if not answer_trust or answered:
                if time.time() >= deadline:
                    return "trust"
            else:
                herdr("pane", "send-keys", pane, "Enter")
                answered = True
        elif _agent_up(pane, name):
            return "ready"
        if time.time() >= deadline:
            return "down"
        time.sleep(1)


def _office_owned(run: dict, cwd: Path | str | None) -> bool:
    """A directory Office created for this run (dispatch dirs, review checkouts,
    task worktrees): Office may answer a harness's trust prompt for it."""
    if not cwd:
        return False
    try:
        where = Path(cwd).resolve()
        roots = [paths.run_dir(run["id"]).resolve(), (paths.worktrees_dir() / run["id"][:8]).resolve()]
    except OSError:
        return False
    return any(where == r or r in where.parents for r in roots)


def _herdr_quiet(*args: str) -> None:
    """Run a herdr command whose failure must not escape: the agent is already
    recorded as launched, so a hung or missing herdr must not abort the caller."""
    try:
        subprocess.run(["herdr", *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _land_timeout() -> float:
    return float(os.environ.get("OFFICE_HERDR_LAND_TIMEOUT", "30"))


def _shell_timeout() -> float:
    return float(os.environ.get("OFFICE_HERDR_SHELL_TIMEOUT", "30"))


def _pane_tty(pid: int) -> str | None:
    """The terminal device of a pane's shell (`/dev/ttys005`, `/dev/pts/3`)."""
    try:
        name = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"/dev/{name}" if name and not name.startswith("?") else None


def _shell_at_line_editor(pane: str) -> bool | None:
    """Whether the pane's shell is reading a command line in its line editor:
    it owns the terminal and has switched it to raw input (no canonical mode,
    no echo), which zle, readline and fish do only once the rc files are done.
    False while the shell is still starting (an instant prompt keeps echo on)
    or a command is running; None when the terminal cannot be inspected."""
    if termios is None:
        return None
    info = _herdr_json(["pane", "process-info", "--pane", pane]).get("process_info") or {}
    pid = info.get("shell_pid")
    if not pid:
        return None
    if info.get("foreground_process_group_id") != pid:
        return False
    tty = _pane_tty(pid)
    if not tty:
        return None
    try:
        fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            lflag = termios.tcgetattr(fd)[3]
        finally:
            os.close(fd)
    except (OSError, termios.error):
        return None
    return not lflag & (termios.ICANON | termios.ECHO)


def _shell_run(pane: str, command: str, marker: Path, timeout: float | None = None) -> bool:
    """Run `command` in the pane's shell and confirm it ran. A freshly split
    pane draws its prompt before the shell reads input (zsh with an instant
    prompt), and a `pane run` sent then is dropped, queued until the rc files
    finish, or left typed on the line. The command touches `marker` once it has
    run; until it does, it is sent again, so it must be safe to run twice.

    A resend waits for the shell's line editor. While the shell is still
    starting, the first line is queued in the terminal, and a resend would
    queue a copy plus a Ctrl-U that cuts the queued lines apart, leaving a
    broken line (often an unclosed quote) that swallows the agent command
    `herdr agent start` types next. At the line editor, Ctrl-C clears a line
    left typed, including a continuation (`quote>`) that Ctrl-U cannot leave;
    it is safe there because the rc files are done. When the terminal cannot
    be inspected, the line is cleared with Ctrl-U (a SIGINT during the rc
    files would abort them) and sent again. False when it never ran within
    OFFICE_HERDR_SHELL_TIMEOUT."""
    marker.unlink(missing_ok=True)
    line = f"{command} && touch {shlex.quote(str(marker))}"
    retry = float(os.environ.get("OFFICE_HERDR_SHELL_RETRY", "3"))
    deadline = time.time() + (_shell_timeout() if timeout is None else timeout)
    first = True
    while True:
        if first:
            _herdr_quiet("pane", "run", pane, line)
        else:
            editor = _shell_at_line_editor(pane)
            if editor is not False:
                _herdr_quiet("pane", "send-keys", pane, "ctrl+c" if editor else "ctrl+u")
                _herdr_quiet("pane", "run", pane, line)
        first = False
        until = min(deadline, time.time() + retry)
        while True:
            if marker.exists():
                return True
            if time.time() >= until:
                break
            time.sleep(0.25)
        if time.time() >= deadline:
            return False


# Claude shows a long paste in its composer as this placeholder, not the text.
_PASTE_PLACEHOLDER = re.compile(r"\[Pasted text #\d+")
# The composer sits at the bottom of the pane; a long pointer wraps over many rows.
_COMPOSER_ROWS = 20
# A held pointer matches on any window this long of its letters and digits, so
# a composer that mangled its start (a dropped "Read and ") still counts.
_HELD_WINDOW = 24


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def _composer_holds(text: str | None, prompt: str) -> bool:
    """Any part of the prompt sits typed but unsubmitted near the bottom of the
    pane. Trailing blank rows are skipped, and only letters and digits are
    compared, so the composer's wrapping, borders and prompt glyph (`>`, `›`,
    `│`) do not hide it."""
    if not text:
        return False
    rows = text.rstrip().splitlines()
    tail = "\n".join(rows[-_COMPOSER_ROWS:])
    if _PASTE_PLACEHOLDER.search(tail):
        return True
    want, have = _squash(prompt), _squash(tail)
    size = min(len(want), _HELD_WINDOW)
    return bool(want) and any(want[i:i + size] in have for i in range(len(want) - size + 1))


def _press_enter(name: str, pane: str | None, herdr=_herdr_quiet) -> None:
    if pane:
        herdr("pane", "send-keys", pane, "Enter")
    else:
        herdr("agent", "send-keys", name, "Enter")


def _submit_held(name: str, pane: str | None, prompt: str, timeout: float, *, baseline_ctx: float | None = None,
                 seen=None, herdr=_herdr_quiet) -> str:
    """Submit a prompt left typed in the composer with Enter, never by sending
    the text again (that would submit it twice). `agent prompt` writes the text
    and Enter together; a TUI that is not ready to submit yet, or that takes the
    Enter as part of the paste, keeps the text. Returns 'landed'; 'absent' when
    the composer never held it; 'held' when it is still there after
    OFFICE_HERDR_ENTER_TRIES Enters; 'unknown' when the pane cannot be read
    at the end. A composer an Enter emptied counts as landed: the harness took
    the submit."""
    tries = int(os.environ.get("OFFICE_HERDR_ENTER_TRIES", "3"))
    delay = float(os.environ.get("OFFICE_HERDR_KEY_DELAY", "1"))
    wait = min(timeout, float(os.environ.get("OFFICE_HERDR_ENTER_WAIT", "5")))
    for n in range(tries):
        view = _pane_view(name)
        # An unreadable pane may still hold the prompt: press Enter, never
        # report it absent (that would type the prompt again).
        if view is not None and not _composer_holds(view, prompt):
            return "landed" if n else "absent"
        time.sleep(delay)
        _press_enter(name, pane, herdr)
        if _prompt_landed(name, wait, baseline_ctx=baseline_ctx, seen=seen) == "landed":
            return "landed"
    view = _pane_view(name)
    if view is None:
        return "unknown"
    return "held" if _composer_holds(view, prompt) else "landed"


def submit_prompt(name: str, text: str, *, pane: str | None = None) -> str:
    """Send `text` to a live herdr agent with `agent prompt` and confirm it was
    submitted, pressing Enter for it when it is left in the composer. Returns
    'landed', 'held' (still unsubmitted after the bounded Enters) or '' (no
    landed signal, nothing left in the composer). Never sends the text twice."""
    timeout = _land_timeout()
    baseline = _ctx_k(_pane_view(name))
    _herdr_quiet("agent", "prompt", name, text)
    if _prompt_landed(name, timeout, baseline_ctx=baseline) == "landed":
        return "landed"
    got = _submit_held(name, pane, text, timeout, baseline_ctx=baseline)
    return "" if got in ("absent", "unknown") else got


def _deliver_prompt(name: str, pane: str, pointer: str, *, answer_trust: bool = False, seen=None) -> bool:
    """Wait for the agent UI, send the one-line pointer and confirm it landed.
    A pointer left in the composer is submitted with Enter; a lost one is typed
    into the pane once and submitted (agy can drop a prompt sent right after
    `agent start` returns). Nothing is typed into a pane where herdr sees no
    agent: that text would go to the shell."""
    timeout = _land_timeout()
    herdr = _herdr_quiet
    ui = _await_agent_ui(name, pane, timeout, answer_trust=answer_trust, herdr=herdr)
    if ui == "trust":
        return False
    baseline = _ctx_k(_pane_view(name))
    herdr("agent", "prompt", name, pointer)
    got = _prompt_landed(name, timeout, baseline_ctx=baseline, seen=seen)
    if got == "landed":
        return True
    if got == "trust":
        # The dialog came up after the prompt was sent (a slow start): answer
        # it if Office may, then send the pointer again.
        if not answer_trust or not _trust_dialog(_pane_view(name)):
            return False
        herdr("pane", "send-keys", pane, "Enter")
        if _await_agent_ui(name, pane, timeout, answer_trust=False, herdr=herdr) != "ready":
            return False
        herdr("agent", "prompt", name, pointer)
        if _prompt_landed(name, timeout, baseline_ctx=baseline, seen=seen) == "landed":
            return True
    if not _agent_up(pane, name):
        return False
    held = _submit_held(name, pane, pointer, timeout, baseline_ctx=baseline, seen=seen, herdr=herdr)
    if held != "absent":
        return held == "landed"
    # Look again right before typing: a pointer held in any form, or a pane
    # that cannot be read, is never typed a second time.
    view = _pane_view(name)
    if view is None or _composer_holds(view, pointer):
        return False
    # The composer is empty: the pointer was lost, so type it once. A TUI can
    # take an Enter that follows typed text too closely as part of the paste:
    # pause, submit, and submit once more if it still has not landed.
    herdr("pane", "send-text", pane, pointer)
    delay = float(os.environ.get("OFFICE_HERDR_KEY_DELAY", "1"))
    for _ in range(2):
        time.sleep(delay)
        herdr("pane", "send-keys", pane, "Enter")
        if _prompt_landed(name, timeout / 2, baseline_ctx=baseline, seen=seen) == "landed":
            return True
    return False


def _watch_notice(dispatch_id: str, text: str) -> None:
    con = db.connect()
    try:
        d = state.get_dispatch(con, dispatch_id)
        run = state.get_run(con, d["run_id"])
    finally:
        con.close()
    _launch_notice(run, d, text)


def _launch_notice(run: dict, dispatch: dict, text: str) -> None:
    """Record a launch problem where `office status` shows it."""
    con = db.connect()
    try:
        with db.transaction(con):
            state.emit(con, run, "launch", f"{dispatch.get('task_id') or dispatch['id']}: {text}",
                       task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"])
    finally:
        con.close()


def _submitted(con, dispatch: dict) -> bool:
    """A submission that ends the session. One held at amendment_pending does
    not: the session still has to ack the amendment and resubmit (#200 B12)."""
    return bool(con.execute("SELECT 1 FROM revisions WHERE dispatch_id=? AND status<>'amendment_pending'",
                            (dispatch["id"],)).fetchone()
                or con.execute("SELECT 1 FROM plans WHERE run_id=? AND created_by=?",
                               (dispatch["run_id"], dispatch["id"])).fetchone())


def watch_herdr_agent(dispatch_id: str, spec: dict, *, poll: float | None = None, stable_samples: int = 3) -> tuple:
    """(exit_code, classification) for a pane-hosted agent. A submit or the
    output file ends the dispatch. The agent disappearing (process exit) ends
    it. For a reviewer, a single `done`/`idle` sample is never trusted: only
    `stable_samples` consecutive settled samples with unchanged pane content
    end it. A worker never ends on idle: an agent waiting on a background test
    run looks settled, and ending it started a second writer on the same
    worktree (#200 B10). An idle worker gets one notice instead."""
    poll = float(os.environ.get("OFFICE_HERDR_POLL", "5")) if poll is None else poll
    output = Path(spec["output"]) if spec.get("output") else None
    worker = spec.get("kind") == "worker"
    idle_notice = float(os.environ.get("OFFICE_WORKER_IDLE_NOTICE_MIN", "20")) * 60
    idle_since = None
    noticed = False
    history: list[dict] = []
    last_size = None
    unknown = 0
    unknown_limit = int(os.environ.get("OFFICE_HERDR_UNKNOWN_LIMIT", "60"))
    while True:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
            if d["status"] in ("exited", "failed", "cancelled"):
                # Already recorded (cancelled, or ended by another path): not ours to finish.
                return None, "cancelled" if d["status"] == "cancelled" else "duplicate_ignored"
            if _submitted(con, d):
                return 0, "success"
        finally:
            con.close()
        size = output.stat().st_size if output and output.is_file() else 0
        sample = _herdr_agent_sample(spec["herdr_agent"])
        if sample is not None:
            unknown = unknown + 1 if sample.get("busy") is None else 0
        # Blind only while this very sample is still unreadable.
        blind = sample is not None and sample.get("busy") is None and unknown >= unknown_limit
        if blind and unknown == unknown_limit:
            # Said once, before anything below can end the watch. A reviewer's
            # stable output now ends it; a worker, like a headless process,
            # ends on submit, exit, or revoke.
            _watch_notice(dispatch_id, f"herdr agent {spec['herdr_agent']} has been unreadable for "
                                       f"{unknown_limit} polls; check its pane, or office revoke")
        # The result file is the evidence (R13): only a pane still drawing a
        # turn (text busy) holds it back, not a `working` status alone.
        if size and size == last_size and (sample is None or sample.get("text_busy", sample.get("busy")) is False
                                           or blind):
            # Complete: written, no longer growing between two polls, and the
            # agent is not still mid-turn (it may rewrite the file). After the
            # pane has been unreadable past the limit, a file that stopped
            # growing is the only evidence left, so it bounds the wait.
            return 0, "success"
        last_size = size or None
        if sample is None:
            return None, "nonzero"
        if worker:
            if sample.get("busy") is False:
                idle_since = idle_since or time.time()
                if not noticed and time.time() - idle_since >= idle_notice:
                    noticed = True
                    _watch_notice(dispatch_id, f"herdr agent {spec['herdr_agent']} has been idle for "
                                               f"{int(idle_notice // 60)} min without submitting; check its pane")
            else:
                idle_since, noticed = None, False
            time.sleep(poll)
            continue
        history.append(sample)
        window = history[-stable_samples:]
        if (len(window) == stable_samples and all(w["status"] in ("done", "idle") for w in window)
                and all(w.get("busy") is False for w in window)
                and len({w["content_hash"] for w in window}) == 1):
            # A reviewer's result is only its reply file (R13). A missing file is
            # left missing: run_reviewer re-prompts the same session to write it
            # (R11); pane or transcript text is never taken as the review.
            return 0, "success"
        time.sleep(poll)


def _session_cwd(spec: dict, harness: str | None, cwd: Path) -> Path:
    """The directory the harness records as its session cwd. A codex reviewer
    runs in its dispatch dir, its only writable root, not in the checkout."""
    if harness == "codex" and spec.get("kind") in read_scope.READER_KINDS and spec.get("output"):
        return Path(spec["output"]).parent
    return cwd


def transcript_reply(d: dict, spec: dict) -> str | None:
    """The pane-hosted agent's final reply from its harness session log, found
    by the brief path its prompt named and the cwd it ran in."""
    from office import transcripts
    marker = spec.get("prompt_file")
    if not marker or not d:
        return None
    try:
        return transcripts.final_reply(d.get("harness"), marker=marker, cwd=spec.get("cwd"),
                                       since=d.get("launched_at") or d.get("started_at"))
    except Exception:  # a fallback reader must never end the watch abnormally
        return None


def _herdr_json(args: list[str]) -> dict:
    try:
        proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=30)
        return json.loads(proc.stdout or "{}").get("result") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _herdr_pane(run: dict, cwd: Path, label: str | None = None, dispatch_id: str | None = None) -> str | None:
    """A visible Herdr pane for a dispatch, split beside the caller's own pane.

    The first dispatch splits the orchestrator's pane (`HERDR_PANE_ID`, or
    `OFFICE_HERDR_ANCHOR` when set) to the right, so the agent appears in the
    tab the user is watching. Later dispatches reuse a pane whose dispatch has
    ended, else stack down in that column. The caller's pane is only ever split,
    never run in or closed. Without a caller pane, the run gets its own tab.

    Choosing the pane and recording it as reserved for `dispatch_id` is one
    step under the run's pane lock, so two launches at once (parallel dispatch,
    back-to-back reruns) never take the same pane or lose each other's record."""
    anchor = os.environ.get("OFFICE_HERDR_ANCHOR") or os.environ.get("HERDR_PANE_ID")
    with _pane_lock(run) as tab_file:
        layout = _read_layout(tab_file)
        if layout and layout.get("mode") != "split" or (layout is None and not anchor):
            pane = _herdr_own_tab_pane(run, cwd, tab_file, layout, dispatch_id)
        else:
            pane = _herdr_split_pane(run, cwd, tab_file, layout, anchor, dispatch_id)
    if pane and label:
        _herdr_rename(pane, label)
    return pane


@contextlib.contextmanager
def _pane_lock(run: dict):
    """Exclusive, cross-process lock on the run's pane layout (herdr-tab.json);
    yields that file's path. A flock dies with its process."""
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    tab_file.parent.mkdir(parents=True, exist_ok=True)
    with tab_file.with_name("herdr-tab.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield tab_file
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_layout(tab_file: Path) -> dict | None:
    try:
        layout = json.loads(tab_file.read_text()) if tab_file.is_file() else None
    except (OSError, ValueError):
        return None
    return layout if isinstance(layout, dict) else None


def _release_panes(run: dict, dispatch_id: str) -> None:
    """Drop every pane reservation this dispatch holds (its launch fell back, so the pane is free)."""
    with _pane_lock(run) as tab_file:
        layout = _read_layout(tab_file)
        reserved = (layout or {}).get("reserved") or {}
        held = [p for p, holder in reserved.items() if holder == dispatch_id]
        if held:
            for pane in held:
                del reserved[pane]
            atomic_write_json(tab_file, layout)


def _reserved_by(run: dict, pane: str) -> str | None:
    """The dispatch this run's layout reserved `pane` for, if any."""
    layout = _read_layout(paths.run_dir(run["id"]) / "herdr-tab.json") if run.get("id") else None
    return ((layout or {}).get("reserved") or {}).get(pane)


def _reserved_busy(layout: dict, dispatch_id: str | None = None) -> set:
    """Reserved panes whose dispatch is still launching or running: a launch reserves its
    pane well before the dispatch row records it. `dispatch_id`'s own reservation is not busy to it."""
    reserved = {p: d for p, d in (layout.get("reserved") or {}).items() if d != dispatch_id}
    if not reserved:
        return set()
    con = db.connect()
    try:
        busy = set()
        for pane, did in reserved.items():
            row = con.execute("SELECT status FROM dispatches WHERE id=?", (did,)).fetchone()
            if row is not None and row["status"] in ("launching", "running"):
                busy.add(pane)
        return busy
    finally:
        con.close()


def _herdr_rename(pane: str, label: str) -> None:
    """A label is cosmetic: a rename that fails or times out never fails a launch."""
    try:
        subprocess.run(["herdr", "pane", "rename", pane, label], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def pane_label(run: dict, dispatch: dict, kind: str = "") -> str:
    """What a human sees on a pane tab: `<task> <role> [PR#n] <short id>`, e.g.
    `T3 executor PR#261 D4f2`. Integration and plan dispatches carry no task, so
    they lead with `integration` or `plan` (plus the integration PR when known)."""
    role = dispatch.get("role") or kind
    short = str(dispatch["id"])[:5]
    pr = None
    scope = dispatch.get("task_id")
    try:
        con = db.connect()
        try:
            if scope:
                task = state.get_task(con, run["id"], scope)
                pr = ((task or {}).get("pr") or {}).get("number")
            else:
                gate = con.execute("SELECT subject FROM gates WHERE id=?", (dispatch.get("gate_id"),)).fetchone() \
                    if dispatch.get("gate_id") else None
                scope = "integration" if gate and gate["subject"] == "integration" else "plan"
                if scope == "integration":
                    pr = (((state.get_run(con, run["id"]) or {}).get("landing") or {}).get("integration") or {}).get("pr")
                    pr = pr.get("number") if isinstance(pr, dict) else pr
        finally:
            con.close()
    except Exception:  # a label is cosmetic; fall back to the fields known without the DB
        pr = None
    return " ".join(p for p in (scope, role, f"PR#{pr}" if pr else "", short) if p)


def relabel_task_panes(run: dict, task_id: str) -> None:
    """Best-effort: rename the task's live herdr panes once its PR number is known."""
    try:
        con = db.connect()
        try:
            rows = con.execute("SELECT * FROM dispatches WHERE run_id=? AND task_id=? AND launcher='herdr' "
                               "AND pane_id IS NOT NULL AND status IN ('launching','running')",
                               (run["id"], task_id)).fetchall()
            labels = [(r["pane_id"], pane_label(run, dict(r), r["kind"] or "")) for r in rows]
        finally:
            con.close()
        for pane, label in labels:
            _herdr_rename(pane, label)
    except Exception:
        pass


def _busy_panes(run: dict) -> set:
    con = db.connect()
    try:
        return {r["pane_id"] for r in con.execute("SELECT pane_id FROM dispatches WHERE run_id=? AND launcher='herdr' "
                                                  "AND status IN ('launching','running')", (run["id"],)).fetchall()}
    finally:
        con.close()


def _pane_is_shell(pane: str) -> bool:
    """A pane Office may start an agent in: live, with no agent in it. An ended
    dispatch's pane can still hold its finished session, which herdr rejects
    (`agent_pane_busy`), so the dispatch row alone does not make it reusable."""
    info = _herdr_json(["pane", "get", pane]).get("pane") or {}
    return bool(info) and not info.get("agent")


def _herdr_split_pane(run: dict, cwd: Path, tab_file: Path, layout: dict | None, anchor: str | None,
                      dispatch_id: str | None = None) -> str | None:
    """Caller holds the pane lock."""
    layout = layout or {"mode": "split", "anchor": anchor, "panes": []}
    anchor = layout.get("anchor") or anchor
    live = [p for p in layout["panes"] if _herdr_json(["pane", "get", p])]  # the user may close panes
    busy = _busy_panes(run) | _reserved_busy(layout, dispatch_id)
    for pane in live:
        if pane not in busy and _pane_is_shell(pane):
            subprocess.run(["herdr", "pane", "run", pane, f"cd {shlex.quote(str(cwd))}"], capture_output=True, timeout=30)
            layout["panes"] = live
            _reserve(layout, pane, dispatch_id)
            atomic_write_json(tab_file, layout)
            return pane
    # Split the caller's pane vertically (side by side); stack further agents in that column.
    target, direction = (live[-1], "down") if live else (anchor, "right")
    res = _herdr_json(["pane", "split", "--pane", target, "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    info = res.get("pane") or {}
    pane = info.get("pane_id")
    if not pane:
        return None
    layout["panes"] = live + [pane]
    layout.setdefault("tab_id", info.get("tab_id"))
    _reserve(layout, pane, dispatch_id)
    atomic_write_json(tab_file, layout)
    return pane


def _reserve(layout: dict, pane: str, dispatch_id: str | None) -> None:
    """Reserve `pane` for `dispatch_id`, which holds one pane at a time: an earlier launch attempt's
    pane (a retried job, a busy-pane retry) is given up."""
    if dispatch_id:
        reserved = layout.setdefault("reserved", {})
        for held in [p for p, holder in reserved.items() if holder == dispatch_id]:
            del reserved[held]
        reserved[pane] = dispatch_id
    else:
        layout.get("reserved", {}).pop(pane, None)  # a caller naming no dispatch takes the pane over


def _herdr_own_tab_pane(run: dict, cwd: Path, tab_file: Path, tab: dict | None, dispatch_id: str | None = None) -> str | None:
    """No caller pane to split (or a run begun before split mode): a tab owned by the run.
    Caller holds the pane lock."""
    if tab and not _herdr_json(["tab", "get", tab["tab_id"]]):
        tab = None  # the user closed it
    if tab is None:
        workspace = (os.environ.get("HERDR_WORKSPACE_ID") or os.environ.get("HERDR_PANE_ID") or "").split(":")[0]
        args = ["tab", "create", "--label", f"office-{run['id'][:8]}", "--cwd", str(cwd), "--no-focus"]
        if workspace:
            args[2:2] = ["--workspace", workspace]
        res = _herdr_json(args)
        root = (res.get("root_pane") or {}).get("pane_id")
        if not root:
            return None
        tab = {"mode": "tab", "tab_id": (res.get("tab") or {}).get("tab_id"), "panes": [root]}
        _reserve(tab, root, dispatch_id)
        atomic_write_json(tab_file, tab)
        return root
    busy = _busy_panes(run) | _reserved_busy(tab, dispatch_id)
    for pane in tab["panes"]:
        if pane not in busy and _pane_is_shell(pane):
            subprocess.run(["herdr", "pane", "run", pane, f"cd {shlex.quote(str(cwd))}"], capture_output=True, timeout=30)
            _reserve(tab, pane, dispatch_id)
            atomic_write_json(tab_file, tab)
            return pane
    direction = "right" if len(tab["panes"]) % 2 else "down"
    res = _herdr_json(["pane", "split", "--pane", tab["panes"][-1], "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    pane = (res.get("pane") or {}).get("pane_id")
    if pane:
        tab["panes"].append(pane)
        _reserve(tab, pane, dispatch_id)
        atomic_write_json(tab_file, tab)
    return pane


def close_herdr_tab(run: dict) -> None:
    """Close what the run created: its panes in the caller's tab, or its own tab."""
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    if not shutil.which("herdr"):
        return
    # Each dispatch pane is snapshotted before it closes.
    con = db.connect()
    try:
        owned = [r["id"] for r in con.execute("SELECT id FROM dispatches WHERE run_id=? AND launcher='herdr' "
                                              "AND pane_id IS NOT NULL AND pane_closed_at IS NULL", (run["id"],))]
    finally:
        con.close()
    for did in owned:
        try:
            reclaim_pane(run, did, explicit=True)
        except Exception:
            pass
    if tab_file.is_file():
        try:
            tab = json.loads(tab_file.read_text())
            if tab.get("mode") == "split":
                for pane in tab.get("panes") or []:
                    if _pane_exists(pane):
                        subprocess.run(["herdr", "pane", "close", pane], capture_output=True, timeout=30)
            else:
                subprocess.run(["herdr", "tab", "close", tab["tab_id"]], capture_output=True, timeout=30)
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            pass


def _orchestrator_pane(run: dict) -> str | None:
    """The pane of the orchestrator that owns this dispatch: its HERDR_PANE_ID,
    else the anchor the run's split layout recorded (a relaunch from a detached
    job has no pane of its own)."""
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        return pane
    layout = _read_layout(paths.run_dir(run["id"]) / "herdr-tab.json") or {}
    return layout.get("anchor") or os.environ.get("OFFICE_HERDR_ANCHOR") or None


def _started_session(stdout: str | None) -> str | None:
    """The agent session id `herdr agent start` reports, when it reports one."""
    try:
        res = json.loads(stdout or "{}").get("result") or {}
    except ValueError:
        return None
    agent = res.get("agent") or res
    return agent.get("session_id") or agent.get("agent_session_id") if isinstance(agent, dict) else None


def _pane_ledger(run: dict, dispatch: dict, pane: str, *, agent: str | None = None, kind: str | None = None,
                 worktree: Path | str | None = None, session_id: str | None = None) -> None:
    """One row per spawned pane, in the herdr-ledger schema, so `herdr-ledger
    sweep` (which only touches rows whose orchestrator_pane_id is the caller's
    pane) can close it once the agent has finished."""
    ledger = paths.run_dir(run["id"]) / "panes.jsonl"
    now = now_iso()
    row = {"pane_id": pane, "agent": agent, "kind": kind, "session_id": session_id,
           "worktree": str(worktree) if worktree else None, "spawned_at": now, "recorded_at": now,
           "orchestrator_pane_id": _orchestrator_pane(run),
           "orchestrator_session_id": os.environ.get("HERDR_SESSION_ID") or None,
           "run_id": run["id"], "dispatch_id": dispatch["id"], "role": dispatch.get("role"),
           "status": "working", "suggestion": None, "note": None, "closed": False, "updated_at": now}
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _capture_session(name: str) -> str | None:
    """The harness session id herdr detected for the agent (`agent_session.value`),
    polled briefly: detection lags `agent start` by a moment."""
    deadline = time.time() + float(os.environ.get("OFFICE_HERDR_SESSION_WAIT", "2"))
    while True:
        agent = _herdr_json(["agent", "get", name]).get("agent") or {}
        value = (agent.get("agent_session") or {}).get("value")
        if value:
            return str(value)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _resume_with_reply_access(dispatch: dict, kind: str, resume: dict, cwd: Path, include_dirs: list[Path] | None,
                              output: Path) -> dict:
    """A resumed reviewer still writes its reply file: rebuild the resume argv
    with the directory that file lives in, which the caller's form lacked."""
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or "")
    form = adapters.resume_argv(adapter, kind, session_id=resume["session_id"], model=dispatch.get("model") or "",
                                effort=dispatch.get("effort") or "none", cwd=cwd, include_dirs=include_dirs,
                                output=output) if adapter and resume.get("session_id") else None
    return {**resume, "argv": form[0], "herdr_kind": form[1]} if form else resume


def _assigned_session(d: dict) -> str | None:
    """The id to pass a harness that takes one at launch. A resumed dispatch
    continues its parent's session and passes none."""
    return None if d.get("resumed_from") else d.get("session_id")


def _assign_session(dispatch: dict) -> None:
    """Where the harness accepts an assigned session id (claude --session-id),
    pick it and record it before the agent starts, whatever the role. A
    dispatch relaunched after a failed start keeps the id it already has."""
    if dispatch.get("session_id") or not adapters.assigns_session(adapters.load_all().get(dispatch.get("adapter_id") or "")):
        return
    session = str(uuid.uuid4())
    _set_dispatch(dispatch["id"], session_id=session)
    dispatch["session_id"] = session


def record_session(con, run: dict, dispatch_id: str, session: str, *, harness: str | None = None, source: str) -> str:
    """Record the harness session id seen for a dispatch: "set" when it was
    empty, "same" when it already matches, "mismatch" when a different id is
    recorded (kept, and noted once as an event), "ignored" for another run's
    dispatch, another harness's id, or a malformed one."""
    d = state.get_dispatch(con, dispatch_id)
    if (not d or d["run_id"] != run["id"] or (harness and d.get("harness") != harness)
            or not _SESSION_ID.fullmatch(session)):
        return "ignored"
    if d.get("session_id") == session:
        return "same"
    with db.transaction(con):
        recorded = state.get_dispatch(con, dispatch_id).get("session_id")  # re-read under the write lock
        if not recorded:
            con.execute("UPDATE dispatches SET session_id=? WHERE id=?", (session, dispatch_id))
            return "set"
        if recorded == session:
            return "same"
        seen = {loads(r[0], {}).get("seen") for r in con.execute(
            "SELECT payload_json FROM events WHERE dispatch_id=? AND kind='session.mismatch'", (dispatch_id,))}
        if session not in seen:
            state.emit(con, run, "session.mismatch", f"{d.get('task_id') or dispatch_id}: {source} saw session {session} but "
                       f"{recorded} is recorded for {dispatch_id}; the recorded id is kept", audience="runtime",
                       task_id=d.get("task_id"), dispatch_id=dispatch_id,
                       payload={"recorded": recorded, "seen": session, "source": source})
        return "mismatch"


class _SessionSniffer:
    """Reads a headless agent's stream for the session id its harness prints in
    the header block before the prompt (adapter `session.output_pattern`), and
    records it. Only a line inside that block counts: the block is the text
    between two rule lines, the first within a few lines of the start. A stream
    with no such block, a prompt echoed after it, or an agent reply that happens
    to contain the pattern is never taken for an id. Best effort: a recording
    failure is noted in the log and retried, and never disturbs the run."""

    HEADER_BYTES = 16384
    BANNER_LINES = 3  # lines allowed before the opening rule
    ATTEMPTS = 2
    _ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def __init__(self, run: dict, dispatch: dict, adapter: dict | None, log_path=None):
        self.run, self.dispatch, self.log_path = run, dispatch, log_path
        self.pattern = adapters.session_output_pattern(adapter)
        self.buffer, self.seen, self.rules, self.lead = b"", 0, 0, 0
        if not self.pattern and adapters.session_spec(adapter).get("output_pattern"):
            _note(log_path, "office: the adapter's session.output_pattern is unusable; the session id is not captured")

    def _stop(self, why: str | None = None) -> None:
        """Stop reading. `why` is logged when no id was recorded, so a harness
        that stops printing one is visible instead of silently unresumable."""
        self.pattern, self.buffer = None, b""
        if why:
            _note(self.log_path, f"office: no session id captured: {why}")

    def feed(self, chunk: bytes) -> None:
        if not self.pattern:
            return
        self.seen += len(chunk)
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for raw in lines:
            line = self._ANSI.sub("", raw.decode("utf-8", "replace")).strip()
            if re.fullmatch(r"-{8,}", line):
                self.rules += 1
                if self.rules >= 2:  # the header block is closed: the prompt follows
                    return self._stop("the header block closed without a session id line")
            elif self.rules == 0:
                self.lead += 1 if line else 0
                if self.lead > self.BANNER_LINES:  # no header block opens this stream
                    return self._stop("no header block opened the stream")
            else:
                found = self.pattern.match(line)
                if found:
                    self._record(found.group(1))
                    return self._stop()
        if self.seen > self.HEADER_BYTES or len(self.buffer) > self.HEADER_BYTES:
            self._stop("the header block did not finish within the first bytes")

    def _record(self, session: str) -> None:
        """Record the id, retrying a failed write once; a final failure is noted in the log."""
        for attempt in range(self.ATTEMPTS):
            try:
                return _record_session(self.run, self.dispatch, session, source="output")
            except Exception as exc:
                if attempt + 1 == self.ATTEMPTS:
                    _note(self.log_path, f"office: could not record session id {session}: {exc!r}")


def _record_session(run: dict, dispatch: dict, session: str, *, source: str) -> None:
    con = db.connect()
    try:
        record_session(con, run, dispatch["id"], session, harness=dispatch.get("harness"), source=source)
    finally:
        con.close()


def _set_dispatch(dispatch_id: str, **cols) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            sets = ", ".join(f"{k}=?" for k in cols)
            con.execute(f"UPDATE dispatches SET {sets} WHERE id=?", (*cols.values(), dispatch_id))
    finally:
        con.close()


def _herdr_fresh_pane(run: dict, cwd: Path, busy_pane: str, dispatch_id: str | None = None) -> str | None:
    """A new pane split from one herdr refused as busy, recorded in the run's layout
    and reserved for the dispatch in place of the busy one (`_reserve` gives that up)."""
    with _pane_lock(run) as tab_file:
        res = _herdr_json(["pane", "split", "--pane", busy_pane, "--direction", "down", "--cwd", str(cwd), "--no-focus"])
        pane = (res.get("pane") or {}).get("pane_id")
        if not pane:
            return None
        layout = _read_layout(tab_file)
        if layout is not None:
            layout.setdefault("panes", []).append(pane)
            _reserve(layout, pane, dispatch_id)
            atomic_write_json(tab_file, layout)
    return pane


def _pane_exists(pane: str) -> bool:
    """False only when herdr says the pane is gone; an unreachable herdr counts
    as present, so nothing is recorded closed that may still be open."""
    try:
        proc = subprocess.run(["herdr", "pane", "get", pane], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return "pane_not_found" not in (proc.stdout or "") + (proc.stderr or "")


def _pane_snapshot(name: str, pane: str) -> str:
    """The pane's final text, trying the unwrapped scrollback first. Argument
    lists only: a command held in a string never reaches herdr intact."""
    for args in (["agent", "read", name, "--source", "recent-unwrapped", "--lines", "5000"],
                 ["pane", "read", pane, "--source", "recent-unwrapped", "--lines", "5000"],
                 ["pane", "read", pane, "--source", "recent"]):
        try:
            proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout
    return ""


def _ledger_event(run: dict, d: dict, **fields) -> None:
    ledger = paths.run_dir(run["id"]) / "panes.jsonl"
    row = {"pane_id": d.get("pane_id"), "dispatch_id": d["id"], "run_id": run["id"], "role": d.get("role"),
           "recorded_at": now_iso(), **fields}
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def reclaim_pane(run: dict, dispatch_id: str, *, explicit: bool = False) -> str:
    """Snapshot, then close, the herdr pane Office opened for this dispatch.

    Returns "closed", "kept", or "skipped". Order: pane-final.txt, the ledger
    row (`result: accepted` carrying the snapshot path; the Stop hook closes
    only rows whose snapshot exists), `herdr pane close`, a check that the pane
    is gone, then `pane_closed_at` and a `closed_at` row. Only this dispatch's
    own pane is ever touched. On the automatic path a failed snapshot or
    `keep_pane` (OFFICE_KEEP_PANES=1 at launch) keeps the pane with a `kept`
    row; `explicit` (dismiss, close, revoke) closes regardless."""
    con = db.connect()
    try:
        d = state.get_dispatch(con, dispatch_id)
    finally:
        con.close()
    if not d or d.get("launcher") != "herdr" or not d.get("pane_id") or not shutil.which("herdr"):
        return "skipped"
    if d.get("pane_closed_at"):
        return "closed"
    pane = d["pane_id"]
    if not _pane_exists(pane):
        _set_dispatch(dispatch_id, pane_closed_at=now_iso())
        _ledger_event(run, d, closed_at=now_iso(), note="pane already gone")
        return "closed"
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch_id
    ddir.mkdir(parents=True, exist_ok=True)
    snap = ddir / "pane-final.txt"
    text = _pane_snapshot(herdr_agent_name(dispatch_id), pane)
    if text:
        snap.write_text(text, encoding="utf-8")
    if d.get("keep_pane") and not explicit:
        _ledger_event(run, d, kept=True, reason="OFFICE_KEEP_PANES", snapshot=str(snap) if text else None)
        return "kept"
    if not text and not explicit:
        _ledger_event(run, d, kept=True, reason="snapshot failed")
        _launch_notice(run, d, f"pane {pane} kept open: its final text could not be saved; "
                               f"close it with office dismiss {d.get('task_id') or dispatch_id}")
        return "kept"
    _ledger_event(run, d, result="accepted" if not explicit else "dismissed", snapshot=str(snap) if text else None)
    grace = float(os.environ.get("OFFICE_RECLAIM_GRACE", "3"))
    if grace and not explicit:
        time.sleep(grace)  # let the agent's own `office submit` finish printing
    try:
        subprocess.run(["herdr", "pane", "close", pane], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    if _pane_exists(pane):
        _ledger_event(run, d, kept=True, reason="herdr pane close did not close it")
        _launch_notice(run, d, f"pane {pane} did not close; close it by hand: herdr pane close {pane}")
        return "kept"
    _set_dispatch(dispatch_id, pane_closed_at=now_iso())
    _ledger_event(run, d, closed_at=now_iso())
    return "closed"


def _record_launch(run: dict, dispatch_id: str, *, launcher: str, pid: int | None = None, pane_id: str | None = None) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET launcher=?, pid=COALESCE(?, pid), pane_id=?, launched_at=?, "
                        "status=CASE WHEN status='launching' THEN 'running' ELSE status END WHERE id=?",
                        (launcher, pid, pane_id, now_iso(), dispatch_id))
            if pid:
                _record_identity(run["id"], dispatch_id, "supervisor", pid)
            if os.environ.get("OFFICE_KEEP_PANES") == "1":
                # R10: this dispatch's pane stays open after it ends (snapshot still written).
                con.execute("UPDATE dispatches SET keep_pane=1 WHERE id=?", (dispatch_id,))
            d = state.get_dispatch(con, dispatch_id)
            if d.get("task_id"):
                task = state.get_task(con, run["id"], d["task_id"])
                if task and task["current_dispatch_id"] == dispatch_id and task["status"] == "launching":
                    state.update_task(con, run["id"], d["task_id"], status="running")
    finally:
        con.close()


def _started(dispatch_id: str, timeout: float) -> bool:
    """True once the supervisor has marked the dispatch (it sets last_seen_at)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
        finally:
            con.close()
        if d.get("last_seen_at") or d["status"] in ("exited", "failed", "cancelled"):
            return True
        time.sleep(1)
    return False


def _wait_terminal(dispatch_id: str, timeout: float | None = None) -> dict:
    deadline = time.time() + (timeout or 4 * 3600)
    while time.time() < deadline:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
        finally:
            con.close()
        if d["status"] in ("exited", "failed", "cancelled"):
            return {"exit_code": d["exit_code"], "terminal": d["terminal_classification"], "signal": d["signal"]}
        if d.get("pid") and d.get("launcher") in ("process", "process-fallback") and not pid_alive(d["pid"]) and d["status"] == "running":
            time.sleep(1)
            continue
        time.sleep(2)
    return {"exit_code": None, "terminal": "timeout"}


# ------------------------------------------------------------------ supervisor

def supervise(dispatch_id: str) -> int:
    """Run one agent process and always record how it ended, including when
    the supervisor itself cannot start the agent."""
    started = time.time()
    code, sig, classification = None, None, None
    child = None
    state_box = {"signal": None, "watching": False}

    def forward(signum, _frame):
        state_box["signal"] = signum
        if state_box["watching"]:
            raise _Stopped(signum)
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except OSError:
                pass

    # An in-process supervisor hands its caller's handlers back when the dispatch ends.
    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    timer = None
    log_path = None
    try:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
            run = state.get_run(con, d["run_id"])
        finally:
            con.close()
        if os.environ.get("OFFICE_SUPERVISOR_VIA") == "herdr" and d.get("launcher") != "herdr":
            # This pane command arrived after the launch fell back to a plain
            # process; running it too would mean two writers for one dispatch.
            classification = "duplicate_ignored"
            return 0
        spec = json.loads((paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "launch.json").read_text())
        if spec.get("external") and spec.get("output"):
            _mark(dispatch_id, pid_child=os.getpid())
            state_box["watching"] = True
            code, classification = watch_external_output(dispatch_id, spec)
            state_box["watching"] = False
            return 0 if classification == "success" else 1
        if spec.get("herdr_agent"):
            # The agent runs in its herdr pane; this process only watches for the end.
            _mark(dispatch_id, pid_child=os.getpid())
            state_box["watching"] = True
            code, classification = watch_herdr_agent(dispatch_id, spec)
            state_box["watching"] = False
            return 0 if classification == "success" else 1
        log_path = Path(d.get("log_path") or spec.get("log_path"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        adapter = adapters.load_all()[d["adapter_id"]]
        output = Path(spec["output"]) if spec.get("output") else None
        argv, prof = adapters.build_argv(adapter, spec["kind"], model=d["model"], effort=d["effort"] or "none",
                                         cwd=Path(spec["cwd"]), output=output,
                                         images=[Path(i) for i in spec.get("images") or []],
                                         include_dirs=[Path(i) for i in spec.get("include_dirs") or []],
                                         session_id=_assigned_session(d))
        prompt = Path(spec["prompt_file"]).read_text(encoding="utf-8")
        if prof.get("image_transport") in ("prompt-at", "prompt-path") and spec.get("images"):
            prefix = "@" if prof["image_transport"] == "prompt-at" else ""
            prompt += "\n\nEvidence images (inspect each one):\n" + "\n".join(f"{prefix}{i}" for i in spec["images"])
        env = worker_env(run, d, d["role"] if spec["kind"] == "worker" else "reviewer")
        if spec["kind"] != "worker":
            # Reviewers get no Office authority: they cannot submit or ack.
            for k in ("OFFICE_TASK_ID", "OFFICE_DISPATCH_ID"):
                env.pop(k, None)
            env["OFFICE_ROLE"] = "reviewer"
        with log_path.open("ab") as log:
            os.chmod(log_path, 0o600)
            stdin = subprocess.PIPE if prof.get("prompt") == "stdin" else subprocess.DEVNULL
            if prof.get("prompt") == "argv":
                argv = argv + [prompt]
            elif prof.get("prompt") == "argv-bound":
                argv = argv + [prof.get("prompt_flag", "--prompt=") + prompt]
            child = _spawn_agent(argv, spec["cwd"], env, stdin)
            _mark(dispatch_id, pid_child=child.pid)
            _agent_pgid_file(run, dispatch_id).write_text(str(child.pid))
            _record_identity(run["id"], dispatch_id, "agent", child.pid)
            cap = _wall_cap_seconds(prof)
            if cap:
                # A harness whose own timeout can fail to fire (agy stalled 3.5h
                # past --print-timeout 45m) is stopped here instead of hanging the task.
                def expire(pid=child.pid):
                    state_box["timed_out"] = True
                    _note(log_path, f"office: wall-clock cap of {cap / 60:g} min reached; stopping the agent")
                    _killpg(pid, signal.SIGTERM)
                    time.sleep(10)
                    _killpg(pid, signal.SIGKILL)
                timer = threading.Timer(cap, expire)
                timer.daemon = True
                timer.start()
            if stdin == subprocess.PIPE:
                try:
                    child.stdin.write(prompt.encode())
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            sniffer = None if d.get("resumed_from") else _SessionSniffer(run, d, adapter, log_path)
            for chunk in iter(lambda: child.stdout.read1(65536), b""):
                log.write(chunk)
                log.flush()
                if sniffer:
                    sniffer.feed(chunk)
                if sys.stdout.isatty():
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
            child.wait()
        code = child.returncode
        if state_box.get("timed_out"):
            classification = "timeout"
        elif code is not None and code < 0:
            sig, classification = -code, "signal"
        elif state_box["signal"]:
            sig, classification = state_box["signal"], "signal"
        else:
            classification = "success" if code == 0 else "nonzero"
    except _Stopped as stop:
        sig, classification = stop.signum, "signal"
    except FileNotFoundError as exc:
        code, classification = 127, "launch_failed"
        _note(log_path, f"launch failed: {exc}")
    except BaseException as exc:  # the classification must still be recorded
        classification = "supervisor_error"
        _note(log_path, f"supervisor error: {exc!r}")
    finally:
        try:
            if classification != "duplicate_ignored":
                _finish(dispatch_id, code, sig, classification or "unknown", time.time() - started)
        finally:
            if timer:
                timer.cancel()
            for s, handler in previous.items():
                signal.signal(s, handler if handler is not None else signal.SIG_DFL)
    return 0 if classification == "success" else 1


class _Stopped(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = int(signum)


def _note(log_path, text: str) -> None:
    if log_path is None:
        return
    try:
        with Path(log_path).open("ab") as log:
            log.write(f"\n[office] {text}\n".encode())
    except OSError:
        pass


def _mark(dispatch_id: str, pid_child: int) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET pid=?, status=CASE WHEN status='launching' THEN 'running' ELSE status END, "
                        "last_seen_at=? WHERE id=?", (os.getpid(), now_iso(), dispatch_id))
            run_id = state.get_dispatch(con, dispatch_id)["run_id"]
    finally:
        con.close()
    _record_identity(run_id, dispatch_id, "supervisor", os.getpid())


def _finish(dispatch_id: str, code, sig, classification: str, wall: float) -> None:
    accepted = False
    con = db.connect()
    try:
        with db.transaction(con):
            d = state.get_dispatch(con, dispatch_id)
            status = "cancelled" if d["status"] == "cancelled" else ("exited" if classification in ("success", "nonzero", "signal") else "failed")
            con.execute("UPDATE dispatches SET status=?, exit_code=?, signal=?, terminal_classification=?, ended_at=?, "
                        "wall_clock_seconds=? WHERE id=?", (status, code, sig, classification, now_iso(), wall, dispatch_id))
            run = state.get_run(con, d["run_id"])
            state.emit(con, run, "dispatch.ended", f"{d['task_id'] or d['role']} {d['role']} ended: {classification}"
                       + (f" (exit {code})" if code not in (None, 0) else ""), audience="runtime",
                       task_id=d["task_id"], dispatch_id=dispatch_id,
                       payload={"exit_code": code, "signal": sig, "classification": classification})
            if d["kind"] in ("planner", "executor"):
                after_worker_exit(con, run, dispatch_id)
            accepted = (d["kind"] in ("planner", "executor") and classification == "success"
                        and _submitted(con, d))
    finally:
        con.close()
    if accepted:
        # R1: a worker that ended with an accepted submission leaves no pane
        # behind. Every other end keeps its pane for diagnosis (R4).
        try:
            reclaim_pane(run, dispatch_id)
        except Exception as exc:  # reclaiming must never lose the recorded end
            _launch_notice(run, d, f"pane reclaim failed: {exc}")
    con = db.connect()
    try:
        jobs.kick(con, d["run_id"])
    finally:
        con.close()


def after_worker_exit(con, run: dict, dispatch_id: str) -> None:
    """A worker that exits without submitting leaves a blocker, not a pass."""
    d = state.get_dispatch(con, dispatch_id)
    task = state.get_task(con, run["id"], d["task_id"]) if d.get("task_id") else None
    if task is None or task["current_dispatch_id"] != dispatch_id:
        return
    if task["status"] == "changes_required":
        # Findings arrived while this session was alive and it ended without
        # acting on them: they wait for the orchestrator (office rerun), R8.
        state.emit(con, run, "task.findings_queued", f"{task['id']} worker ended with findings open: "
                   f"office rerun {task['id']} --resume | --fresh", task_id=task["id"])
        return
    if task["status"] in ("running", "launching"):
        submitted = con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        plan_sub = con.execute("SELECT 1 FROM plans WHERE run_id=? AND created_by=?", (run["id"], dispatch_id)).fetchone()
        if submitted or plan_sub:
            return
        refused = con.execute("SELECT summary FROM events WHERE run_id=? AND dispatch_id=? AND kind='submit.refused' "
                              "ORDER BY seq DESC LIMIT 1", (run["id"], dispatch_id)).fetchone()
        if refused:
            # The worker's submit was refused for a reason a fresh session would
            # hit again; the orchestrator must amend the scope or the brief.
            state.update_task(con, run["id"], task["id"], status="blocked", pause_reason=f"submit refused: {refused[0]}")
            state.emit(con, run, "task.blocked", f"{task['id']} submit refused ({refused[0]}); amend the scope "
                       f"(office amend {task['id']} -- ...) or the brief; work is preserved in its worktree",
                       task_id=task["id"])
            return
        wall = _quota_wall(run, d)
        if wall:
            # Relaunching on the same route lands in the same exhausted quota
            # (issue #267 A3): block at once and say so, never retry.
            state.update_task(con, run["id"], task["id"], status="blocked",
                              pause_reason=f"harness quota exhausted on {d['triple']}: {wall}")
            state.emit(con, run, "task.blocked", f"{task['id']} worker hit a harness quota wall on {d['triple']} "
                       f"({wall}); not relaunching into the same quota. After it resets: office rerun {task['id']} "
                       f"--fresh; work is preserved in its worktree", task_id=task["id"])
            return
        retries = con.execute("SELECT COUNT(*) FROM dispatches d WHERE d.run_id=? AND d.task_id=? AND d.terminal_classification "
                              "IS NOT NULL AND NOT EXISTS (SELECT 1 FROM revisions r WHERE r.dispatch_id=d.id)",
                              (run["id"], task["id"])).fetchone()[0]
        limit = (state.pinned_config(run).get("verification") or {}).get("environment_retry_max", 2)
        if retries <= limit:
            state.emit(con, run, "task.relaunch", f"{task['id']} worker ended ({d['terminal_classification']}) "
                       f"without submitting; relaunching {retries}/{limit}", audience="runtime", task_id=task["id"])
            request_launch(con, run, task["id"], role=d["role"])
            return
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"worker ended ({d['terminal_classification']}) without submitting")
        state.emit(con, run, "task.blocked", f"{task['id']} worker ended without submitting "
                   f"({d['terminal_classification']}); work is preserved in its worktree", task_id=task["id"])


def _quota_wall(run: dict, d: dict) -> str | None:
    """The harness's own quota/rate-limit line when a worker ended nonzero on
    one. Only the last few log lines count: the harness prints the wall last,
    while the worker's narration above it may mention quotas for any reason."""
    if d.get("terminal_classification") in (None, "success"):
        return None
    from office import gates
    text = gates._log_text(d, paths.run_dir(run["id"]) / "dispatches" / d["id"])
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()][-5:][::-1]:
        if not line.startswith("[office]") and gates._quota_signature(line):
            return line[:200]
    return None


def job_notify_worker(con, run: dict, job: dict) -> dict:
    """Best-effort native nudge to a live Herdr-hosted worker. Delivery truth
    stays in runs.db and rides on the worker's next office command; an
    amendment's prompt that lands is recorded delivered, and one that cannot
    reach the worker records why it stayed queued."""
    from office import amend, gates
    payload = job["payload"]
    d = state.get_dispatch(con, payload["dispatch_id"])
    unblock = bool(payload.get("unblock"))

    def note(delivered: bool, reason: str) -> None:
        try:  # the record is bookkeeping: failing it never undoes or skips what was sent
            amend.record_notify(con, run, payload, delivered=delivered, reason=reason)
        except Exception:
            pass

    if not d or d.get("launcher") != "herdr" or not d.get("pane_id") or d["status"] != "running" \
            or (unblock and not gates._agent_alive(herdr_agent_name(d["id"]))):
        if unblock and d:
            _amendment_undelivered(con, run, payload, d)
        note(False, amend.unreachable_reason(d))
        return {"sent": False}
    wrong = _pane_mismatch(run, d, d["pane_id"], Path(d["worktree"]), check_cwd=True,
                          agent=herdr_agent_name(d["id"])) if d.get("worktree") else None
    if wrong:  # the recorded pane is someone else's now: nothing is typed into it
        if unblock:
            _amendment_undelivered(con, run, payload, d)
        note(False, f"not sent: {wrong}")
        return {"sent": False}
    text = payload.get("text", "office status has an update for you.")
    landed = submit_prompt(d["pane_id"], text, pane=d["pane_id"])
    if unblock:
        if landed == "landed":
            from office import db, submit
            with db.transaction(con):
                task = state.get_task(con, run["id"], payload["task_id"])
                # Still this dispatch's own block (a revoke or newer owner since keeps its blocker).
                current = con.execute("SELECT amendment_id FROM deliveries WHERE run_id=? AND task_id=? AND dispatch_id=? "
                                      "AND status IN ('queued','delivered') ORDER BY target_version DESC LIMIT 1",
                                      (run["id"], payload["task_id"], d["id"])).fetchone()
                # Only the current amendment for the current block lifts it; a stale notice leaves it.
                fresh = bool(current and current["amendment_id"] == payload.get("amendment_id")
                             and submit.block_id(con, d["id"]) == payload.get("block_id"))
                if task and fresh and task["current_dispatch_id"] == d["id"] and submit.unblock_self(con, run, task):
                    state.emit(con, run, "task.unblocked", f"{task['id']} unblocked: {payload.get('amendment_id')} "
                               "delivered to the live worker, which resubmits", audience="runtime", task_id=task["id"],
                               dispatch_id=d["id"])
        else:
            _amendment_undelivered(con, run, payload, d)
    pane = d["pane_id"]
    note(landed == "landed", f"prompt landed in pane {pane}" if landed == "landed" else
         f"prompt typed but unsubmitted in pane {pane} when last checked" if landed == "held" else
         f"prompt sent to pane {pane} but no landed signal")
    return {"sent": True, "landed": landed}


def _amendment_undelivered(con, run: dict, payload: dict, d: dict) -> None:
    """The amendment could not be confirmed delivered to a live agent: the blocker
    stays, and the orchestrator is told what to do instead."""
    from office import db
    from office import submit
    tid = payload["task_id"]
    with db.transaction(con):
        task = state.get_task(con, run["id"], tid)
        current = con.execute("SELECT amendment_id FROM deliveries WHERE run_id=? AND task_id=? AND dispatch_id=? "
                              "AND status IN ('queued','delivered') ORDER BY target_version DESC LIMIT 1",
                              (run["id"], tid, d["id"])).fetchone()
        # Only while this dispatch still owns the task, blocked by this block, with this delivery
        # current: otherwise the notice is stale and "revoke" would hit the current owner.
        if not (task and task["current_dispatch_id"] == d["id"] and submit.self_blocked(task)
                and submit.block_id(con, d["id"]) == payload.get("block_id")
                and current and current["amendment_id"] == payload.get("amendment_id")):
            return
        state.emit(con, run, "task.amend_undelivered", f"{tid} {payload.get('amendment_id')} not confirmed delivered "
                   f"to {d['id']}; its blocker stays: office revoke {tid}, then office rerun {tid} --resume|--fresh", task_id=tid, dispatch_id=d["id"])


def start_stacked(con, run: dict, accepted_task: str) -> list[str]:
    """Launch tasks the orchestrator stacked after `accepted_task`. Caller holds tx."""
    started = []
    for t in state.tasks(con, run["id"]):
        if t["status"] == "queued" and t.get("stack_after") == accepted_task:
            graph = {x["id"]: x["depends"] for x in state.tasks(con, run["id"])}
            try:
                base = _base_for(con, run, t, graph, accepted_task)
            except Refused as e:
                # The acceptance that released this task must still commit: park the task and tell the orchestrator.
                state.update_task(con, run["id"], t["id"], status="paused", stack_after=None, pause_reason=e.message[:200])
                state.signal_orchestrator(con, run, source="stacked start refused", task_id=t["id"], dispatch_id=None,
                                          reason=e.message, next_step=e.next_step or f"office dispatch {t['id']}")
                continue
            request_launch(con, run, t["id"], role="executor", base=base)
            started.append(t["id"])
    return started
