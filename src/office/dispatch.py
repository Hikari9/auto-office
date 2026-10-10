"""Strategy-owned dispatch with runtime-owned mechanics.

The orchestrator says which tasks run and whether in parallel. The runtime
routes each role, checks dependencies and scope ownership, takes a fenced lease,
creates the worktree, builds the packet (with the run-pinned office_version),
injects the environment, and launches through a durable outbox job.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
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

from office import adapters, briefs, candidates, contract, db, frontdoor, jobs, paths, planfile, planpath, read_scope, route_policy, route_probe, routing, scoring, state, version, worktree_setup
from office.result import Result
from office.state import Refused, Usage
from office.util import (DEAD, atomic_write_json, atomic_write_text, claim_signalable, dumps, now_iso, parse_iso,
                         pid_alive, pid_state,
                         process_is, process_start, sha256_obj, short, loads)

LEASE_TTL_SECONDS = 4 * 3600
# A harness session id lands in a resume argv: plain id characters only, never a leading dash.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Only an agent carries this: the dispatch id it was launched for, which names every process it starts. Kept apart
# from OFFICE_DISPATCH_ID, which authority checks read, so no job, supervisor or other command a worker runs
# passes the tag on to a process that is not the worker's.
_WORKER_TAG = "OFFICE_WORKER_TAG"
IDENTITY_ENV = ("OFFICE_WORKER_TAG", "OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE", "OFFICE_STATE_DIR",
                "OFFICE_SESSION", "OFFICE_HARNESS", "OFFICE_VERSION", "OFFICE_FRONT_DOOR_HOPS")
PLANNER_TASK = "P1"

# Startup screens that can hold a harness before Herdr sees it ready. Office
# answers only a folder-trust dialog, and only for a directory it created for
# the run (`_office_owned`); hook trust, imports and updates stay the user's (#399).
TRUST_SCREEN = "folder-trust 'Trust this folder'"
EXTERNAL_IMPORTS_SCREEN = "external CLAUDE.md imports dialog"
_STARTUP_SCREEN_MARKERS = (
    ("hooks need review", "Codex 'Hooks need review'"),
    ("trust this folder", TRUST_SCREEN),
    # Claude Code: "Quick safety check: Is this a project you created or one you trust?"
    ("quick safety check", TRUST_SCREEN),
    ("is this a project you created or one you trust", TRUST_SCREEN),
    ("allow external claude.md file imports", EXTERNAL_IMPORTS_SCREEN),
    ("update available", "'Update available' prompt"),
)


# A launch receipt must never turn adapter arguments or a prompt into a credential leak.
_LAUNCH_SECRET = re.compile(r"(?i)(?:api[_-]?key|access[_-]?key|private[_-]?key|password|passwd|secret|token(?!s)"
                            r"|credential|authorization|bearer|cookie|session[_-]?key|\bauth\b|\bpat\b|\bpass\b)")
# Flags whose value is a secret or a prompt even though the flag name does not say so.
_HIDE_VALUE_FLAGS = {"--auth", "--key", "--pat", "--pass", "-p", "--prompt"}
_ENV_FLAGS = {"-e", "--env", "--set-env"}
# Credential shapes caught wherever they appear, whatever the flag or key name says.
_SECRET_VALUE = re.compile(r"(?:sk-[A-Za-z0-9_-]{8,}|gh[opsur]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|xox[abpr]-[\w-]{8,}"
                           r"|glpat-[\w-]{8,}|AKIA[0-9A-Z]{16}|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+|AIza[\w-]{30,})")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_REDACTED = "[REDACTED]"


def _redact_arg(arg: str) -> str:
    named = _LAUNCH_SECRET.search(arg)
    if named:
        # Keep everything before the secret's own separator; whatever follows it is the value.
        sep = re.search(r"[=:]", arg[named.end():])
        if sep is None:
            return _REDACTED
        cut = named.end() + sep.end()
        return arg[:cut] + (" " if arg[cut - 1] == ":" else "") + _REDACTED
    arg = _URL_USERINFO.sub(lambda m: m.group(1) + _REDACTED + "@", arg)
    return _SECRET_VALUE.sub(_REDACTED, arg)


def _redact_launch_argv(argv: list[str]) -> list[str]:
    """Redact an argv for evidence. A secret-named flag hides its next argument;
    `name=value` and `Header: value` keep the name and hide the value; an env
    flag's `NAME=value` keeps only the name."""
    safe: list[str] = []
    hide = None
    for arg in argv:
        if hide == "value":
            safe.append(_REDACTED)
        elif hide == "env":
            safe.append(arg.split("=", 1)[0] + "=" + _REDACTED if "=" in arg else _REDACTED)
        elif arg.startswith("-") and not re.search(r"[=:]", arg) and (arg in _HIDE_VALUE_FLAGS or _LAUNCH_SECRET.search(arg)):
            safe.append(arg)
            hide = "value"
            continue
        elif arg in _ENV_FLAGS:
            safe.append(arg)
            hide = "env"
            continue
        else:
            safe.append(_redact_arg(arg))
        hide = None
    return safe


def write_launch_spec(run: dict, dispatch_id: str, spec: dict) -> None:
    """launch.json is private to the user: it carries argv evidence and paths (#500)."""
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "launch.json", spec, mode=0o600)


def _record_launch_form(run: dict, dispatch: dict, spec: dict, form: str, argv: list[str],
                        *, transport: str, **fields) -> None:
    """Persist the actual rendered launch form, never the prompt or secret arguments (#500)."""
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or dispatch.get("harness") or "")
    route = dispatch.get("route") or {}
    cand = route.get("candidate") or {}
    spec.setdefault("rendered_launches", {})[form] = {
        "argv": _redact_launch_argv(argv),
        "prompt_transport": transport,
        "adapter_hash": adapters.adapter_hash(adapter) if adapter else None,
        "harness_version": cand.get("harness_version") or dispatch.get("harness_version"),
        **fields,
    }
    write_launch_spec(run, dispatch["id"], spec)


# ------------------------------------------------------------------ planner task

def create_planner_task(con, run: dict, *, contract_request: str | None = None, decision: dict | None = None) -> str:
    """Queue the dedicated planner. Caller holds the transaction."""
    now = now_iso()
    existing = state.get_task(con, run["id"], PLANNER_TASK)
    from office import gates
    live = gates.live_task_session(con, run["id"], PLANNER_TASK)
    if live:
        # A planner cannot be superseded while it is writing PLAN.md. The new
        # amendment is already durable in amendments. A queued planner picks it
        # up when its packet is built; a running one acknowledges only what its
        # packet carried, so the request waits for the next revision (#506).
        if contract_request:
            d = state.get_dispatch(con, live)
            if d and d.get("status") == "running":
                state.enqueue(con, run, "notify_worker",
                              {"dispatch_id": live, "task_id": PLANNER_TASK,
                               "text": f"CONTRACT AMENDMENT QUEUED: {contract_request}. "
                                       "It is not part of your brief; Office plans it in a follow-up "
                                       "revision after you submit. Finish the current revision as briefed."},
                              dedup_key=f"planner-amend:{live}:{contract_request.split(':', 1)[0]}",
                              max_attempts=1)
        return live
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
        candidates.declared_decision(review_as, flag="--review-as", role="code_reviewer",
                                    config=state.pinned_config(run))  # validates shape and role fit
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
    for index, tid in enumerate(task_ids):
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
            routes[tid] = candidates.declared_decision(as_model, role="executor",
                                                       config=state.pinned_config(run))
        else:
            routes[tid] = planned_route(con, run, task, override=route, reroute=reroute)
            if not (route or launch_prefs) and (parallel or index == 0):  # a stacked task launches later, not now
                routes[tid] = preflight_discovery(con, run, task, routes[tid], reroute=reroute)
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
            released = stack_released(con, run, after) if task["status"] == "queued" and after else None
            if released:
                # Stacked after a task that was accepted, or that submitted and whose session
                # ended, before this dispatch: nothing else starts it, so launch it now.
                state.update_task(con, run["id"], tid, stack_after=None, pause_reason=None)
                decision = routes.get(tid) if (routes.get(tid) or {}).get("status") == "selected" else None
                if decision:
                    decision = settle_discovery(con, run, task, decision, launching=True)
                    _record_routing(con, run, decision)
                    note_route(con, run, task, decision)
                parts: list = []
                did = request_launch(con, run, tid, role="executor", decision=decision,
                                     base=_base_for(con, run, task, graph, after, parents=parts),
                                     extra={"base_parents": parts})
                if decision:
                    record_discovery(con, run, task, decision, did)
                res.add(f"{tid} was stacked after {after}, which {released} -> {did} launching")
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
            holder = (state.get_task(con, run["id"], stack_after) or {}) if stack_after else {}
            holder_accepted = holder.get("status") == "accepted"
            parts = []
            base = _base_for(con, run, task, graph, stack_after, queued=bool(stack_after) and not holder_accepted,
                             parents=parts)
            if holder_accepted:
                # Nothing would release a stack on an accepted task: launch now,
                # based on its accepted revision (base above).
                stack_after = None
            decision = routes[tid]
            if decision.get("status") != "selected":
                raise Refused("no-route", _route_failure(tid, decision), scope=tid,
                              preserved="plan and other dispatches", next_step=_route_next(decision, tid, run))
            decision = settle_discovery(con, run, task, decision, launching=not stack_after)
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
                note_route(con, run, task, decision)
                res.add(f"{tid} stacked after {stack_after}")
            else:
                # A planned slate reports its own fallback; drift is for legacy previews.
                drift = None if decision.get("route_source") == "plan" else plan_view.drift(con, run, tid, decision)
                if drift:
                    res.add(drift)
                note_route(con, run, task, decision)
                did = request_launch(con, run, tid, role="executor", decision=decision, base=base,
                                     extra={"base_parents": parts})
                record_discovery(con, run, task, decision, did)
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


def planned_route(con, run: dict, task: dict, *, override: str | None = None, reroute: bool = False,
                  exclude: set[str] | None = None, discovery_input: dict | None = None) -> dict:
    """Route an executor dispatch: the planner's primary, else its fallbacks in order.

    Live evidence (quota, availability, trust, learned eligibility) is refreshed
    by routing again now; the planned routes are then tried in their recorded
    order and the first one that still qualifies runs. A route is never swapped
    for an unplanned one: when every planned route fails, the result is
    `slate_exhausted` and the orchestrator reroutes (`--reroute`). Without a
    planned slate (`--route`, `--reroute`, a pre-#300 plan) the fresh decision stands.

    `discovery_input` is the preflight's recompute handle (#494): a decision that comes back
    with `intent: "trial"` stands whole, because the trial route is the one route a planned
    slate cannot name. `exclude` keeps routes out of the decision (the trial route, when the
    known-working fallback is wanted)."""
    tid = task["id"]
    kind = "fix" if task.get("current_dispatch_id") else "fresh"
    planned = None if (override or reroute) else _effective_slate(con, run, task)
    # A declared route is a manual one: overkill rules never remove it. Denial, quarantine,
    # capability, permission, archive and quota gates still apply to the fresh evidence.
    declared = planned["primary"] if planned and planned.get("chooser") == "declared" else None
    fresh = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id=tid, override=override,
                                  dispatch_kind=kind, manual=bool(declared), declared=declared,
                                  exclude=exclude, discovery_input=discovery_input)
    if reroute and fresh.get("status") == "selected":
        fresh["route_source"], fresh["route_note"] = "reroute", "rerouted from current evidence"
    if discovery_input and fresh.get("status") == "selected" and (fresh.get("discovery") or {}).get("intent") == "trial":
        fresh.setdefault("route_source", "router")
        return fresh
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
    chooser = planned.get("chooser")
    label = {"declared": "declared route", "recorded": "recorded route"}.get(chooser, "planned primary") if i == 0 \
        else f"planned fallback {i}"
    note = label if i == 0 else f"{label}; " + "; ".join(f"{t['route']}: {t['reason']}" for t in taken)
    disclosure["reason"] = f"{disclosure['reason'].split('; ')[0]}; {note}"
    disclosure["adaptive"] = True
    disclosure["route_plan"] = {k: planned.get(k) for k in ("primary", "fallbacks", "chooser", "audit_id")}
    return {**fresh, "status": "selected", "selected": rid, "candidate": cand, "selection_disclosure": disclosure,
            "route_source": "plan", "route_note": None if i == 0 else note, "fallbacks_taken": taken,
            "planned": planned,
            "decision_hash": sha256_obj({"planned": planned.get("decision_hash"), "fresh": fresh.get("decision_hash"),
                                         "dispatched": rid, "fallbacks_taken": taken})}


def _effective_slate(con, run: dict, task: dict) -> dict | None:
    """The slate dispatch tries in order (#426). A route already recorded on the
    task (an earlier dispatch, a stack, or a deliberate `office amend route`) is
    the primary, so a re-dispatch restores it instead of recomputing; a declared
    one has no fallbacks, because a deliberate choice is never swapped silently.
    Otherwise the approved plan's slate."""
    plan = _planned_slate(con, run, task["id"])
    rec = state.recorded_route(task)
    if not rec:
        return plan
    primary = routing.candidate_id(rec["candidate"])
    declared = bool(rec.get("declared"))
    order = [] if declared else [r for r in [(plan or {}).get("primary"), *((plan or {}).get("fallbacks") or [])]
                                 if r and r != primary]
    return {**(plan or {}), "primary": primary, "fallbacks": order, "chooser": "declared" if declared else "recorded",
            "planned_primary": (plan or {}).get("primary")}


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
        link, record = decision.get("discovery_link"), decision.get("trial_record")
        if link:
            audit["dispatch"]["discovery"] = {
                "attempt_id": link["attempt_id"], "probe_key": link["probe_key"], "policy_digest": link["policy_digest"],
                "intent": "trial" if record else "none", "blocked": link.get("blocked"),
                "trial_reason": record["reason"] if record else None,
                "fallback_route": record["fallback_route"] if record else decision.get("selected")}
        explored = source != "plan" and (audit.get("exploration") or {}).get("picked") == decision.get("selected")
        decision["audit_id"] = plan_view.record_audit(con, run, audit, plan_version=run.get("plan_version"),
                                                      dispatched=decision.get("selected"), explored=explored)


def _route_payload(decision: dict) -> dict:
    """What a dispatch keeps of its decision, so a stacked start or a relaunch
    reproduces the same route, override, and launch form."""
    out = {"candidate": decision.get("candidate"), "selection_disclosure": decision.get("selection_disclosure")}
    for key in ("override", "launch", "benchmark_snapshot", "route_source", "fallbacks_taken", "audit_id"):
        if decision.get(key):
            out[key] = decision[key]
    unknown = candidates.quota_unknown_record(decision.get("candidate"))
    if unknown and decision.get("candidate"):
        out["quota_unknown"] = unknown
    if (decision.get("planned") or {}).get("chooser") == "declared" or decision.get("declared") \
            or decision.get("override"):
        out["declared"] = True  # a deliberate route survives the next dispatch of the task
    link = decision.get("discovery_link")
    if link:
        record = decision.get("trial_record") or {}
        out["discovery"] = {"attempt_id": link["attempt_id"], "intent": "trial" if record else "none",
                            "blocked": link.get("blocked"), "probe_key": link["probe_key"],
                            "candidate": link["candidate"], "policy_digest": link["policy_digest"],
                            "reason": record.get("reason"), "fallback_route": record.get("fallback_route"),
                            "fallback": record.get("fallback")}
    return out


def note_route(con, run: dict, task: dict, decision: dict) -> None:
    """Record the effective route on the task before its agent runs (#426), and a
    `route.changed` event when it differs from what Office would have run:
    a fallback (quota, unavailable), `--reroute`, or an explicit `--as`/`--route`.
    Caller holds the transaction."""
    tid = task["id"]
    after = decision["selected"]
    norm = scoring.normalize_triple
    rec = state.recorded_route(task)
    recorded = routing.candidate_id(rec["candidate"]) if rec else None
    source = decision.get("route_source") or ("override" if decision.get("override") else "router")
    planned = decision.get("planned") or {}
    taken = decision.get("fallbacks_taken") or []
    why = "; ".join(f"{t['route']}: {t['reason']}" for t in taken)
    if taken:
        before = planned.get("primary")
        kind = "quota" if any("quota" in t["reason"] for t in taken) else "unavailable"
        change = (before, kind, f"fallback: {why}", "office")
    elif decision.get("trial_record"):
        before = decision["trial_record"]["fallback_route"]
        change = (before, "trial", decision["trial_record"]["reason"], "office")
    elif source == "reroute":
        before = recorded or ((state.task_dispatches(con, run["id"], tid) or [{}])[-1].get("triple"))
        change = (before, "reroute", "rerouted from current evidence", "orchestrator")
    elif source == "override":
        before = recorded or (_planned_slate(con, run, tid) or {}).get("primary")
        change = (before, "override", (decision.get("selection_disclosure") or {}).get("reason") or "route override",
                  "user")
    else:
        before, change = None, None
    if change and before and norm(before) != norm(after):
        state.record_route_change(con, run, tid, before, after, kind=change[1], reason=change[2], actor=change[3])
    state.update_task(con, run["id"], tid, route_json=dumps(_route_payload(decision)))


# ------------------------------------------------------------------ route discovery (#494)

TRIAL_OPEN = ("reserved", "launched")
PINNED_CHOOSERS = ("planner", "declared", "recorded")  # a route a person or an approved plan chose is never traded for a trial
_TRIAL_EVENTS = {"launched": "trial-launched", "launch-failed": "trial-launch-failed", "fell-back": "trial-fell-back",
                 "submitted": "trial-submitted", "abandoned": "trial-abandoned"}
_TRIAL_FROM = {"launched": ("reserved",), "launch-failed": TRIAL_OPEN, "abandoned": TRIAL_OPEN,
               "submitted": TRIAL_OPEN, "fell-back": ("launch-failed",)}


def _policy_digest(config: dict) -> str:
    return config.get(route_policy.DIGEST_KEY) or route_policy.policy_digest(config)


def _annotated(decision: dict, link: dict, *, blocked: str | None = None) -> dict:
    """`decision` with what discovery did to it: the audit's `discovery` block and the link the
    dispatch transaction records. Never a trial: a decision that carries a link without a
    `trial_record` dispatches its known-working route."""
    out = {k: v for k, v in decision.items() if k not in ("trial", "trial_record")}
    if link.get("attempt_id"):
        out["discovery_link"] = {**link, "intent": "none", "blocked": blocked}
    audit = out.get("routing")
    if audit:
        block = {**(audit.get("discovery") or {}), "intent": "none", "blocked": blocked}
        if link.get("attempt_id"):
            block.update(attempt_id=link["attempt_id"], candidate=link["candidate"], probe_key=link["probe_key"])
        out["routing"] = {**audit, "discovery": block}
    return out


def preflight_discovery(con, run: dict, task: dict, decision: dict, *, reroute: bool = False) -> dict:
    """D5 (#494): turn a `probe` intent into a trial or a known-working dispatch, before any lease,
    worktree claim or session exists. The attempt id is minted first, so the probe's events exist before
    a dispatch row does. `route_probe.ensure` reserves atomically and then probes (outside any
    transaction). The route is then decided again with the attempt's handle, and only that recomputed
    decision is used: a probe that failed or was refused, or a quota, permission or fingerprint that
    changed since, comes back as a route without a trial, and that route is dispatched."""
    disc = decision.get("discovery") or {}
    if decision.get("status") != "selected" or disc.get("intent") != "probe":
        return decision
    candidate = next((c for c in (decision.get("request") or {}).get("candidates") or []
                      if routing.candidate_id(c) == disc.get("candidate")), None)
    if (decision.get("planned") or {}).get("chooser") in PINNED_CHOOSERS or candidate is None:
        return _annotated(decision, {}, blocked="pinned-route" if candidate else "candidate-gone")
    config = state.pinned_config(run)
    link = {"attempt_id": route_policy.new_attempt_id(), "candidate": disc["candidate"],
            "probe_key": disc.get("probe_key"), "policy_digest": _policy_digest(config), "primary": decision["selected"]}
    context = {"origin": "preflight", "role": "executor", "task_id": task["id"],
               "reason": f"discovery: untried route {link['candidate']} drawn for an exact probe; "
                         f"{decision['selected']} stays the known-working fallback",
               "primary_route": decision["selected"], "fallback_route": decision["selected"]}
    try:
        outcome = route_probe.ensure(con, run, candidate, attempt_id=link["attempt_id"], context=context)
        if isinstance(outcome, route_probe.Refused):
            return _annotated(decision, link, blocked=outcome.reason)
        handle = {"candidate": link["candidate"], "probe_key": link["probe_key"], "reservation_id": link["attempt_id"],
                  "attempt_id": link["attempt_id"]}
        fresh = planned_route(con, run, task, reroute=reroute, discovery_input=handle)
        block = fresh.get("discovery") or {}
        if fresh.get("status") != "selected" or block.get("intent") != "trial":
            return _annotated(fresh, link, blocked=block.get("blocked") or "no-trial")
        fallback = planned_route(con, run, task, reroute=reroute, exclude={link["candidate"]})
    except Refused as refused:
        if refused.category == "policy-unreadable":  # nothing is offered while a denial cannot be ruled out
            raise
        return _annotated(decision, link, blocked=f"probe-error: {type(refused).__name__}")
    except Exception as exc:  # discovery never costs the dispatch: the known-working decision stands
        return _annotated(decision, link, blocked=f"probe-error: {type(exc).__name__}")
    if fallback.get("status") != "selected":
        return _annotated(fallback, link, blocked="no-fallback")
    reason = (f"discovery trial: a fresh exact conformance probe passed for {link['candidate']} (attempt "
              f"{link['attempt_id']}); falls back to known-working {fallback['selected']}")
    return {**fresh, "discovery_link": {**link, "intent": "trial", "blocked": None},
            "trial": {"fallback": fallback, "reason": reason}}


def settle_discovery(con, run: dict, task: dict, decision: dict, *, launching: bool) -> dict:
    """The dispatch transaction's last word on a trial. Re-reads the caps, task risk, quota reserve, probe
    record and fallback under the write lock; any failure dispatches the known-working fallback instead
    and writes no `route_trials` row. Caller holds the transaction."""
    trial = decision.get("trial")
    if not trial:
        return decision
    fallback, link = trial["fallback"], decision["discovery_link"]
    blocked = None if launching else "not-launching"
    blocked = blocked or _trial_blocked(con, run, decision, fallback)
    if blocked:
        return _annotated(fallback, link, blocked=blocked)
    record = {"id": link["attempt_id"], "route": link["candidate"], "probe_key": link["probe_key"],
              "fallback_route": fallback["selected"], "policy_digest": link["policy_digest"],
              "reason": trial["reason"], "fallback": _route_payload(fallback)}
    return {**{k: v for k, v in decision.items() if k != "trial"}, "trial_record": record}


def _known_working(con, cand: dict) -> bool:
    """The routing's own definition (`candidates.discovery_inputs`): an available route that is proven, or
    learned eligible, and not quarantined."""
    from office import route_learning
    _, trust = scoring.evaluate_trust_state(con, routing.candidate_id(cand))
    if trust == "quarantined" or cand.get("route_status", "available") != "available":
        return False
    if trust == "proven":
        return True
    learned = route_learning.current_eligibility(con, "executor")
    return (learned.get(route_learning.candidate_key(cand)) or {}).get("state") == "learned-eligible"


def _trial_blocked(con, run: dict, decision: dict, fallback: dict) -> str | None:
    """Why no trial may be reserved now (a routing `blocked` token), or None."""
    config = state.pinned_config(run)
    settings = route_policy.discovery_settings(config)
    cand, link = decision["candidate"], decision["discovery_link"]
    risk = run.get("risk") or {}
    if not (settings["enabled"] and "executor" in settings["roles"]):
        return "discovery-disabled"
    if (risk.get("size_class") not in settings["trial_size_classes"]
            or risk.get("blast_radius") not in settings["trial_blast_radius"] or risk.get("irreversible")):
        return "risk"
    alloc = route_probe.allocation(con, run["id"], settings)
    if alloc["trials"]["used"] >= alloc["trials"]["max"]:
        return "trial-cap"
    if alloc["rolling"]["used"] >= alloc["rolling"]["max"]:
        return "rolling-cap"
    if route_probe._quota_refusal(cand, config):
        return "quota-reserve"
    probe = route_probe.status(con, cand)
    if not probe or probe.get("result") != "pass" or cand.get("probe_key") != link["probe_key"]:
        return "probe-stale"
    if scoring.evaluate_trust_state(con, link["candidate"])[1] == "quarantined":
        return "quarantined"
    # No trial without a known-working fallback on record, whoever launched this dispatch.
    if (fallback.get("status") != "selected" or not fallback.get("candidate")
            or routing.candidate_id(fallback["candidate"]) == link["candidate"]
            or not _known_working(con, fallback["candidate"])):
        return "no-fallback"
    return None


def _discovery_event(con, kind: str, attempt_id: str, *, origin: str, run_id: str, task_id: str | None,
                     dispatch_id: str | None, reason: str, outcome: str | None = None, detail: str | None = None,
                     primary: str | None = None, fallback: str | None = None) -> None:
    """Append one immutable event to an attempt, in the caller's transaction. Identity (fingerprint,
    candidate, probe key, policy digest) is read off the attempt's own earlier events; no event is updated."""
    base = con.execute("SELECT * FROM route_discovery_events WHERE attempt_id=? ORDER BY seq DESC LIMIT 1",
                       (attempt_id,)).fetchone()
    if base is None:
        return
    run = state.get_run(con, run_id)
    settings = route_policy.discovery_settings(state.pinned_config(run))
    route_policy.record_event(
        con, kind=kind, attempt_id=attempt_id, origin=origin, policy_digest=base["policy_digest"],
        probe_key=base["probe_key"], reason=reason, run_id=run_id, plan_version=run.get("plan_version"),
        task_id=task_id, dispatch_id=dispatch_id, role="executor", fingerprint_json=base["fingerprint_json"],
        candidate_route=base["candidate_route"], primary_route=primary or base["primary_route"],
        fallback_route=fallback or base["fallback_route"], probe_freshness=base["probe_freshness"],
        allocation_json=route_probe.allocation(con, run_id, settings), outcome=outcome, detail=detail)


def record_discovery(con, run: dict, task: dict, decision: dict, dispatch_id: str) -> None:
    """In the dispatch transaction, right after the dispatch row: `dispatch-linked` names the dispatch an
    attempt ended in (the fallback's, after a failed or refused probe), and a trial adds its `route_trials`
    row and `trial-reserved`. Earlier events are never touched. Caller holds the transaction."""
    link = decision.get("discovery_link")
    if not link:
        return
    record = decision.get("trial_record")
    tid = task["id"]
    now = now_iso()
    if record:
        con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                    "policy_digest, reason, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], run["id"], tid, dispatch_id, "executor", record["route"], record["probe_key"],
                     record["fallback_route"], record["policy_digest"], record["reason"], "reserved", now, now))
    dispatched = decision.get("selected")
    detail = (record["reason"] if record else
              f"no trial: {link.get('blocked') or 'none'}; dispatched known-working {dispatched}")
    _discovery_event(con, "dispatch-linked", link["attempt_id"], origin="dispatch", run_id=run["id"], task_id=tid,
                     dispatch_id=dispatch_id, reason=f"attempt linked to dispatch {dispatch_id}",
                     outcome="trial" if record else "fallback", detail=detail, primary=link.get("primary"),
                     fallback=record["fallback_route"] if record else dispatched)
    if record:
        _discovery_event(con, "trial-reserved", link["attempt_id"], origin="dispatch", run_id=run["id"], task_id=tid,
                         dispatch_id=dispatch_id, reason=record["reason"], outcome="reserved", detail=record["reason"],
                         primary=link.get("primary"), fallback=record["fallback_route"])


def open_trial(con, dispatch_id: str | None) -> dict | None:
    """The live trial this dispatch is, or None (an ordinary dispatch, or a trial already settled)."""
    row = con.execute(f"SELECT * FROM route_trials WHERE dispatch_id=? AND status IN ({','.join('?' * len(TRIAL_OPEN))})",
                      (dispatch_id, *TRIAL_OPEN)).fetchone() if dispatch_id else None
    return dict(row) if row else None


def set_trial_status(con, trial: dict, status: str, *, origin: str = "dispatch", detail: str = "",
                     outcome: dict | None = None, dispatch_id: str | None = None) -> bool:
    """Move a trial to `status` and append its `trial-*` event in the same transaction. A trial that is not
    in a state this move is allowed from is left alone (False): every status change happens once."""
    cur = con.execute(f"UPDATE route_trials SET status=?, outcome=COALESCE(?, outcome), updated_at=? WHERE id=? "
                      f"AND status IN ({','.join('?' * len(_TRIAL_FROM[status]))})",
                      (status, dumps(outcome) if outcome is not None else None, now_iso(), trial["id"],
                       *_TRIAL_FROM[status]))
    if cur.rowcount != 1:
        return False
    _discovery_event(con, _TRIAL_EVENTS[status], trial["id"], origin=origin, run_id=trial["run_id"],
                     task_id=trial["task_id"], dispatch_id=dispatch_id or trial["dispatch_id"],
                     reason=detail or f"trial {status}", outcome=status, detail=detail or None,
                     fallback=trial["fallback_route"])
    return True


def trial_submitted(con, dispatch_id: str) -> bool:
    """A trial dispatch's revision exists. Caller holds the transaction."""
    trial = open_trial(con, dispatch_id)
    return bool(trial) and set_trial_status(con, trial, "submitted", detail="a revision was submitted")


def abandon_trial(con, dispatch_id: str, why: str) -> bool:
    """A trial that ends without a revision and without a recovery (revoked, re-routed by the user, ended
    on a question). Caller holds the transaction."""
    trial = open_trial(con, dispatch_id)
    return bool(trial) and set_trial_status(con, trial, "abandoned", detail=why)


def _trial_launched(con, dispatch_id: str) -> None:
    trial = con.execute("SELECT * FROM route_trials WHERE dispatch_id=? AND status='reserved'", (dispatch_id,)).fetchone()
    if trial:
        set_trial_status(con, dict(trial), "launched", detail="the dispatch's agent was launched")


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
        kind_name = "worker" if output is None else "reviewer"
        inter = adapters.interactive_argv(adapter, kind_name, model=d.get("model") or "",
                                          effort=d.get("effort") or "none", cwd=Path(wt),
                                          output=Path(output) if output else None,
                                          session_id=_assigned_session(d)) if adapter and d.get("model") else None
        if inter is None and adapter and d.get("model") and adapters.profile(adapter, kind_name):
            # No pane-hosted form: show the recorded headless invocation instead
            # of a bare executable, which would run on the harness's default
            # model with none of the pinned trust flags.
            try:
                headless, _ = adapters.build_argv(adapter, kind_name, model=d.get("model") or "",
                                                  effort=d.get("effort") or "none", cwd=Path(wt),
                                                  output=Path(output) if output else None,
                                                  session_id=_assigned_session(d))
            except adapters.AdapterError:
                headless = None
            if headless:
                inter = (headless[1:], d.get("harness") or "<kind>")
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


def _base_for(con, run: dict, task: dict, graph: dict, stack_after: str | None, *, queued: bool = False,
              parents: list | None = None) -> str | None:
    """Base commit: the run base, or one commit combining every dependency revision this task builds on.

    Each dependency contributes its accepted revision. One with none is not used silently: only the
    `stack_after` holder released while submitted (#398) and a v3.1 run (a dependant may start on a
    submitted revision, Q16) build on its current one, and the brief names that revision unaccepted.
    A dependency with no revision at all refuses. Heads that are ancestors of another drop out and the
    rest combine; heads that conflict refuse naming both tasks and the paths (#489). `queued` is a task
    that only waits for its holder: its base is computed when it starts, so only the existence check runs.
    `parents` receives {"task", "revision", "commit", "accepted"} for each dependency used."""
    from office import integration
    heads = []
    for dep in dict.fromkeys([*task["depends"], *([stack_after] if stack_after else [])]):
        d = state.get_task(con, run["id"], dep) or {}
        rev_id = d.get("accepted_revision_id")
        accepted = bool(rev_id)
        if not accepted and (queued or dep == stack_after or contract.of(run) == contract.LEGACY):
            rev_id = d.get("current_revision_id")
        if not rev_id:
            if dep == stack_after:
                continue
            if d.get("current_revision_id"):
                raise Refused("dependency-not-ready", f"{task['id']} depends on {dep}, which is {d['status']} on "
                              f"{d['current_revision_id']} and not accepted; a base must not carry unreviewed work",
                              scope=task["id"], next_step=f"wait for {dep} to be accepted, or office dispatch {dep} "
                                                          f"{task['id']} (stacked)")
            raise Refused("dependency-not-ready", f"{task['id']} depends on {dep}, which has no submitted revision",
                          scope=task["id"], next_step=f"dispatch {dep} first, or office dispatch {dep} {task['id']} (stacked)")
        sha = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone()["commit_sha"]
        heads.append({"task": dep, "revision": rev_id, "commit": sha, "accepted": accepted})
    if queued:
        return None
    if parents is not None:
        parents.extend(heads)
    if not heads:
        return run["base_sha"]
    try:
        return integration.combine(run, [(h["task"], h["commit"]) for h in heads])
    except integration.CombineConflict as exc:
        raise Refused("dependency-conflict", f"{task['id']} cannot build on both {exc.left} and {exc.right}: their "
                      f"accepted revisions conflict on {', '.join(exc.paths[:6])}", scope=task["id"],
                      preserved="both tasks' revisions",
                      next_step=f"amend the plan so {exc.left} and {exc.right} do not edit the same lines, or "
                                f"depend {task['id']} on one of them") from exc


# ------------------------------------------------------------------ launch request

def request_launch(con, run: dict, task_id: str, *, role: str, decision: dict | None = None,
                   base: str | None = None, extra: dict | None = None, fix_of: str | None = None,
                   replaces: str | None = None) -> str:
    """Create the dispatch row, lease and launch job. Caller holds the tx.

    Routing for executors happens before the transaction; planner and fix
    rounds route here from pinned state because they have no orchestrator turn.
    """
    task = state.get_task(con, run["id"], task_id)
    if role in ("executor", "planner"):
        from office import gates
        live = gates.live_task_session(con, run["id"], task_id, exclude=replaces)
        if live:
            # One session per worktree: amend, rerun and relaunch all come through here.
            raise Refused("planner-live" if role == "planner" else "worker-live",
                          f"{task_id} still has a live {role} ({live})", scope=task_id,
                          next_step=f'office prompt {task_id} -- "<message>" to reach it, or office revoke {task_id} '
                                    "to end it first")
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
    executor = role == "executor"
    # Evidence links: the executor this one replaces (same run and task only), and the task's
    # first executor, recorded once and left NULL on a task whose earlier executors predate it.
    predecessor = prior["id"] if (executor and prior and prior["run_id"] == run["id"] and prior["task_id"] == task_id
                                  and prior["role"] == "executor") else None
    first = executor and not con.execute(
        "SELECT 1 FROM dispatches WHERE run_id=? AND task_id=? AND role='executor'", (run["id"], task_id)).fetchone()
    worktree = prior["worktree"] if prior and prior.get("worktree") else str(paths.worktrees_dir() / run["id"][:8] / task_id)
    branch = prior["branch"] if prior and prior.get("branch") else f"office/{run['id'][:8]}/{task_id}"
    applied = task["contract_version"] if role != "planner" else run["plan_version"]
    con.execute(
        "INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, selection_reason, task_shape, "
        "started_at, task_id, kind, office_version, status, worktree, branch, base_commit, lease_id, harness, model, effort, "
        "adapter_id, applied_plan_version, route_json, size_class, predecessor_dispatch_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dispatch_id, run["id"], role, dispatch_id, routing.candidate_id(cand), cand.get("invocation_model_id"),
         (decision.get("selection_disclosure") or {}).get("reason"), run.get("playbook"), now_iso(), task_id, role,
         version.current(), "launching", worktree, branch, base or (prior or {}).get("base_commit") or run["base_sha"],
         lease["id"], cand["harness"], cand.get("invocation_model_id"), cand.get("effort"), cand.get("adapter_id"),
         applied, dumps(_route_payload(decision)),
         (task.get("descriptor") or {}).get("task_size") if executor else None, predecessor))
    if task.get("descriptor"):
        con.execute("UPDATE dispatches SET descriptor_json=? WHERE id=?",
                    (dumps(task["descriptor"]), dispatch_id))
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
                      pause_reason=None, stack_after=None,
                      **({"first_executor_dispatch_id": dispatch_id} if first else {}))
    payload = {"dispatch_id": dispatch_id, "task_id": task_id, "role": role, "fix_of": fix_of,
               **(decision.get("launch") or {}), **(extra or {})}
    state.enqueue(con, run, "launch_agent", payload, dedup_key=f"launch:{dispatch_id}", max_attempts=2)
    return dispatch_id


def lease_dormant(con, run_id: str, row) -> bool:
    """A task lease that holds no scope: its task has no live or launching session (the
    worker submitted and ended, an ack-only session ended, an accepted task was reopened
    and its session is gone). Nothing edits the task's files, so an overlapping task may
    run (#398, #307). The lease row stays: a relaunch of the task supersedes it, and that
    relaunch is refused while the overlapping task's session is live. A planner lease, a
    lease with no task, and a task Office still shows running or launching keep holding."""
    if row["role"] == "planner" or not row["task_id"]:
        return False
    task = state.get_task(con, run_id, row["task_id"])
    if task is None or task["role"] == "planner" or task["status"] in ("running", "launching"):
        return False
    from office import gates
    return gates.live_task_session(con, run_id, row["task_id"]) is None


def scope_holder(con, run: dict, task: dict) -> str | None:
    """The task whose live session holds a scope overlapping `task`'s, or None. Read-only."""
    for row in con.execute("SELECT * FROM leases WHERE run_id=? AND released_at IS NULL AND revoked_at IS NULL "
                           "AND task_id IS NOT ?", (run["id"], task["id"])).fetchall():
        other = state.get_task(con, run["id"], row["task_id"]) if row["task_id"] else None
        other_scope = other["scope"] if other else json.loads(row["scope"] or "[]")
        if planfile.scopes_overlap(task["scope"], other_scope) and not lease_dormant(con, run["id"], row):
            return row["task_id"] or row["holder_id"]
    return None


def acquire_lease(con, run: dict, task: dict, holder: str, role: str) -> dict:
    """One fenced lease per task scope. Overlapping live scopes are refused; a dormant
    lease (no live session of its task) does not hold its scope."""
    now = datetime.now(timezone.utc)
    other = scope_holder(con, run, task)
    if other:
        raise Refused("scope-held", f"{task['id']} scope overlaps {other}, which holds a live lease",
                      scope=task["id"], preserved="both tasks' work",
                      next_step=f"wait for {other}'s session to end (submit, accept, or office revoke {other}), "
                                f"or office dispatch {other} {task['id']} (stacked)")
    for row in con.execute("SELECT id FROM leases WHERE run_id=? AND task_id=? AND released_at IS NULL "
                           "AND revoked_at IS NULL", (run["id"], task["id"])).fetchall():
        # Handing the task's lease to a new holder (fix round, relaunch):
        # revoke the old one so a late submit from it is fenced out.
        con.execute("UPDATE leases SET revoked_at=?, revoke_reason='superseded' WHERE id=?", (now.isoformat(), row["id"]))
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
    """Extend a lease. Submit calls this in the transaction that captures a revision, so it is also where a
    trial dispatch's first revision settles its trial as `submitted`."""
    now = datetime.now(timezone.utc)
    con.execute("UPDATE leases SET expires_at=?, renewed_at=? WHERE id=?",
                ((now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(), now.isoformat(), lease_id))
    holder = con.execute("SELECT dispatch_id FROM leases WHERE id=?", (lease_id,)).fetchone()
    if holder and holder[0] and con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (holder[0],)).fetchone():
        trial_submitted(con, holder[0])


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


def _revoke_standing(con, run_id: str, task: dict) -> tuple[str | None, str | None, list[str], list[str]]:
    """How a revoke leaves a task: (status to set or None to keep it, pause_reason, unapplied ordinary
    amendment ids, contract-class amendment ids that block keeping it accepted).

    A task whose current revision is its accepted revision stays accepted: revoking a worker that only held the
    lease (an amendment relaunch whose submit was refused, an ack-only session) does not undo accepted work.
    Revision identity decides, not status, because `amend._deliver` already demoted the task and a refused submit
    already blocked it. A `contract_version` bump from an ordinary amendment does not demote: its check and
    acceptance edits are enforced by the gate rerun the amendment triggered, and its delta is listed as unapplied.
    An unapplied amendment that is not ordinary (scope, interfaces, ownership) is changes_required naming it.
    A cancelled task keeps its status."""
    if task["status"] == "cancelled":
        return None, None, [], []
    rev_id = task.get("accepted_revision_id")
    rev = con.execute("SELECT applied_version FROM revisions WHERE id=?", (rev_id,)).fetchone() if rev_id else None
    if rev is None or rev_id != task.get("current_revision_id"):
        return "paused", None, [], []
    pending = con.execute("SELECT d.amendment_id, COALESCE(a.class, 'contract') AS class FROM deliveries d "
                          "LEFT JOIN amendments a ON a.run_id=d.run_id AND a.id=substr(d.run_id, 1, 8) || ':' || d.amendment_id "
                          "WHERE d.run_id=? AND d.task_id=? AND d.target_version>? ORDER BY d.target_version",
                          (run_id, task["id"], rev["applied_version"] or 0)).fetchall()
    contract = list(dict.fromkeys(r["amendment_id"] for r in pending if r["class"] != "ordinary"))
    ordinary = [i for i in dict.fromkeys(r["amendment_id"] for r in pending) if i not in contract]
    if contract:
        return "changes_required", f"amended by {', '.join(contract)}", ordinary, contract
    return "accepted", None, ordinary, []


def _revoke_task(con, run: dict, task_id: str, reason: str, only: str | None = None) -> Result:
    """Revoke the task's lease and stop its live dispatches; `only` (a revoked
    dispatch id) stops that one and leaves any other live session alone. An accepted task whose accepted
    revision is still current stays accepted and its lease is released instead (`_revoke_standing`)."""
    with db.transaction(con):
        task = state.get_task(con, run["id"], task_id)
        if task is None:
            raise Usage("unknown-task", f"no task {task_id}")
        status, pause_reason, unapplied, contract_ids = _revoke_standing(con, run["id"], task)
        kept = status == "accepted"
        now = now_iso()
        if kept:
            con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND task_id=? AND released_at IS NULL "
                        "AND revoked_at IS NULL", (now, run["id"], task_id))
        else:
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason=? WHERE run_id=? AND task_id=? "
                        "AND released_at IS NULL AND revoked_at IS NULL", (now, reason, run["id"], task_id))
        if status:
            state.update_task(con, run["id"], task_id, status=status,
                              pause_reason=pause_reason or (f"lease revoked: {reason}" if status == "paused" else None))
        state.emit(con, run, "lease.released" if kept else "lease.revoked",
                   f"{task_id} lease {'released' if kept else 'revoked'}", task_id=task_id)
    live = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE run_id=? AND task_id=? AND ended_at IS NULL "
                                         "AND status IN ('launching', 'running')", (run["id"], task_id)).fetchall()]
    notes: list[str] = []
    others = [d["id"] for d in live if only and d["id"] != only]
    targets = [d for d in live if not only or d["id"] == only]
    cancelled = [d["id"] for d in targets if _cancel_pending_launch(con, run, d, reason)]
    # Re-read: a launch may have recorded its launcher and pid since the snapshot.
    fresh = [state.get_dispatch(con, d["id"]) for d in targets if d["id"] not in cancelled]
    stopped = [d["id"] for d in fresh if stop_dispatch(run, d, notes=notes)]
    starting = [d["id"] for d in fresh if d["id"] not in stopped and d["status"] == "launching" and not d.get("launcher")]
    if kept:
        lines = [f"{task_id} stays accepted on {task['accepted_revision_id']} | lease released; later submits from its "
                 "holder are rejected"]
        if unapplied:
            lines.append(f"unapplied amendments: {', '.join(unapplied)}")
    else:
        lines = [f"{task_id} lease revoked | later submits from its holder are rejected"]
    if stopped:
        lines.append(f"stopped {', '.join(stopped)} (SIGTERM)")
    if cancelled:
        lines.append(f"cancelled {', '.join(cancelled)} before its agent started")
    if starting:
        lines.append(f"{', '.join(starting)} is starting its agent now; it is tracked once up: office revoke "
                     f"{task_id} again to stop it")
    if others:
        lines.append(f"left running: {', '.join(others)} (not the dispatch named; its lease is revoked too, so its "
                     f"submits are rejected; office revoke {task_id} ends it, and office rerun refuses until it ends)")
    lines += notes
    if kept:
        nxt = (f"no relaunch is needed to stay accepted; office rerun {task_id} --resume to apply {', '.join(unapplied)}"
               if unapplied else f"no relaunch is needed: {task_id} is accepted; office status")
    elif status == "changes_required":
        nxt = f"office rerun {task_id} --resume to apply {', '.join(contract_ids)}"
    elif status is None:
        nxt = "office status"
    else:
        nxt = f"office dispatch {task_id} to relaunch"
    return Result(lines=lines, next=nxt)


def _cancel_pending_launch(con, run: dict, d: dict, reason: str) -> bool:
    """End a dispatch whose launch job has not been picked up yet: fail the queued job
    and record the end in one transaction. A claimed job may already be starting the
    agent, and cancelling under it would leave an agent nothing tracks; it is left to
    finish its launch (it is then tracked, and a second revoke stops it)."""
    if d["status"] != "launching" or d.get("launcher") or d.get("pid"):
        return False
    key = (run["id"], f"launch:{d['id']}")
    with db.transaction(con):
        if not con.execute("SELECT 1 FROM outbox WHERE run_id=? AND kind='launch_agent' AND dedup_key=? "
                           "AND status='queued'", key).fetchone():
            return False
        # Guarded on the row as it is now, not the caller's snapshot: a launch that recorded
        # its launcher or pid meanwhile is running and is stopped, not cancelled.
        ended = con.execute("UPDATE dispatches SET status='cancelled', terminal_classification='revoked', ended_at=? "
                            "WHERE id=? AND ended_at IS NULL AND status='launching' AND launcher IS NULL AND pid IS NULL",
                            (now_iso(), d["id"])).rowcount
        if not ended:
            return False
        con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, max_attempts=attempts "
                    "WHERE run_id=? AND kind='launch_agent' AND dedup_key=? AND status='queued'",
                    (f"revoked: {reason}"[:200], now_iso(), *key))
        abandon_trial(con, d["id"], f"revoked before its agent started: {reason}"[:200])
        if ended:
            state.emit(con, run, "dispatch.ended", f"{d.get('task_id') or d['role']} {d['role']} ended: revoked before "
                       "its agent started", audience="runtime", task_id=d.get("task_id"), dispatch_id=d["id"],
                       payload={"exit_code": None, "signal": None, "classification": "revoked"})
    return bool(ended)


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
        if job["status"] == "queued" or jobs.claim_live(job):
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
        return _revoke_task(con, run, d["task_id"], reason, only=d["id"])
    notes: list[str] = []
    ended = _end_dispatch(con, run, d, "revoked", f"revoked: {reason}", notes=notes)
    line = f"{d['id']} {'ended' if ended else 'had already ended'} ({reason})"
    return Result(lines=[line, *notes], next="office status")


def _fence_integrate_jobs(con, run: dict, reason: str) -> list[str]:
    """Stop and cancel the integrate jobs that own the integration reviews, so
    ending a reviewer cannot make a live job start a fallback or retry."""
    with db.transaction(con):
        jobs_ = [dict(r) for r in con.execute("SELECT * FROM outbox WHERE run_id=? "
                                              "AND kind='integrate' AND status IN ('queued','claimed')",
                                              (run["id"],)).fetchall()]
        for j in jobs_:
            con.execute("UPDATE outbox SET status='failed', error=?, finished_at=?, claimed_pid=NULL, max_attempts=attempts "
                        "WHERE id=?", (f"revoked: {reason}"[:200], now_iso(), j["id"]))
    unstopped = []
    for j in jobs_:
        if j["status"] != "claimed" or not jobs.claim_live(j):
            continue
        if not claim_signalable(j["claimed_pid"], j["claimed_by"]):
            unstopped.append(f"{j['id']} (pid {j['claimed_pid']})")  # no start time: the pid may be reused
            continue
        _killpg(j["claimed_pid"])
        deadline = time.time() + 5
        while jobs.claim_live(j) and time.time() < deadline:
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


def _pane_group_wait() -> float:
    """How long a pane launch polls for the agent's foreground group (#507)."""
    try:
        return max(0.0, float(os.environ.get("OFFICE_PANE_GROUP_WAIT", "3.0")))
    except ValueError:
        return 3.0


def _record_pane_agent_group(run: dict, dispatch_id: str, pane: str) -> int | None:
    """Record the pane agent's process group and its leader's start time, as a
    headless launch does (`agent.pgid`, `agent.identity`), so a server it leaves
    behind can later be proven Office's (#507). The agent is the pane's
    foreground job, so it leads that group. Nothing is recorded when the group
    is unknown, is the shell's own, or its leader's start cannot be read."""
    deadline = time.time() + _pane_group_wait()
    while True:
        # Right after start the shell may still own the terminal, or a short-lived
        # wrapper may: poll briefly until another group holds the foreground.
        info = _herdr_json(["pane", "process-info", "--pane", pane]).get("process_info") or {}
        pgid = info.get("foreground_process_group_id")
        if (isinstance(pgid, int) and pgid != info.get("shell_pid")
                and process_start(pgid)) or time.time() >= deadline:
            break
        time.sleep(0.2)
    if not isinstance(pgid, int) or pgid <= 1 or pgid == info.get("shell_pid") \
            or pgid in (os.getpid(), os.getpgrp()) or not process_start(pgid):
        return None
    try:
        if os.getpgid(pgid) != pgid:
            return None  # not a group leader: not the agent's own job
    except OSError:
        return None
    path = _agent_pgid_file(run, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pgid))
    _record_identity(run["id"], dispatch_id, "agent", pgid)
    return pgid


def _identity_file(run_id: str, dispatch_id: str, which: str) -> Path:
    return paths.run_dir(run_id) / "dispatches" / dispatch_id / f"{which}.identity"


def _c_start(pid: int | None) -> str | None:
    """The process's start time as `ps` prints it in the C locale, whitespace-normalized, or None. Unlike
    `process_start` it reads the same in any locale, so an identity recorded under one locale still matches
    when it is checked under another."""
    if not pid or pid <= 0:
        return None
    try:
        out = subprocess.run([_system_tool("ps", "/bin/ps", "/usr/bin/ps"), "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10, env={**os.environ, "LC_ALL": "C", "LANG": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(out.split()) or None


def _record_identity(run_id: str, dispatch_id: str, which: str, pid: int) -> None:
    """Record which process instance `pid` is (`supervisor` or `agent`), so a
    later stop never signals another process that reused the pid."""
    path = _identity_file(run_id, dispatch_id, which)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"pid": pid, "start": process_start(pid), "c_start": _c_start(pid)})


def _verified(run: dict, dispatch_id: str, which: str, pid: int | None) -> bool:
    """Whether `pid` is provably the recorded `which` process of this dispatch.
    No record (an older Office) or another start time means no."""
    try:
        rec = json.loads(_identity_file(run["id"], dispatch_id, which).read_text())
    except (OSError, ValueError):
        return False
    return rec.get("pid") == pid and (_c_start(pid) == rec["c_start"] if rec.get("c_start") else process_is(pid, rec.get("start")))


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
    # A pane agent is stopped by snapshot plus close (reclaim_pane below); its
    # recorded group serves only leftover-port reclaim (#507).
    if pgid_file.is_file() and d.get("launcher") != "herdr":
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
        from office import integration
        head = paths.git(repo, "rev-parse", f"refs/heads/{dispatch['branch']}")
        if not integration.contains(run, head, dispatch["base_commit"]):
            # An existing branch is reused as it is: it must hold the base this dispatch recorded.
            raise Refused("base-mismatch", f"branch {dispatch['branch']} ({head[:12]}) does not contain the base "
                          f"{dispatch['base_commit'][:12]} dispatch {dispatch['id']} recorded", scope=dispatch.get("task_id"),
                          preserved="the branch and its commits",
                          next_step=f"merge {dispatch['base_commit'][:12]} into {dispatch['branch']}, or remove the branch "
                                    f"and office rerun {dispatch.get('task_id')} --fresh")
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
    # Gather contract requests when the launch job constructs the packet, not
    # when the first amendment queued it: later requests may arrive before the
    # planner starts and must be included in its first brief (#506).
    requests = con.execute("SELECT seq, delta FROM amendments WHERE run_id=? AND class='contract' "
                           "AND to_plan_version IS NULL ORDER BY seq", (run["id"],)).fetchall() if role == "planner" else []
    contract_request = ("\n".join(f"A{r['seq']}: {r['delta']}" for r in requests)
                        if requests else extra.get("contract_request"))
    contract_request_max_seq = max((r["seq"] for r in requests), default=None)
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
        "worktree": dispatch["worktree"],
        "route": dispatch["route"].get("selection_disclosure"),
        "branch": dispatch.get("branch"),
        "pr": _pr_packet(con, run, task, dispatch) if role == "executor" else None,
        "requirements": req["frozen"],
        "fix_of": extra.get("fix_of"),
        "contract_request": contract_request,
        "contract_request_max_seq": contract_request_max_seq,
        "restack": extra.get("restack"),
        "base_parents": extra.get("base_parents"),
    }
    return state.packet_envelope(run, f"{role}-dispatch", body)


def _pr_packet(con, run: dict, task: dict, dispatch: dict) -> dict | None:
    """What the executor needs to push and open its draft PR (3.2), or None."""
    from office import prs
    if not prs.enabled(run) or not prs.has_pr(task):
        return None
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch["id"]
    ddir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    ddir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    carried: list = []
    brief = briefs.worker_brief(con, run, packet, setup=setup, carried=carried)
    (ddir / "brief.md").write_text(brief, encoding="utf-8")
    # The deliveries this brief rendered: they are confirmed once its prompt lands.
    atomic_write_json(ddir / "brief-deliveries.json", carried)
    with db.transaction(con):
        con.execute("UPDATE dispatches SET packet_hash=?, packet_path=?, log_path=? WHERE id=?",
                    (packet["packet_hash"], str(ddir / "packet.json"), str(ddir / "output.log"), dispatch["id"]))
    current = state.get_dispatch(con, dispatch["id"])
    if current["status"] != "launching":
        return {"skipped": current["status"]}
    lease = con.execute("SELECT revoked_at FROM leases WHERE id=?", (current.get("lease_id"),)).fetchone() \
        if current.get("lease_id") else None
    if lease and lease["revoked_at"]:
        # Revoked while its worktree was being set up: this job owns the claim, so it ends the
        # dispatch itself instead of starting an agent whose submits are fenced (R3-4).
        with db.transaction(con):
            con.execute("UPDATE dispatches SET status='cancelled', terminal_classification='revoked', ended_at=? "
                        "WHERE id=? AND ended_at IS NULL AND status='launching'", (now_iso(), dispatch["id"]))
            state.emit(con, run, "dispatch.ended", f"{dispatch.get('task_id')} {role} ended: revoked before its agent "
                       "started", audience="runtime", task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"],
                       payload={"exit_code": None, "signal": None, "classification": "revoked"})
        return {"skipped": "revoked"}
    if role == "executor" and open_trial(con, dispatch["id"]):
        # What the launch leaves in the worktree, so a failed trial can be told from one that did work.
        snapshot = _worktree_snapshot(wt)
        if snapshot is not None:
            atomic_write_json(ddir / "worktree-baseline.json", snapshot)
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
        _WORKER_TAG: dispatch["id"],
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
    write_launch_spec(run, dispatch["id"], spec)
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
    env.pop(_WORKER_TAG, None)  # only the agent carries a dispatch's id (worker_env): it names the processes to stop
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
    # The brief tells the agent to source agent.env whichever way it starts (the Herdr path rewrites it identically).
    write_agent_env(run, dispatch, ddir, worker=kind == "worker")
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
        no_pane = "no herdr pane could be opened"
        try:
            pane = _herdr_pane(run, cwd, label=label, dispatch_id=dispatch["id"]) if inter else None
        except ValueError as exc:
            pane, no_pane = None, f"{no_pane}: {exc}"
        if inter and not pane:
            spec["fallback_reason"] = no_pane
            write_launch_spec(run, dispatch["id"], spec)
            _launch_notice(run, dispatch, f"{no_pane}; running headless instead")
            if resume:
                _headless_resume(run, dispatch, kind, spec, resume)
        if pane:
            started = _herdr_agent_start(run, dispatch, spec, env, inter, pane, cwd, ddir, label=label)
            if started:
                if wait:
                    return _wait_terminal(dispatch["id"])
                return started
            if cli:
                # Running the adapter's own argv headless would not be what the user asked for.
                _launch_notice(run, dispatch, f"--cli agent did not start in herdr; left external. Start it by hand: {cli}")
                return _launch_external(run, dispatch, kind, spec, ddir, sup, env, cwd, wait=wait, announce=False)
            # The agent never came up in the pane: fall back to a plain process,
            # and keep that visible on the dispatch (its notice says why).
            headless = "process-fallback"
            if resume:
                _headless_resume(run, dispatch, kind, spec, resume)
    log = open(ddir / "supervisor.log", "ab")
    try:
        proc = subprocess.Popen(sup, cwd=str(paths.run_dir(run["id"])), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    finally:
        log.close()
    _record_launch(run, dispatch["id"], launcher=headless, pid=proc.pid)
    if wait:
        proc.wait()
        return _wait_terminal(dispatch["id"])
    return {"launcher": headless, "pid": proc.pid}


def _headless_resume(run: dict, dispatch: dict, kind: str, spec: dict, resume: dict) -> None:
    """A `rerun --resume` dispatch whose pane never came up. Resume the recorded
    session headless with the adapter's headless resume args when it declares
    them, else say it started fresh, and drop the parent's session id the
    dispatch inherited so the new session is recorded without a session.mismatch (#445)."""
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or dispatch.get("harness") or "")
    session = resume.get("session_id")
    form = adapters.headless_resume_args(adapter, kind, session) if adapter and session else None
    if form:
        spec["headless_resume"] = form
        _launch_notice(run, dispatch, f"resumed headless: session {session} continues with the harness's resume form "
                                      "(no pane)")
    else:
        spec["headless_fresh"] = True
        _set_dispatch(dispatch["id"], session_id=None)
        dispatch["session_id"] = None
        _assign_session(dispatch)
        why = "no session id was recorded" if not session else "its adapter declares no headless resume form"
        _launch_notice(run, dispatch, f"the resume could not run in a pane and {why}: started a FRESH session, not "
                                      "a continuation of the parent's; the brief and the preserved worktree carry the task")
    write_launch_spec(run, dispatch["id"], spec)


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
    write_launch_spec(run, dispatch["id"], spec)
    log = open(ddir / "supervisor.log", "ab")
    try:
        watcher = subprocess.Popen(sup, cwd=str(paths.run_dir(run["id"])), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
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


# CSI sequences, OSC sequences (an OSC 8 hyperlink), and two-byte escapes.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _strip_ansi(text: str | None) -> str:
    return _CSI.sub("", text or "")


def _startup_screen(text: str | None) -> str | None:
    low = _strip_ansi(text).lower()
    return next((label for marker, label in _STARTUP_SCREEN_MARKERS if marker in low), None)


def _herdr_fallback_notice(run: dict, dispatch: dict, spec: dict, ddir: Path, pane: str,
                           failure: str, why: str) -> None:
    """Preserve failed-pane evidence and hand recovery back to the orchestrator.

    The automatic workaround remains the existing headless fallback. The
    pane's last screen is saved to pane-tail.txt (#399); the pane itself, now
    abandoned, is closed when Office can prove it opened it for this dispatch
    and no other dispatch holds it.
    """
    # No agent is registered, so read the pane itself. Only the last screen
    # counts: older scrollback may mention any of the markers.
    view = "\n".join(_pane_read(pane).splitlines()[-40:]) or None
    tail = ddir / "pane-tail.txt"
    screen = _startup_screen(view)
    if view:
        atomic_write_text(tail, view + "\n", mode=0o600)  # an unredacted screen: private (#500)
    spec["failed_herdr_pane"] = pane
    spec["failed_herdr_snapshot"] = str(tail) if view else None
    spec["failed_herdr_screen"] = screen
    closed = bool(view) and _close_abandoned_pane(run, dispatch, pane)
    spec["failed_herdr_pane_closed"] = closed
    write_launch_spec(run, dispatch["id"], spec)
    observed = (f"; pane {pane} was waiting on {screen}" if screen
                else f"; pane {pane} snapshot saved to {tail}" if view
                else f"; pane {pane} could not be read")
    if closed:
        observed += f"; the abandoned pane is closed (its last screen is in {tail})"
    inspect = f"read {tail}" if closed else f"inspect it with `herdr pane read {pane}`"
    recovery = (f"orchestrator recovery: {inspect}; do not auto-approve trust, "
                "credentials, or user-authority prompts; resolve a safe runtime blocker or choose another route, "
                "then follow `office status` / `office resume` instead of abandoning the run")
    spec["fallback_reason"] = f"{failure} ({why}){observed}"
    write_launch_spec(run, dispatch["id"], spec)
    _launch_notice(run, dispatch, f"{failure} ({why}){observed}; running headless instead. {recovery}")


def _close_abandoned_pane(run: dict, dispatch: dict, pane: str) -> bool:
    """Close the pane a failed Herdr start left behind once its dispatch runs
    headless. Never the caller's pane or the layout anchor, never a pane another
    open dispatch records or has reserved. False when it was kept."""
    if not pane or pane in (os.environ.get("HERDR_PANE_ID"), os.environ.get("OFFICE_HERDR_ANCHOR")):
        return False
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    try:
        layout = json.loads(tab_file.read_text()) if tab_file.is_file() else {}
    except (OSError, ValueError):
        layout = {}
    if pane == layout.get("anchor"):
        return False
    con = db.connect()
    try:
        held = con.execute("SELECT 1 FROM dispatches WHERE pane_id=? AND id<>? AND ended_at IS NULL "
                           "AND status IN ('launching','running')", (pane, dispatch["id"])).fetchone()
        open_ids = {r["id"] for r in con.execute("SELECT id FROM dispatches WHERE run_id=? AND id<>? AND ended_at IS NULL "
                                                 "AND status IN ('launching','running')",
                                                 (run["id"], dispatch["id"])).fetchall()}
    finally:
        con.close()
    try:
        reserved = _reserved_panes(run, open_ids)
    except ValueError:
        return False  # an unreadable ledger cannot show the pane is free to close
    if held or pane in reserved:
        return False
    _herdr_quiet("pane", "close", pane)
    if _pane_exists(pane):
        return False
    with _pane_lock(run):
        # A closed pane left in the layout would be split from later and fail (review F1).
        try:
            layout = json.loads(tab_file.read_text()) if tab_file.is_file() else None
        except (OSError, ValueError):
            layout = None
        if layout and pane in (layout.get("panes") or []):
            layout["panes"] = [p for p in layout["panes"] if p != pane]
            atomic_write_json(tab_file, layout)
        _unreserve_pane(run, pane)
    return True


def _identity_line(value: object) -> str:
    """Printable and bounded evidence from a pane or a reservation."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())[:200]


def _read_reservations(run: dict) -> dict[str, str]:
    """Pane -> holding dispatch from the run's reservation ledger.

    Runs created before reservation support have no ledger: that is empty. A
    ledger that exists but cannot be read, or is malformed, never authorizes a
    pane, so every reader and writer refuses on the ValueError.
    """
    path = paths.run_dir(run["id"]) / "herdr-reservations.json"
    if not path.exists():
        return {}
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("pane reservation evidence is unreadable") from exc
    if not isinstance(held, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
                                          for k, v in held.items()):
        raise ValueError("pane reservation evidence is malformed")
    return held


def _reserved_by(run: dict, pane: str) -> str | None:
    return _read_reservations(run).get(pane)


def reported_identity(info: dict) -> dict:
    """The cwd, agent and session Herdr reports in a `pane get` record (None when it reports none)."""
    running = info.get("agent")
    nested = running if isinstance(running, dict) else {}
    return {"cwd": info.get("foreground_cwd") or info.get("cwd"),
            "agent": nested.get("name") if nested else running,
            "session": (nested.get("session_id") or nested.get("agent_session_id")) or info.get("session_id")}


def _pane_identity_mismatch(run: dict, dispatch: dict, pane: str, cwd: Path, *,
                            check_cwd: bool = True, agent: str | None = None) -> str | None:
    """Refuse a *reported* contradictory Herdr identity before crossing into a pane.

    Herdr versions do not all report cwd/session fields; the dispatch-specific
    `_shell_run` marker separately confirms its setup ran. We never treat a
    reported contradictory value as missing evidence or a successful prompt.
    """
    try:
        holder = _reserved_by(run, pane)
    except ValueError as exc:
        return f"pane {pane} cannot be trusted: {exc}"
    if holder and holder != dispatch["id"]:
        return f"pane {_identity_line(pane)} belongs to dispatch {_identity_line(holder)}, not {dispatch['id']} ({dispatch.get('task_id')})"
    info = _herdr_json(["pane", "get", pane]).get("pane") or {}
    actual = info.get("foreground_cwd") or info.get("cwd")
    if check_cwd and actual:
        expected = os.path.realpath(str(cwd))
        observed = os.path.realpath(str(actual))
        if observed != expected and not observed.startswith(expected.rstrip(os.sep) + os.sep):
            # A pane can report another task's path, never echo terminal control
            # sequences from an untrusted shell path into the operator output.
            return (f"pane {_identity_line(pane)} cwd {_identity_line(actual)} does not belong to {dispatch['id']} "
                    f"({dispatch.get('task_id')}); expected {_identity_line(cwd)}")
    running = info.get("agent")
    reported_name = running.get("name") if isinstance(running, dict) else running
    if isinstance(running, dict) and not reported_name:
        return f"pane {_identity_line(pane)} has an agent without a verifiable identity"
    if agent is None and reported_name:
        return f"pane {pane} already runs an agent; shell setup is unsafe for {dispatch['id']}"
    if agent and reported_name and reported_name != agent:
        return (f"pane {_identity_line(pane)} runs {_identity_line(reported_name)}, "
                f"not {_identity_line(agent)} for dispatch {dispatch['id']}")
    reported_session = ((running.get("session_id") or running.get("agent_session_id"))
                        if isinstance(running, dict) else None) or info.get("session_id")
    recorded_session = dispatch.get("session_id")
    if agent and reported_session and recorded_session and reported_session != recorded_session:
        return f"pane {pane} session does not match dispatch {dispatch['id']}"
    return None


class PaneMismatch(Exception):
    """Herdr reports a pane identity that contradicts the dispatch about to be sent to."""


@contextlib.contextmanager
def fenced_pane(run: dict, dispatch: dict):
    """Yield the dispatch's recorded pane with the run's pane lock held, once its
    reported cwd, agent and session are shown to be this dispatch's own; else
    raise PaneMismatch. Send a prompt into a live agent's pane only inside this
    block, so the pane cannot be reassigned between the check and the send."""
    pane = dispatch["pane_id"]
    worktree = dispatch.get("worktree")
    with _pane_lock(run):
        mismatch = _pane_identity_mismatch(run, dispatch, pane, Path(worktree or ""), check_cwd=bool(worktree),
                                           agent=herdr_agent_name(dispatch["id"]))
        if mismatch:
            raise PaneMismatch(mismatch)
        yield pane


def _herdr_agent_start(run: dict, dispatch: dict, spec: dict, env: dict, inter: tuple[list[str], str], pane: str,
                       cwd: Path, ddir: Path, *, retried: bool = False, label: str | None = None) -> dict | None:
    """Start the real harness in the pane with `herdr agent start`, hand it a
    one-line brief pointer, and leave a detached watcher to record the end.
    The pane runs the agent itself, never a shell wrapper around it."""
    args, herdr_kind = inter
    name = herdr_agent_name(dispatch["id"])
    worker = spec["kind"] == "worker"
    mismatch = _pane_identity_mismatch(run, dispatch, pane, cwd, check_cwd=False)
    if mismatch:
        spec["fallback_reason"] = f"pane-mismatch: {mismatch}"
        spec["prompt_landed"] = False
        write_launch_spec(run, dispatch["id"], spec)
        _launch_notice(run, dispatch, f"pane-mismatch: {mismatch}; no shell command or brief sent")
        return None
    # The pane's shell does not inherit this process's environment: source the
    # dispatch identity into it first, so the agent's own `office submit` works.
    env_file = write_agent_env(run, dispatch, ddir, worker=worker)
    setup = f". {shlex.quote(str(env_file))} && cd {shlex.quote(str(cwd))}"
    if not _shell_run(pane, setup, ddir / "shell-ready"):
        _herdr_fallback_notice(run, dispatch, spec, ddir, pane, f"the shell in pane {pane} never ran Office's setup "
                               "line (env and cd)", f"within {_shell_timeout():g}s")
        return None
    mismatch = _pane_identity_mismatch(run, dispatch, pane, cwd)
    if mismatch:
        spec["fallback_reason"] = f"pane-mismatch: {mismatch}"
        spec["prompt_landed"] = False
        write_launch_spec(run, dispatch["id"], spec)
        _launch_notice(run, dispatch, f"pane-mismatch: {mismatch}; agent and brief were not started")
        return None
    _record_launch_form(run, dispatch, spec, "herdr",
                        ["herdr", "agent", "start", name, "--kind", herdr_kind, "--pane", pane, "--", *args],
                        transport="herdr agent prompt pointer", herdr_kind=herdr_kind,
                        brief_delivery="herdr agent prompt (brief pointer)")
    try:
        proc = subprocess.run(["herdr", "agent", "start", name, "--kind", herdr_kind, "--pane", pane, "--", *args],
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(exc.cmd, 1, "", f"timeout: {exc}")
    except (OSError, subprocess.SubprocessError) as exc:
        _herdr_fallback_notice(run, dispatch, spec, ddir, pane, "herdr agent start failed", str(exc))
        return None
    if proc.returncode != 0 and _office_owned(run, cwd) and "agent_not_ready" in (proc.stdout or "") + (proc.stderr or "") \
            and _answer_startup_trust(pane, name, _land_timeout()):
        # The harness opened Office's own worktree on its folder-trust dialog and Herdr
        # stopped waiting; the dialog is answered and the agent is up (330605a8).
        proc = subprocess.CompletedProcess(proc.args, 0, "", "")
    if proc.returncode != 0 and re.search(r"agent_not_ready|\btimeout\b", (proc.stdout or "") + (proc.stderr or "")):
        # Held on an interactive startup screen: keep the pane and let the
        # orchestrator answer it before falling back to headless (#510).
        from office import startup
        held = startup.hold(run, dispatch, spec, pane, name, herdr_kind, ddir)
        write_launch_spec(run, dispatch["id"], spec)
        if held == "ready":
            proc = subprocess.CompletedProcess(proc.args, 0, "", "")
        elif held == "cancelled":
            _close_abandoned_pane(run, dispatch, pane)
            return {"launcher": "herdr", "agent": name, "pane": pane, "cancelled": True}
        elif held == "fallback":
            sp = spec["startup_prompt"]
            _herdr_fallback_notice(run, dispatch, spec, ddir, pane, f"startup prompt {sp['id']} ({sp['screen']}) "
                                   f"held herdr agent {name}", sp.get("resolution") or "not answered")
            return None
    if proc.returncode != 0:
        why = (proc.stdout or proc.stderr or "").strip()[:200]
        if "agent_pane_busy" in why and not retried:
            # The pane still holds an agent (a finished session herdr keeps):
            # split a fresh one and try once more before going headless (#200 B7).
            try:
                fresh = _herdr_fresh_pane(run, cwd, pane, dispatch_id=dispatch["id"])
            except ValueError:
                fresh = None  # the ledger cannot vouch for a new pane: fall back below
            if fresh:
                return _herdr_agent_start(run, dispatch, spec, env, inter, fresh, cwd, ddir, retried=True, label=label)
        _herdr_fallback_notice(run, dispatch, spec, ddir, pane, "herdr agent start failed", why)
        return None
    spec.update({"herdr_agent": name, "pane": pane})
    write_launch_spec(run, dispatch["id"], spec)
    _record_launch(run, dispatch["id"], launcher="herdr", pane_id=pane)
    _record_pane_agent_group(run, dispatch["id"], pane)
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
            session = _record_session(run, dispatch, session, source="herdr")
    if not session and not dispatch.get("resumed_from"):
        # Herdr often reports none for codex (#406), while codex prints it in
        # its startup banner: read it there, before any prompt is sent.
        found = _banner_session(pane, adapters.load_all().get(dispatch.get("adapter_id") or ""), ddir / "shell-ready")
        if found:
            session = _record_session(run, dispatch, found, source="output")
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

    # The same lock guards both pane assignment and delivery. Without it an
    # ended dispatch's pane could be reassigned between inspection and send.
    # The reservation is still checked inside this critical section.
    with _pane_lock(run):
        mismatch = _pane_identity_mismatch(run, dispatch, pane, cwd, agent=name)
        reported = reported_identity(_herdr_json(["pane", "get", pane]).get("pane") or {})
        spec["pane_evidence"] = {"pane": pane, "dispatch": dispatch["id"],
                                "task": dispatch.get("task_id"), "expected_worktree": str(cwd),
                                "reported_cwd": reported["cwd"], "reported_agent": reported["agent"],
                                "reported_session": reported["session"], "mismatch": mismatch}
        if mismatch:
            # Once an agent starts, never launch a racing headless copy.
            landed = False
            spec["identity_failure"] = mismatch
            _launch_notice(run, dispatch, f"pane-mismatch: {mismatch}; brief NOT sent; "
                           f"inspect pane {pane} and use office revoke {dispatch.get('task_id') or dispatch['id']}")
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
            title = ("folder-trust dialog" if screen == TRUST_SCREEN else screen if screen == EXTERNAL_IMPORTS_SCREEN
                     else f"{screen} screen")
            why = (f" the {title} holds the composer; review or skip it in the pane and"
                   if screen else "")
            _launch_notice(run, dispatch, f"brief pointer did not land in herdr agent {name} (pane {pane});{why} "
                                          f"re-prompt it: office prompt {dispatch['id']} -- {shlex.quote(pointer)}")
    spec["prompt_landed"] = landed
    write_launch_spec(run, dispatch["id"], spec)
    if not session and landed and not dispatch.get("resumed_from"):
        # The session's own transcript now records the prompt naming this brief.
        session = _transcript_session(run, dispatch, spec, herdr_kind, cwd, since=sent_at)
    if not session:
        # Not a launch failure: the agent runs. Recorded where `office inspect` shows it.
        con = db.connect()
        try:
            with db.transaction(con):
                state.emit(con, run, "launch.session", f"{dispatch.get('task_id') or dispatch['id']}: no session id "
                           f"was reported by herdr, the agent's startup banner or its transcript for {name}; "
                           "office rerun --resume will refuse it", audience="runtime",
                           task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"])
        finally:
            con.close()
    log = open(ddir / "supervisor.log", "ab")
    try:
        watcher = subprocess.Popen(frontdoor.current_argv()[0] + ["_supervise", dispatch["id"]],
                                   cwd=str(paths.run_dir(run["id"])), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
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
TRUST_DIALOG_MARKERS = ("trust this folder", "quick safety check", "is this a project you created or one you trust")
# The ctx figure in a Claude status line ("ctx: 12k · $0.03"): it rises from
# 0k once a prompt reaches the model, which is a landed signal in a pane too
# narrow to show the busy footer.
_CTX_RE = re.compile(r"\bctx:\s*(\d+(?:\.\d+)?)\s*k\b", re.I)


def _trust_dialog(text: str | None) -> bool:
    low = _strip_ansi(text).lower()
    return any(m in low for m in TRUST_DIALOG_MARKERS)


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
                if not _confirm_trust(pane, lambda: _pane_view(name), herdr, text):
                    return "trust"  # no trust option could be selected
                answered = True
        elif _agent_up(pane, name):
            return "ready"
        if time.time() >= deadline:
            return "down"
        time.sleep(1)


# The selected option of a startup dialog: Claude "❯ Yes, I trust this folder",
# codex "> 1. Trust and continue".
_CURSOR = re.compile(r"^\s*[❯›>▸▶]\s*(?:\d+[.)]\s*)?(\S.*?)\s*$")
_DECLINE = re.compile(r"\b(?:no|exit|quit|cancel|skip|without|don'?t|do not)\b", re.I)


def _selected_option(text: str | None) -> str | None:
    lines = _strip_ansi(text).splitlines()[-40:]
    picked = [m.group(1) for ln in lines if (m := _CURSOR.match(ln))]
    return picked[-1] if picked else None


def _trust_selected(option: str | None) -> bool:
    low = (option or "").strip().lower()
    # The trust option itself, or a plain "Yes, proceed/continue"; never any other "Yes, ..."
    # a startup screen offers (an MCP server, terms).
    ok = "trust" in low or re.match(r"yes,?\s+(?:proceed|continue)\b", low)
    return bool(low) and bool(ok) and not _DECLINE.search(low)


def _confirm_trust(pane: str, read, herdr, text: str | None = None) -> bool:
    """Move a folder-trust dialog's selection onto its trust option and confirm it.
    Current Claude Code preselects "No, exit", so a bare Enter would quit the
    agent; Enter is pressed only once the selected option is the trust one.
    `text` is a screen the caller already read."""
    if text is None or _selected_option(text) is None or not _trust_dialog(text):
        # The first frame may be blank, unreadable or half-drawn too (R3-11).
        text = _settled(read, None)
    for key in ("down", "down", "up", "up", "up", None):
        text = read() if text is None else text
        if not _trust_dialog(text):
            return False
        option = _selected_option(text)
        if _trust_selected(option):
            # A frame read right after a key can predate the redraw: confirm only when a
            # fresh, complete frame still shows the trust option selected.
            again = _settled(read, None)
            if _trust_dialog(again) and _trust_selected(_selected_option(again)):
                herdr("pane", "send-keys", pane, "Enter")
                return True
            text = again
            continue
        if key is None or option is None:
            return False
        herdr("pane", "send-keys", pane, key)
        text = _settled(read, option)
    return False


def _settled(read, before: str | None) -> str:
    """The first complete frame (the dialog with a selection) whose selection differs from
    `before`; a blank, half-drawn or unreadable frame is read again. After the settle time
    (OFFICE_HERDR_KEY_SETTLE, default twice OFFICE_HERDR_KEY_DELAY) the last frame read is
    returned (the list end: the key moved nothing)."""
    delay = float(os.environ.get("OFFICE_HERDR_KEY_DELAY", "1"))
    deadline = time.time() + float(os.environ.get("OFFICE_HERDR_KEY_SETTLE", 2 * delay))
    last = ""
    while True:
        text = read()
        option = _selected_option(text) if _trust_dialog(text) else None
        if option is not None:
            last = text
            if option != before:
                return text
        if time.time() >= deadline:
            return last or text
        time.sleep(0.2)


def _answer_startup_trust(pane: str, name: str, timeout: float) -> bool:
    """`herdr agent start` gave up on an agent held by a folder-trust dialog
    (`agent_not_ready`): answer it, then wait until Herdr reports the agent
    ready. True when the agent is up past the dialog."""
    read = lambda: _pane_visible(pane)  # noqa: E731  the screen now, not scrollback (review F4)
    if not _confirm_trust(pane, read, _herdr_quiet):
        return False
    deadline = time.time() + timeout
    while True:
        res = _herdr_json(["agent", "get", name])
        agent = res.get("agent") or res  # flat or nested, as _agent_up reads it (review F5)
        status = (agent.get("status") or agent.get("agent_status")) if isinstance(agent, dict) else None
        if status in ("idle", "working", "done") and not _trust_dialog(read()):
            return True
        if time.time() >= deadline:
            return False
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
        if not answer_trust or not _confirm_trust(pane, lambda: _pane_view(name), herdr, _pane_view(name)):
            return False
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


def headless_fallbacks(con, run: dict) -> list[str]:
    """One line per live dispatch that was meant for a Herdr pane and runs headless
    instead, with why (launch.json `fallback_reason`). `office status` and `wait` show it."""
    out = []
    for d in con.execute("SELECT id, task_id, role FROM dispatches WHERE run_id=? AND ended_at IS NULL "
                         "AND status IN ('launching','running') AND launcher IN ('process','process-fallback') "
                         "ORDER BY started_at", (run["id"],)).fetchall():
        try:
            spec = json.loads((paths.run_dir(run["id"]) / "dispatches" / d["id"] / "launch.json").read_text())
        except (OSError, ValueError):
            continue
        reason = spec.get("fallback_reason") if isinstance(spec, dict) else None
        if reason:
            out.append(f"{d['task_id'] or d['role']} {d['id']} runs headless (herdr fallback): {reason[:300]}")
    return out


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
    """Pick (or open) a pane under the run's pane lock and reserve it for
    `dispatch_id`. Launch jobs run in parallel, and a dispatch records its pane
    only once its agent has started: without the lock and the reservation two
    launches took the same idle pane, and one's setup line never ran because the
    other's agent already held the pane (run f00446ac).

    Raises ValueError when the reservation ledger is unreadable, before any pane
    is picked or split."""
    with _pane_lock(run):
        _read_reservations(run)
        pane = _herdr_pick_pane(run, cwd)
        if pane and dispatch_id:
            _reserve_pane(run, pane, dispatch_id)
    if pane and label:
        _herdr_rename(pane, label)  # cosmetic: outside the lock (review F9)
    return pane


@contextlib.contextmanager
def _pane_lock(run: dict):
    """Serializes pane picks, splits and layout edits for one run."""
    run_dir = paths.run_dir(run["id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "herdr-tab.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _reserve_pane(run: dict, pane: str, dispatch_id: str) -> None:
    """Reserve `pane` for `dispatch_id`. Caller holds the pane lock. An unreadable
    ledger raises ValueError rather than being rewritten from empty, which would
    drop other dispatches' reservations."""
    held = _read_reservations(run)
    held[pane] = dispatch_id
    atomic_write_json(paths.run_dir(run["id"]) / "herdr-reservations.json", held)


def _unreserve_pane(run: dict, pane: str) -> None:
    """Drop a closed pane's reservation. Caller holds the pane lock."""
    try:
        held = _read_reservations(run)
    except ValueError:
        return  # nothing is released from a ledger that cannot be trusted
    if pane in held:
        held.pop(pane)
        atomic_write_json(paths.run_dir(run["id"]) / "herdr-reservations.json", held)


def _tab_exists(tab_id: str) -> bool:
    """False only when herdr says the tab is gone; an unreachable herdr counts as present."""
    try:
        proc = subprocess.run(["herdr", "tab", "get", tab_id], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return proc.returncode == 0 or "not_found" not in (proc.stdout or "") + (proc.stderr or "")


def _live_panes(panes: list) -> list:
    """The recorded panes herdr has not reported gone (an unreachable herdr keeps them)."""
    return [p for p in panes if _pane_exists(p)]


def _reserved_panes(run: dict, open_ids: set) -> set:
    """Panes reserved for a dispatch that has not ended. Raises ValueError on an
    unreadable ledger: an empty answer would offer every pane as free."""
    return {pane for pane, did in _read_reservations(run).items() if did in open_ids}


def _herdr_pick_pane(run: dict, cwd: Path) -> str | None:
    """A visible Herdr pane for a dispatch, split beside the caller's own pane.

    The first dispatch splits the orchestrator's pane (`HERDR_PANE_ID`, or
    `OFFICE_HERDR_ANCHOR` when set) to the right, so the agent appears in the
    tab the user is watching. Later dispatches reuse a pane whose dispatch has
    ended, else stack down in that column. The caller's pane is only ever split,
    never run in or closed. Without a caller pane, the run gets its own tab."""
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    layout = json.loads(tab_file.read_text()) if tab_file.is_file() else None
    anchor = os.environ.get("OFFICE_HERDR_ANCHOR") or os.environ.get("HERDR_PANE_ID")
    if layout and layout.get("mode") != "split" or (layout is None and not anchor):
        pane = _herdr_own_tab_pane(run, cwd, tab_file, layout)
    else:
        pane = _herdr_split_pane(run, cwd, tab_file, layout, anchor)
    return pane


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
        busy = {r["pane_id"] for r in con.execute("SELECT pane_id FROM dispatches WHERE run_id=? AND launcher='herdr' "
                                                  "AND status IN ('launching','running')", (run["id"],)).fetchall()}
        open_ids = {r["id"] for r in con.execute("SELECT id FROM dispatches WHERE run_id=? AND ended_at IS NULL "
                                                 "AND status IN ('launching','running')", (run["id"],)).fetchall()}
    finally:
        con.close()
    busy |= _reserved_panes(run, open_ids)
    # A failed startup can leave the harness TUI alive in a pane even though
    # Herdr never registered an agent. Each dispatch records that pane in its
    # own launch.json; derive reservations from those per-dispatch records so
    # parallel failures cannot race on shared layout state (#399).
    for launch_file in (paths.run_dir(run["id"]) / "dispatches").glob("*/launch.json"):
        try:
            failed = json.loads(launch_file.read_text()).get("failed_herdr_pane")
        except (OSError, ValueError):
            failed = None
        if failed:
            busy.add(failed)
    return busy


def _pane_is_shell(pane: str) -> bool:
    """A pane Office may start an agent in: live, with no agent in it. An ended
    dispatch's pane can still hold its finished session, which herdr rejects
    (`agent_pane_busy`), so the dispatch row alone does not make it reusable."""
    info = _herdr_json(["pane", "get", pane]).get("pane") or {}
    return bool(info) and not info.get("agent")


def _herdr_split_pane(run: dict, cwd: Path, tab_file: Path, layout: dict | None, anchor: str | None) -> str | None:
    layout = layout or {"mode": "split", "anchor": anchor, "panes": []}
    anchor = layout.get("anchor") or anchor
    live = _live_panes(layout["panes"])  # the user may close panes
    busy = _busy_panes(run)
    for pane in live:
        if pane not in busy and _pane_is_shell(pane):
            # No `cd` here: the launch's setup line cds, so the lock holds no extra herdr call.
            layout["panes"] = live
            atomic_write_json(tab_file, layout)
            return pane
    if not live and anchor and not _pane_exists(anchor):  # unreachable herdr is not "gone"
        # The recorded anchor is gone (the orchestrator's pane was closed): split from the
        # caller's pane instead, else give the run its own tab. Splitting a gone pane failed
        # every later launch into headless.
        caller = os.environ.get("OFFICE_HERDR_ANCHOR") or os.environ.get("HERDR_PANE_ID")
        if caller and caller != anchor and _pane_exists(caller):
            anchor = layout["anchor"] = caller
        else:
            return _herdr_own_tab_pane(run, cwd, tab_file, None)
    # Split the caller's pane vertically (side by side); stack further agents in that column.
    target, direction = (live[-1], "down") if live else (anchor, "right")
    res = _herdr_json(["pane", "split", "--pane", target, "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    info = res.get("pane") or {}
    pane = info.get("pane_id")
    if not pane:
        return None
    layout["panes"] = live + [pane]
    layout.setdefault("tab_id", info.get("tab_id"))
    atomic_write_json(tab_file, layout)
    return pane


def _herdr_own_tab_pane(run: dict, cwd: Path, tab_file: Path, tab: dict | None) -> str | None:
    """No caller pane to split (or a run begun before split mode): a tab owned by the run."""
    orphans: list = list((tab or {}).get("orphan_tabs") or [])  # carried whenever the record is replaced
    if tab and not _tab_exists(tab["tab_id"]):
        tab = None  # the user closed it (an unreachable herdr keeps it)
    if tab is not None:
        # Panes closed since (by the user or as abandoned) are never split from (review F1).
        tab["panes"] = _live_panes(tab.get("panes") or [])
        if not tab["panes"]:
            # The tab outlived its recorded panes: open a new one, and keep the old id so
            # close_herdr_tab still closes it.
            orphans.append(tab["tab_id"])
            tab = None
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
        if orphans:
            tab["orphan_tabs"] = orphans
        atomic_write_json(tab_file, tab)
        return root
    busy = _busy_panes(run)
    for pane in tab["panes"]:
        if pane not in busy and _pane_is_shell(pane):
            return pane  # the launch's setup line cds
    direction = "right" if len(tab["panes"]) % 2 else "down"
    res = _herdr_json(["pane", "split", "--pane", tab["panes"][-1], "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    pane = (res.get("pane") or {}).get("pane_id")
    if pane:
        tab["panes"].append(pane)
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
                for tab_id in [*(tab.get("orphan_tabs") or []), tab["tab_id"]]:
                    subprocess.run(["herdr", "tab", "close", tab_id], capture_output=True, timeout=30)
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            pass


def _orchestrator_pane(run: dict) -> str | None:
    """The pane of the orchestrator that owns this dispatch: its HERDR_PANE_ID,
    else the anchor the run's split layout recorded (a relaunch from a detached
    job has no pane of its own)."""
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        return pane
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    try:
        layout = json.loads(tab_file.read_text()) if tab_file.is_file() else {}
    except (OSError, ValueError):
        layout = {}
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


# A harness's startup header block opens and closes with a rule line: dashes
# (`codex exec`'s stream) or a box edge (codex's interactive banner, whose
# lines sit between box sides). Session ids are read only inside one (#406).
_ANSI_SEQ = _CSI  # one stripper (OSC included)
_HEADER_RULE = re.compile(r"-{8,}|[╭┌][─━].*[─━][╮┐]|[╰└][─━].*[─━][╯┘]")


def _header_rule(line: str) -> bool:
    return bool(_HEADER_RULE.fullmatch(line))


def _header_text(line: str) -> str:
    """A header line without the box sides an interactive banner draws."""
    if len(line) >= 2 and line[0] in "│┃" and line[-1] in "│┃":
        return line[1:-1].strip()
    return line


def banner_session(text: str | None, pattern, after: str | None = None) -> str | None:
    """The session id a pane's startup screen shows inside a header block, by
    the adapter's `session.output_pattern`. With `after`, only the screen below
    the last occurrence of that text counts (this dispatch's own setup line, so
    a banner left in a reused pane by an earlier agent is never read). One id
    or none: two different ids on the screen are ambiguous, so neither is taken."""
    if not text or pattern is None:
        return None
    lines = [_ANSI_SEQ.sub("", raw).strip() for raw in text.splitlines()]
    if after:
        # The setup line may wrap at the pane's width: find it with the line breaks removed.
        flat, ends = "", []
        for line in lines:
            flat += line
            ends.append(len(flat))
        at = flat.rfind(after)
        if at < 0:
            return None
        end = at + len(after)
        lines = lines[next(i for i, e in enumerate(ends) if e >= end) + 1:]
    found, inside = set(), False
    for line in lines:
        if _header_rule(line):
            inside = not inside
            continue
        if inside:
            match = pattern.match(_header_text(line))
            if match:
                found.add(match.group(1))
    return found.pop() if len(found) == 1 else None


def _banner_session(pane: str, adapter: dict | None, marker: Path) -> str | None:
    """The session id in a just-started pane agent's banner, polled briefly
    (the banner draws a moment after `agent start`). Read before any prompt is
    sent, below this dispatch's own setup line: nothing an agent says counts."""
    pattern = adapters.session_output_pattern(adapter)
    if pattern is None:
        return None
    deadline = time.time() + float(os.environ.get("OFFICE_HERDR_SESSION_WAIT", "2"))
    while True:
        try:
            proc = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "60"],
                                  capture_output=True, text=True, timeout=10)
            text = proc.stdout if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            text = None
        found = banner_session(text, pattern, after=str(marker))
        if found or time.time() >= deadline:
            return found
        time.sleep(0.5)


def _transcript_session(run: dict, dispatch: dict, spec: dict, harness: str | None, cwd: Path, *,
                        since=None, con=None) -> str | None:
    """Record the id from the harness's own transcript of the session that was
    sent this dispatch's brief path, when the adapter declares that source.
    With `con`, it is recorded through that connection (its caller may hold
    the write lock); else through a connection of its own."""
    from office import transcripts
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or dispatch.get("harness") or "")
    if "transcript" not in (adapters.session_spec(adapter).get("sources") or []):
        return None
    harness = dispatch.get("harness") or harness
    try:
        found = transcripts.session_id(harness, marker=spec.get("prompt_file") or "",
                                       cwd=_session_cwd(spec, harness, cwd),
                                       since=since or dispatch.get("launched_at") or dispatch.get("started_at"))
    except Exception:  # a session probe never fails a launch
        return None
    if not found:
        return None
    if con is None:
        return _record_session(run, dispatch, found, source="transcript")
    outcome = record_session(con, run, dispatch["id"], found, harness=dispatch.get("harness"), source="transcript")
    if outcome in ("set", "same"):
        return found
    return state.get_dispatch(con, dispatch["id"]).get("session_id") if outcome == "mismatch" else None


def backfill_session(con, run: dict, d: dict) -> str | None:
    """A dispatch's session id: the recorded one, else one its own transcript
    still proves (an id herdr and the banner never showed live). Never a guess:
    None when no trustworthy source has it."""
    if not d or d.get("session_id"):
        return (d or {}).get("session_id")
    try:
        spec = json.loads((paths.run_dir(run["id"]) / "dispatches" / d["id"] / "launch.json").read_text())
    except (OSError, ValueError):
        return None
    if not spec.get("prompt_file"):
        return None
    return _transcript_session(run, d, spec, d.get("harness"), Path(spec.get("cwd") or "."), con=con)


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
    _ANSI = _CSI

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
            if _header_rule(line):
                self.rules += 1
                if self.rules >= 2:  # the header block is closed: the prompt follows
                    return self._stop("the header block closed without a session id line")
            elif self.rules == 0:
                self.lead += 1 if line else 0
                if self.lead > self.BANNER_LINES:  # no header block opens this stream
                    return self._stop("no header block opened the stream")
            else:
                found = self.pattern.match(_header_text(line))
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


def _record_session(run: dict, dispatch: dict, session: str, *, source: str) -> str | None:
    """Record `session` for the dispatch; the id now recorded for it, or None
    when `session` was not usable (malformed, another harness's)."""
    con = db.connect()
    try:
        outcome = record_session(con, run, dispatch["id"], session, harness=dispatch.get("harness"), source=source)
        if outcome in ("set", "same"):
            return session
        return state.get_dispatch(con, dispatch["id"]).get("session_id") if outcome == "mismatch" else None
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
    and reserved for the dispatch, under the pane lock (review F2). Raises
    ValueError, before splitting, when the reservation ledger is unreadable."""
    with _pane_lock(run):
        if dispatch_id:
            _read_reservations(run)
        res = _herdr_json(["pane", "split", "--pane", busy_pane, "--direction", "down", "--cwd", str(cwd), "--no-focus"])
        pane = (res.get("pane") or {}).get("pane_id")
        if not pane:
            return None
        tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
        try:
            layout = json.loads(tab_file.read_text()) if tab_file.is_file() else None
        except (OSError, ValueError):
            layout = None
        if layout is not None:
            layout.setdefault("panes", []).append(pane)
            atomic_write_json(tab_file, layout)
        if dispatch_id:
            _reserve_pane(run, pane, dispatch_id)
    return pane


def _pane_exists(pane: str) -> bool:
    """False only when herdr says the pane is gone; an unreachable herdr counts
    as present, so nothing is recorded closed that may still be open."""
    try:
        proc = subprocess.run(["herdr", "pane", "get", pane], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return "pane_not_found" not in (proc.stdout or "") + (proc.stderr or "")


def _pane_visible(pane: str) -> str:
    """The pane's visible screen, or "" when herdr cannot read it. An answered dialog
    can linger in scrollback; the screen shows what is up now."""
    try:
        proc = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "40"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _pane_read(pane: str) -> str:
    """The pane's recent text, or "" when herdr cannot read it."""
    try:
        proc = subprocess.run(["herdr", "pane", "read", pane, "--source", "recent-unwrapped", "--lines", "5000"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


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
    ddir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            _trial_launched(con, dispatch_id)
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
        if os.environ.get("OFFICE_PREFLIGHT") != "off":
            # The candidate's provider/model and credentials, validated on this
            # host before anything launches: an unlisted slug or missing auth
            # fails here with the reason recorded, never a silent wrong-model run.
            ok, why = adapters.run_preflight(adapter, d.get("model") or "")
            if not ok:
                _note(log_path, f"launch stopped before the harness started: {why}")
                _launch_notice(run, d, why)
                classification, code = "preflight_failed", 127
                return 1
        argv, prof = adapters.build_argv(adapter, spec["kind"], model=d["model"], effort=d["effort"] or "none",
                                         cwd=Path(spec["cwd"]), output=output,
                                         images=[Path(i) for i in spec.get("images") or []],
                                         include_dirs=[Path(i) for i in spec.get("include_dirs") or []],
                                         session_id=None if spec.get("headless_resume") else
                                         (d.get("session_id") if spec.get("headless_fresh") else _assigned_session(d)),
                                         resume_args=spec.get("headless_resume"))
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
            # The prompt itself is never persisted: evidence carries a placeholder in
            # the prompt's slot, built before the prompt is appended (#500).
            evidence_argv = list(argv)
            if prof.get("prompt") == "argv":
                argv, evidence_argv = argv + [prompt], evidence_argv + ["[PROMPT REDACTED]"]
            elif prof.get("prompt") == "argv-bound":
                flag = prof.get("prompt_flag", "--prompt=")
                argv, evidence_argv = argv + [flag + prompt], evidence_argv + [flag + "[PROMPT REDACTED]"]
            _record_launch_form(run, d, spec, "headless", evidence_argv,
                                transport=prof.get("prompt") or "file", output=spec.get("output"))
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
            sniffer = None if d.get("resumed_from") and not spec.get("headless_fresh") else _SessionSniffer(run, d, adapter, log_path)
            wrote = 0
            for chunk in iter(lambda: child.stdout.read1(65536), b""):
                wrote += len(chunk)
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
        if classification == "success" and not wrote and not output:
            # A worker with no reply file ended cleanly but said nothing: the
            # prompt may never have been consumed (adapter failure_signatures).
            _note(log_path, "the harness exited 0 with no output; the prompt may not have been consumed")
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
            _trial_launched(con, dispatch_id)
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
            if d["kind"] == "planner" and _submitted(con, d):
                # If a request arrived after this planner already submitted,
                # let its session end before starting the next revision.
                pending = con.execute("SELECT 1 FROM amendments WHERE run_id=? AND class='contract' "
                                      "AND to_plan_version IS NULL LIMIT 1", (run["id"],)).fetchone()
                if pending and not state.is_terminal(state.get_run(con, run["id"])):
                    try:
                        create_planner_task(con, run)
                    except Refused as err:
                        # Never roll back the recorded end: block the planner with the reason.
                        state.update_task(con, run["id"], PLANNER_TASK, status="blocked",
                                          pause_reason=f"follow-up planner refused: {err.message}")
                        state.emit(con, run, "task.blocked", f"{PLANNER_TASK} follow-up revision not launched: "
                                   f"{err.message}; {err.next_step or ''}".strip(), task_id=PLANNER_TASK)
            if d["kind"] == "executor" and d.get("task_id") and _submitted(con, d):
                # The session that submitted is gone: tasks stacked after it may start (#398).
                start_stacked(con, run, d["task_id"])
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
    """A worker that exits without submitting leaves a blocker, not a pass. A trial's worker that ends
    with no submission, no refusal and no question goes to the trial recovery instead of a relaunch on the
    route that just failed; any other end settles the trial (`submitted`, or `abandoned`)."""
    _after_worker_exit(con, run, dispatch_id)
    _close_trial(con, run, dispatch_id)


def _after_worker_exit(con, run: dict, dispatch_id: str) -> None:
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
        if wall and _queue_trial_recovery(con, run, d, f"harness quota wall: {wall}"):
            return  # a trial that hit a quota wall before any work falls back; one that worked blocks
        if wall:
            # Relaunching on the same route lands in the same exhausted quota
            # (issue #267 A3): block at once and say so, never retry.
            state.update_task(con, run["id"], task["id"], status="blocked",
                              pause_reason=f"harness quota exhausted on {d['triple']}: {wall}")
            state.emit(con, run, "task.blocked", f"{task['id']} worker hit a harness quota wall on {d['triple']} "
                       f"({wall}); not relaunching into the same quota. After it resets: office rerun {task['id']} "
                       f"--fresh; work is preserved in its worktree", task_id=task["id"])
            return
        from office import questions
        # Only an executor that ended cleanly stopped to ask: a crashed or killed worker's last
        # line is narration, and a planner question has no amend/rerun path (review F3, F6).
        asked = _ended_on_question(run, d) if _may_end_on_question(d) else None
        if asked:
            # A worker that stopped to ask needs an answer, not the same brief again: a
            # relaunch asks the same question (run 330605a8 relaunched one twice).
            state.update_task(con, run["id"], task["id"], status="blocked",
                              pause_reason=questions.ENDED_PREFIX + asked["question"][:200])
            state.emit(con, run, "task.blocked", f"{task['id']} worker ended on a question; answer it: "
                       f"{questions.answer_command(d, asked)}; work is preserved in its worktree", task_id=task["id"])
            return
        acked = [r[0] for r in con.execute("SELECT amendment_id FROM deliveries WHERE run_id=? AND dispatch_id=? "
                                           "AND status='applied' ORDER BY applied_at", (run["id"], dispatch_id))]
        if acked and task.get("current_revision_id"):
            # An amendment relaunch whose session acknowledged it and ended without
            # submitting (D24): a blind relaunch repeats the same brief with nothing left
            # to ack. The task keeps its revision and waits for the orchestrator, whose
            # next: line names the rerun; it is not shown live.
            which = ", ".join(dict.fromkeys(acked))
            state.update_task(con, run["id"], task["id"], status="changes_required",
                              pause_reason=f"{which} applied; the session ended without submitting")
            state.emit(con, run, "task.findings_queued", f"{task['id']} applied {which} and its session ended without "
                       f"submitting: office rerun {task['id']} --resume (resubmits on the amended contract) | --fresh",
                       task_id=task["id"])
            return
        if _queue_trial_recovery(con, run, d, f"worker ended ({d['terminal_classification']}) without submitting"):
            return
        # A dispatch that raised stopped on purpose (an answer, not a retry, resolves it): it is no failed attempt.
        retries = con.execute("SELECT COUNT(*) FROM dispatches d WHERE d.run_id=? AND d.task_id=? AND d.terminal_classification "
                              "IS NOT NULL AND NOT EXISTS (SELECT 1 FROM revisions r WHERE r.dispatch_id=d.id) "
                              "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.run_id=d.run_id AND e.dispatch_id=d.id "
                              "AND e.kind='task.raised')", (run["id"], task["id"])).fetchone()[0]
        limit = (state.pinned_config(run).get("verification") or {}).get("environment_retry_max", 2)
        if retries <= limit:
            state.emit(con, run, "task.relaunch", f"{task['id']} worker ended ({d['terminal_classification']}) "
                       f"without submitting; relaunching {retries}/{limit}", audience="runtime", task_id=task["id"])
            _launch_or_block(con, run, task["id"], role=d["role"])
            return
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"worker ended ({d['terminal_classification']}) without submitting")
        state.emit(con, run, "task.blocked", f"{task['id']} worker ended without submitting "
                   f"({d['terminal_classification']}); work is preserved in its worktree", task_id=task["id"])


# ------------------------------------------------------------------ trial launch-failure recovery (#494)



def trial_launch_failed(con, run: dict, dispatch_id: str | None, why: str) -> bool:
    """A trial dispatch whose launch job failed for good: queue its recovery instead of a bare blocker.
    False when the dispatch is not a live trial. Caller holds the transaction."""
    return _queue_trial_recovery(con, run, state.get_dispatch(con, dispatch_id) if dispatch_id else None, why)


def _queue_trial_recovery(con, run: dict, d: dict | None, why: str) -> bool:
    trial = open_trial(con, (d or {}).get("id"))
    if not trial:
        return False
    # The mark keeps `_close_trial` from abandoning a trial whose recovery is on its way.
    con.execute("UPDATE route_trials SET outcome=?, updated_at=? WHERE id=?",
                (dumps({"recovery": "queued", "why": why[:300]}), now_iso(), trial["id"]))
    state.enqueue(con, run, "trial_recovery", {"dispatch_id": d["id"], "task_id": d["task_id"], "why": why[:300]},
                  dedup_key=f"trial-recovery:{d['id']}", max_attempts=2)
    state.emit(con, run, "trial.recovering", f"{d['task_id']} trial route {trial['route']} did not run to a submission "
               f"({why[:120]}); confirming its worker is gone, then falling back to {trial['fallback_route']} if no "
               "work started", audience="runtime", task_id=d["task_id"], dispatch_id=d["id"])
    return True


def _close_trial(con, run: dict, dispatch_id: str) -> None:
    """The end of a trial dispatch the recovery did not take: a revision makes it `submitted`, anything
    else `abandoned` (revoked, signalled, ended on a question or a refused submit)."""
    trial = open_trial(con, dispatch_id)
    if not trial or "recovery" in (loads(trial.get("outcome"), None) or {}):
        return
    if con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (dispatch_id,)).fetchone():
        trial_submitted(con, dispatch_id)
    else:
        abandon_trial(con, dispatch_id, "the worker ended without a submission and without a recovery")


def _system_tool(name: str, *system: str) -> str:
    """A system tool by absolute path, so a restricted PATH (a sandbox, a test) cannot hide it."""
    return next((p for p in system if os.access(p, os.X_OK)), shutil.which(name) or name)


_PS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]{8}\s+\d{4})\s+(.*)$")


def _process_table() -> list[tuple[int, int, int, str, str, str]] | None:
    """(pid, ppid, pgid, state, start, command-and-environment) for every process, or None when the table cannot be
    trusted. `ps` runs in the C locale, so the start time parses (and matches a recorded identity) whatever the
    user's locale is; a table that does not contain this very process was misread, and says nothing."""
    try:
        listing = subprocess.run([_system_tool("ps", "/bin/ps", "/usr/bin/ps"), "-axeww", "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="],
                                 capture_output=True,
                                 text=True, timeout=10, check=True, env={**os.environ, "LC_ALL": "C", "LANG": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), " ".join(m.group(5).split()), m.group(6))
            for m in map(_PS_ROW.match, listing.splitlines()) if m]
    return rows if any(r[0] == os.getpid() for r in rows) else None


def _terminate_worker(pgid: int, tree: "_WorkerTree", *, term_wait: float = 3.0, kill_wait: float = 3.0) -> bool:
    """SIGTERM, then SIGKILL, the verified agent group (`pgid`, 0 for none) and every process the tree
    attributes to the worker; True once a scan finds none. A zombie is not a process that can write, and the
    scan does not count it."""
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        pids = tree.scan()
        if pids is None:
            return False
        if not pids:
            return True
        if pgid > 1:
            with contextlib.suppress(OSError):
                os.killpg(pgid, sig)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(0.1)
            pids = tree.scan()
            if pids is None:
                return False
            if not pids:
                return True
    pids = tree.scan()
    return pids is not None and not pids


class _WorkerTree:
    """The processes of one dispatch's worker: a recorded root that is still the process it was recorded as
    (pid and start time), anything that carries the dispatch's id in its environment, and everything
    descended from either. A process that left the group (setsid) is still found by its environment.
    This process and its ancestors are never the worker, whatever they carry."""

    def __init__(self, dispatch_id: str, roots: dict[int, str], pgid: int = 0) -> None:
        self.tag = re.compile(rf"(?:^|\s){_WORKER_TAG}={re.escape(dispatch_id)}(?:\s|$)")
        self.roots = roots
        self.pgid = pgid  # the verified agent's process group: every live member of it is the worker's
        self.seen: dict[int, str] = {}

    def scan(self, _root=None) -> set[int] | None:
        table = _process_table()
        if table is None:
            return None
        children: dict[int, list[int]] = {}
        started, parent, tagged, grouped = {}, {}, set(), set()
        for pid, ppid, pgid, state_, start, command in table:
            if state_.startswith("Z"):
                continue  # exited and waiting to be reaped: it cannot write
            children.setdefault(ppid, []).append(pid)
            started[pid], parent[pid] = start, ppid
            if self.tag.search(command):
                tagged.add(pid)
            if self.pgid > 1 and pgid == self.pgid:
                grouped.add(pid)
        mine, pid = set(), os.getpid()
        while pid in parent and pid not in mine:
            mine.add(pid)
            pid = parent[pid]

        def started_by_me(pid: int) -> bool:
            seen = set()
            while pid in parent and pid not in seen:
                if pid == os.getpid():
                    return True
                seen.add(pid)
                pid = parent[pid]
            return False

        def with_descendants(seed) -> set[int]:
            out, queue = set(mine), list(seed)  # this process and its ancestors are never walked into
            while queue:
                pid = queue.pop()
                if pid not in out:
                    out.add(pid)
                    queue.extend(children.get(pid, []))
            return out - mine

        named = {p for p, st in {**self.seen, **self.roots}.items() if started.get(p) == st}
        # A tag alone is not proof: the `ps`, `lsof` or `git` this process runs inherits the tag it carries. A
        # recorded identity or the agent's group stays the worker's whoever started it.
        found = with_descendants(named | grouped) | with_descendants(p for p in tagged if p not in mine and not started_by_me(p))
        found = {p for p in found if p in started and p > 1}
        self.seen.update({p: started[p] for p in found})
        return found


def _recorded_identity(run: dict, dispatch_id: str, which: str) -> dict | None:
    try:
        rec = json.loads(_identity_file(run["id"], dispatch_id, which).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec.get("pid"), int) else None


def stop_worker_tree(run: dict, d: dict) -> tuple[bool, str]:
    """Terminate the failed dispatch's worker process tree and confirm it is gone: (confirmed, detail).

    Roots come from the identities recorded at launch (supervisor and agent, each a pid and start time)
    and the pane Herdr hosts. Closing a pane or ending a binding is not proof of exit, so after the
    signals the tree is scanned again and must be empty. A recorded process whose identity cannot be
    read (alive but no readable start time, or a live pid with no record), or a process table that cannot
    be read, is not confirmed: the caller blocks rather than starting a second writer."""
    notes: list[str] = []
    if d.get("launcher") == "herdr" and d.get("pane_id") and shutil.which("herdr"):
        try:
            notes.append(f"pane {reclaim_pane(run, d['id'], explicit=True)}")
        except Exception as exc:  # the process scan below still decides
            notes.append(f"pane close failed: {exc}")
    roots: dict[int, str] = {}
    agent_pgid = 0
    for which in ("supervisor", "agent"):
        rec = _recorded_identity(run, d["id"], which)
        if rec is None:
            continue
        pid = rec["pid"]
        if pid_state(pid) == DEAD:
            continue
        # A record made by this version carries the C-locale start; an older one only the user-locale start.
        recorded, now = ((rec["c_start"], _c_start(pid)) if rec.get("c_start") else (rec.get("start"), process_start(pid)))
        if not recorded or now is None:
            return False, f"recorded {which} pid {pid} is alive and its identity cannot be proven (start time unreadable)"
        if now == recorded:
            roots[pid] = _c_start(pid) or now
            agent_pgid = pid if which == "agent" else agent_pgid
    if not agent_pgid:
        # The leader may be gone while a member of its group is not. A group id cannot be reused while the group has
        # members, so the recorded one is the worker's unless a live process now has that pid (a new group may have it).
        try:
            recorded = int(_agent_pgid_file(run, d["id"]).read_text().strip())
        except (OSError, ValueError):
            recorded = 0
        if recorded > 1 and pid_state(recorded) == DEAD:
            agent_pgid = recorded
    pid = d.get("pid")
    if pid and pid != os.getpid() and not _recorded_identity(run, d["id"], "supervisor") and pid_alive(pid):
        return False, f"supervisor pid {pid} is alive and has no recorded identity to prove which process it is"
    tree = _WorkerTree(d["id"], roots, agent_pgid)
    gone = _terminate_worker(agent_pgid, tree)
    left = tree.scan()
    if not gone or left is None or left:
        return False, ("worker processes remain: " + (", ".join(map(str, sorted(left))) if left else
                                                      "the process table cannot be read, so exit is unconfirmed"))
    # A helper that cleared its environment and left the group carries nothing that names the dispatch. If
    # one still has the worktree as its working directory it may be writing there: not confirmed.
    if d.get("worktree") and Path(d["worktree"]).is_dir():
        for attempt in range(3):  # a closing pane's shell takes a moment to go
            holders = _cwd_holders(Path(d["worktree"]))
            table = _process_table()
            if holders is None or table is None:
                return False, "the processes working in the worktree cannot be listed, so exit is unconfirmed"
            holders = _foreign(holders, table)
            if not holders:
                break
            time.sleep(0.5)
        else:
            names = {row[0]: row[5].split(None, 1)[0] for row in table if row[0] in holders}
            return False, ("processes with an unattributed working directory in the worktree remain: "
                           + ", ".join(f"{pid} ({names.get(pid, '?')})" for pid in sorted(holders)))
    return True, "; ".join([*notes, f"confirmed no process of {d['id']} remains"
                            + (f" (terminated {len(roots)} recorded)" if roots else "")])


def _file_stamp(path: Path) -> str:
    """What changes when a file does: link text for a symlink (never followed), else size and mtime."""
    try:
        st = path.lstat()
    except OSError:
        return "gone"
    return "link:" + os.readlink(path) if stat.S_ISLNK(st.st_mode) else f"{st.st_size}:{st.st_mtime_ns}"


def _worktree_snapshot(wt: Path) -> dict | None:
    """HEAD, the index, and a stamp for every tracked and every non-ignored untracked path. Taken until two
    consecutive reads agree, so it describes a worktree nothing is writing. None when it never settles or git
    cannot say. The worktree is the failed agent's, so this reads names and stamps only: `git status` would
    hash content through whatever filter drivers the agent configured, and run them here."""
    def git_z(*args: str) -> list[str]:
        raw = subprocess.run(["git", "-C", str(wt), "-c", "core.fsmonitor=false", *args], capture_output=True, timeout=120)
        if raw.returncode != 0:
            raise OSError(raw.stderr.decode("utf-8", "replace"))
        return [x for x in raw.stdout.decode("utf-8", "surrogateescape").split("\0") if x]

    def once() -> dict | None:
        try:
            head = paths.git(wt, "rev-parse", "HEAD")
            index = hashlib.sha256("\0".join(git_z("ls-files", "-s", "-z")).encode("utf-8", "surrogateescape")).hexdigest()
            names = sorted({*git_z("ls-files", "-z"), *git_z("ls-files", "-o", "-z", "--exclude-standard")})
        except (OSError, subprocess.SubprocessError, paths.GitError):
            return None
        return {"head": head, "index": index, "files": {name: _file_stamp(wt / name) for name in names}}

    previous = once()
    for _ in range(4):
        time.sleep(0.2)
        current = once()
        if current is not None and current == previous:
            return current
        previous = current
    return None


def _foreign(pids: set[int], table: list[tuple]) -> set[int]:
    """`pids` that are neither this process, its ancestors, nor something this process started (the `lsof`
    that listed them, a `git` it ran: a supervisor that works from the worktree gives them that directory), and
    that still exist in `table`."""
    parent = {row[0]: row[1] for row in table}
    me, mine = os.getpid(), set()
    pid = me
    while pid in parent and pid not in mine:
        mine.add(pid)
        pid = parent[pid]

    def ours(pid: int) -> bool:
        seen = set()
        while pid in parent and pid not in seen:
            seen.add(pid)
            pid = parent[pid]
            if pid == me:
                return True
        return False

    return {p for p in pids if p in parent and p not in mine and not ours(p)}


def _cwd_holders(wt: Path) -> set[int] | None:
    """Processes whose working directory is inside `wt`, or None when they cannot be listed. Linux reads
    /proc; elsewhere `lsof`, whose failure (nonzero beyond its "nothing found", or no listing at all) is not
    an empty answer."""
    root = str(wt.resolve())
    inside = lambda path: path == root or path.startswith(root + os.sep)
    if Path("/proc/self/cwd").exists():
        holders = set()
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if inside(os.readlink(entry / "cwd")):
                        holders.add(int(entry.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue  # gone since the listing
                except OSError:
                    return None  # a process whose directory cannot be read may be the one writing
        return holders
    try:
        proc = subprocess.run([_system_tool("lsof", "/usr/sbin/lsof", "/usr/bin/lsof"), "-nP", "-a", "-d", "cwd", "-F", "pn"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        return None
    holders, pid = set(), None
    for line in proc.stdout.splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == "n" and pid and inside(line[1:]):
            holders.add(pid)
    return holders


def work_started(con, d: dict, ddir: Path) -> tuple[bool, str]:
    """Whether the failed worker did meaningful work, once its process tree is confirmed gone: a submitted
    revision, a commit past the dispatch's base, or a worktree that now differs from the one the launch left
    (any tracked change or non-ignored untracked file). Ambiguity counts as work: an unreadable snapshot, or
    a worktree that was launched on with no baseline recorded, blocks the fallback."""
    if con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (d["id"],)).fetchone():
        return True, "a revision was submitted"
    if not d.get("worktree") or not (Path(d["worktree"]) / ".git").exists():
        return False, "the dispatch never got a worktree"
    wt = Path(d["worktree"])
    after = _worktree_snapshot(wt)
    if after is None:
        return True, "the worktree could not be snapshotted, so work cannot be ruled out"
    try:
        before = json.loads((ddir / "worktree-baseline.json").read_text())
    except (OSError, ValueError):
        started = [n for n in ("launch.json", "agent.identity", "supervisor.identity", "agent.pgid") if (ddir / n).exists()]
        if d.get("launcher") or d.get("pid") or started:
            return True, "the worktree has no launch baseline to compare against, so work cannot be ruled out"
        return False, "no agent was ever launched in this dispatch"
    if after["head"] != before["head"]:
        return True, f"the branch moved from {str(before['head'])[:8]} to {after['head'][:8]}"
    changed = sorted(p for p in {*after["files"], *before["files"]} if after["files"].get(p) != before["files"].get(p))
    if after["index"] != before.get("index", after["index"]):
        changed.insert(0, "the index")
    if changed:
        return True, "the worktree changed after launch: " + ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
    return False, "no revision, no commit and no worktree change since the launch"


def _recovery_blocked(con, run: dict, d: dict, trial: dict, status: str, reason: str, next_step: str, *,
                      exited: bool = False) -> dict:
    """Stop here: the worktree and the lease stay as they are, the trial is settled and the task blocks with
    a next step. Nothing is launched and no lease moves. A dispatch whose worker is confirmed gone is ended,
    so it does not stay a live session for the next dispatch to refuse on; one whose exit is not confirmed is
    left as it is."""
    with db.transaction(con):
        task = state.get_task(con, run["id"], d["task_id"])
        if exited:
            _end_failed_dispatch(con, run, d["id"], "launch_failed")
        set_trial_status(con, trial, status, origin="recovery", detail=reason, outcome={"recovery": "blocked", "why": reason})
        if task and task["current_dispatch_id"] == d["id"] and task["status"] in ("running", "launching"):
            state.update_task(con, run["id"], task["id"], status="blocked", pause_reason=f"trial {trial['route']}: {reason}"[:300])
            state.emit(con, run, "task.blocked", f"{d['task_id']} trial route {trial['route']} failed: {reason}; no fallback "
                       f"was started. {next_step}", task_id=d["task_id"], dispatch_id=d["id"])
    return {"recovery": "blocked", "reason": reason}


def _end_failed_dispatch(con, run: dict, dispatch_id: str, classification: str) -> None:
    """Record the end of a dispatch whose worker is confirmed gone and never reported one. Caller holds the tx."""
    ended = con.execute("UPDATE dispatches SET status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END, "
                        "terminal_classification=COALESCE(terminal_classification, ?), ended_at=? "
                        "WHERE id=? AND ended_at IS NULL", (classification, now_iso(), dispatch_id)).rowcount
    if ended:
        d = state.get_dispatch(con, dispatch_id)
        state.emit(con, run, "dispatch.ended", f"{d.get('task_id') or d['role']} {d['role']} ended: {classification}",
                   audience="runtime", task_id=d.get("task_id"), dispatch_id=dispatch_id,
                   payload={"exit_code": None, "signal": None, "classification": classification})


def _recovery_fallback(con, run: dict, task: dict, d: dict, trial: dict) -> tuple[dict | None, str]:
    """The recorded fallback route, decided again from live evidence (quota, trust, permission, floors)."""
    recorded = (d.get("route") or {}).get("discovery") or {}
    route = trial["fallback_route"]
    if not recorded.get("fallback"):
        return None, f"the dispatch recorded no fallback payload for {route}"
    try:
        fresh = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id=task["id"], exact=route,
                                      dispatch_kind="fix" if task.get("current_dispatch_id") else "fresh")
    except Refused as refused:  # e.g. policy-unreadable: the fallback cannot be shown to be permitted
        return None, f"the recorded fallback {route} cannot be confirmed ({refused.message})"
    if fresh.get("status") != "selected" or fresh.get("selected") != route:
        why = "; ".join(f"{r['candidate']}: {r['reason']}" for r in (fresh.get("rejected") or [])[:2]) or fresh.get("status")
        return None, f"the recorded fallback {route} no longer qualifies ({why})"
    fresh["route_source"] = "router"
    return fresh, ""


def job_trial_recovery(con, run: dict, job: dict) -> dict:
    """A failure inside the recovery blocks the task with its reason: a dead worker and a trial left open
    would otherwise wait for nobody."""
    progress = {"exited": False}
    try:
        return _trial_recovery(con, run, job, progress)
    except db.StaleAttempt:
        raise
    except Exception as exc:
        return _recovery_crashed(con, run, job["payload"]["dispatch_id"], f"{type(exc).__name__}: {exc}",
                                 exited=progress["exited"]) or _reraise(exc)


def _reraise(exc: BaseException):
    raise exc


def _recovery_crashed(con, run: dict, dispatch_id: str, err: str, *, exited: bool = False) -> dict | None:
    """The recovery failed (an exception in it, or its job failed for good): block the task and settle the
    trial, so neither waits for a recovery that is not coming. None when there is no open trial."""
    d = state.get_dispatch(con, dispatch_id)
    trial = open_trial(con, dispatch_id) if d else None
    if not trial:
        return None
    return _recovery_blocked(con, run, d, trial, "abandoned", f"the recovery itself failed ({err[:200]})",
                             f"The worktree is preserved; office rerun {d['task_id']} --fresh, or office dispatch "
                             f"{d['task_id']} --reroute", exited=exited)


def trial_recovery_failed(con, run: dict, job: dict, err: str) -> None:
    """A trial_recovery job that failed for good (killed twice, a version mismatch): the same block. Caller holds
    the transaction."""
    _recovery_crashed(con, run, job["payload"].get("dispatch_id"), err)


def _trial_recovery(con, run: dict, job: dict, progress: dict) -> dict:
    """The outbox job behind a trial's launch failure (#494). In order: end the failed worker's process
    tree and confirm it is gone; decide from the revisions, commits and a stable worktree snapshot whether
    work started; and only for a confirmed pre-work failure release the trial's lease, end its binding, mark
    `launch-failed` and dispatch the recorded fallback, after re-checking the gates a dispatch checks. Any
    other outcome blocks with a next step and leaves the worktree and lease alone."""
    d = state.get_dispatch(con, job["payload"]["dispatch_id"])
    trial = open_trial(con, d["id"]) if d else None
    if not trial:
        return {"skipped": "not an open trial"}
    task = state.get_task(con, run["id"], d["task_id"])
    if task is None or task["current_dispatch_id"] != d["id"] or task["status"] not in ("running", "launching"):
        with db.transaction(con):
            abandon_trial(con, d["id"], "the task no longer belongs to this dispatch")
        return {"skipped": "superseded"}
    tid = task["id"]
    rerun_hint = f"office rerun {tid} --fresh (after reading the worktree), or office dispatch {tid} --reroute"
    exited, detail = stop_worker_tree(run, state.get_dispatch(con, d["id"]))
    if not exited:
        return _recovery_blocked(con, run, d, trial, "abandoned", f"its worker process could not be confirmed gone ({detail})",
                                 f"Stop the process by hand and confirm it with ps, then {rerun_hint}. The worktree and "
                                 f"lease {d.get('lease_id')} are preserved")
    progress["exited"] = True
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    meaningful, evidence = work_started(con, state.get_dispatch(con, d["id"]), ddir)
    if meaningful:
        return _recovery_blocked(con, run, d, trial, "abandoned", f"work had started ({evidence})",
                                 f"The worktree {d.get('worktree')} is preserved; {rerun_hint}", exited=True)
    fallback, why = _recovery_fallback(con, run, task, d, trial)
    if fallback is None:
        return _recovery_blocked(con, run, d, trial, "launch-failed", why,
                                 f"The worker is gone and no work started; route it again: office dispatch {tid} --reroute",
                                 exited=True)
    from office import plans, queuecmd
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        task = state.get_task(con, run["id"], tid)
        cur = state.get_dispatch(con, d["id"])
        try:
            if task["current_dispatch_id"] != d["id"] or task["status"] not in ("running", "launching") or not open_trial(con, d["id"]):
                raise Refused("state-changed", f"{tid} changed during the recovery")
            if state.is_terminal(run):
                raise Refused("run-terminal", f"run is {run['phase']}")
            plans.require_dispatchable(con, run)
            plans.require_scope_clear(con, run, tid)
            held = queuecmd.paused_block(con, run["id"], tid)
            if held:
                raise Refused("scheduler-paused", f"{held['id']} is paused by the operator")
            fresh_lease = live_lease(con, run["id"], cur["lease_id"]) if cur.get("lease_id") else None
            if cur.get("lease_id") and not fresh_lease:
                raise Refused("lease-lost", f"{cur['lease_id']} is no longer this dispatch's live lease")
        except Refused as refused:
            reason = f"a gate no longer allows the fallback ({refused.message})"
        else:
            reason = None
            now = now_iso()
            _end_failed_dispatch(con, run, d["id"], "launch_failed")
            if cur.get("lease_id"):
                con.execute("UPDATE leases SET released_at=?, revoke_reason='trial launch failed before any work' "
                            "WHERE id=? AND released_at IS NULL AND revoked_at IS NULL", (now, cur["lease_id"]))
                con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], "executor", tid, d["id"], None,
                                                        "release", now))
            if cur.get("session_id") and cur.get("harness"):
                con.execute("UPDATE session_bindings SET ended_at=? WHERE harness=? AND session_id=? AND run_id=? "
                            "AND ended_at IS NULL", (now, cur["harness"], cur["session_id"], run["id"]))
            set_trial_status(con, trial, "launch-failed", origin="recovery", detail=f"pre-work launch failure: {evidence}")
            _record_routing(con, run, fallback)
            note_route(con, run, task, fallback)
            new = request_launch(con, run, tid, role="executor", decision=fallback, base=cur.get("base_commit"))
            state.record_route_change(con, run, tid, trial["route"], fallback["selected"], kind="trial-fallback",
                                      actor="office", dispatch_id=new,
                                      reason=f"trial launch failed before any work ({job['payload'].get('why')})")
            set_trial_status(con, trial, "fell-back", origin="recovery", dispatch_id=new,
                             detail=f"dispatched known-working {fallback['selected']} as {new}",
                             outcome={"recovery": "fell-back", "fallback_dispatch": new})
            state.emit(con, run, "trial.fell_back", f"{tid} trial route {trial['route']} failed before any work; "
                       f"dispatched {fallback['selected']} as {new}", task_id=tid, dispatch_id=new)
    if reason:
        return _recovery_blocked(con, run, d, trial, "launch-failed", reason,
                                 f"The worker is gone and no work started; resolve that, then office dispatch {tid} --reroute",
                                 exited=True)
    if cur.get("launcher") == "herdr" and cur.get("pane_id"):
        with _pane_lock(run):
            _unreserve_pane(run, cur["pane_id"])
    return {"recovery": "fell-back", "fallback_dispatch": new}


def _may_end_on_question(d: dict) -> bool:
    return d.get("role") == "executor" and d.get("terminal_classification") == "success"


def _ended_on_question(run: dict, d: dict) -> dict | None:
    """The question a worker's final message ended on, saved where `office wait`
    finds it (question.json), or None."""
    from office import questions
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    from office import gates
    try:
        with open(gates.log_path(d, ddir), "rb") as fh:  # only the final message matters: read the tail
            fh.seek(0, os.SEEK_END)
            start = max(fh.tell() - 16384, 0)
            fh.seek(start)
            tail = fh.read().decode("utf-8", errors="replace")
        if start:
            tail = tail.split("\n", 1)[-1]  # the first line is cut mid-line
        q = questions.final_question(tail)
    except OSError:
        return None
    if q:
        atomic_write_json(ddir / "question.json", q)
    return q


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
    stays in runs.db and rides on the worker's next office command."""
    from office import gates
    payload = job["payload"]
    d = state.get_dispatch(con, payload["dispatch_id"])
    unblock = bool(payload.get("unblock"))
    if not d or d.get("launcher") != "herdr" or not d.get("pane_id") or d["status"] != "running" \
            or (unblock and not gates._agent_alive(herdr_agent_name(d["id"]))):
        if unblock and d:
            _amendment_undelivered(con, run, payload, d)
        return {"sent": False}
    text = payload.get("text", "office status has an update for you.")
    try:
        with fenced_pane(run, d) as pane:
            landed = submit_prompt(pane, text, pane=pane)
    except PaneMismatch as exc:
        with db.transaction(con):
            state.emit(con, run, "prompt", f"{d.get('task_id') or d['id']} {d['id']}: native nudge refused, pane "
                       f"{d['pane_id']} is not this dispatch's: {exc}", audience="runtime",
                       task_id=d.get("task_id"), dispatch_id=d["id"])
        if unblock:
            _amendment_undelivered(con, run, payload, d)
        return {"sent": False, "refused": str(exc)}
    if unblock:
        if landed == "landed":
            from office import submit
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


def _launch_or_block(con, run: dict, task_id: str, **kw) -> str | None:
    """An internal relaunch (auto-relaunch, stacked start): a refusal blocks the task with
    its reason instead of rolling back the transition that asked for it."""
    try:
        return request_launch(con, run, task_id, **kw)
    except Refused as err:
        state.update_task(con, run["id"], task_id, status="blocked", pause_reason=f"relaunch refused: {err.message}")
        state.emit(con, run, "task.blocked", f"{task_id} not relaunched: {err.message}; {err.next_step or ''}".strip(),
                   task_id=task_id)
        return None


def stack_released(con, run: dict, holder_id: str) -> str | None:
    """Why a task stacked after `holder_id` may start now (a phrase), or None. The holder was
    accepted, or it submitted and no session of it is live: in a convergence RECHECK a submitted
    task can wait on its lane for a long time, and its ended session edits nothing (#398)."""
    holder = state.get_task(con, run["id"], holder_id) or {}
    if holder.get("status") == "accepted":
        return "is already accepted"
    if (holder.get("current_revision_id") and holder.get("status") not in ("running", "launching")
            and _live_session(con, run["id"], holder_id) is None):
        return f"submitted {holder['current_revision_id']} and has no live session"
    return None


def _live_session(con, run_id: str, task_id: str) -> str | None:
    from office import gates
    return gates.live_task_session(con, run_id, task_id)


def start_stacked(con, run: dict, holder_task: str) -> list[str]:
    """Launch tasks the orchestrator stacked after `holder_task` once it is accepted, or has
    submitted with no live session (stack_released). A stacked task whose scope another live
    session holds stays queued. Caller holds tx."""
    started = []
    if not stack_released(con, run, holder_task):
        return started
    for t in state.tasks(con, run["id"]):
        if t["status"] == "queued" and t.get("stack_after") == holder_task:
            if scope_holder(con, run, t):
                continue
            graph = {x["id"]: x["depends"] for x in state.tasks(con, run["id"])}
            parts: list = []
            try:
                base = _base_for(con, run, t, graph, holder_task, parents=parts)
            except Refused as err:
                # Acceptance must not raise out of its own transition: pause the dependent with the reason.
                state.update_task(con, run["id"], t["id"], status="paused", pause_reason=err.message)
                state.emit(con, run, "task.paused", f"PAUSED {t['id']}: {err.message}; {err.next_step or ''}".strip(),
                           task_id=t["id"], payload={"reason": err.message})
                continue
            if _launch_or_block(con, run, t["id"], role="executor", base=base, extra={"base_parents": parts}):
                started.append(t["id"])
    return started
