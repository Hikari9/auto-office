"""`office amend route`: re-record a live dispatch's producer route (#301).

The user switched the model in a running pane, or wants it switched. Without
`--restart` only the record changes: the dispatch keeps its lease, session and
harness, and receipts plus the review independence check name the model that
actually works. With `--restart` the new route is recorded first, then the
current agent is interrupted and the task relaunched on the same harness with
the new model/effort, resuming its session where the harness can.
"""
from __future__ import annotations

from pathlib import Path

from office import adapters, candidates, db, dispatch, jobs, routing, scoring, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, loads, now_iso

USAGE = 'office amend route <task|dispatch> --as <harness>/<model>[@effort] --quote "<user\'s words>" [--restart]'
LIVE = ("launching", "running")
PENDING = ("planned", "queued", "changes_required")  # no live agent: a route change declares the route the next dispatch/rerun runs


def change_route(con, run: dict, target: str | None, as_route: str | None, quote: str | None, *,
                 restart: bool = False) -> Result:
    if not target or not as_route:
        raise Usage("usage", "name the live dispatch and its new route", next_step=USAGE, exit_code=2)
    if not (quote or "").strip():
        raise Usage("quote-required", "a route change records the user's words (--quote)", next_step=USAGE, exit_code=2)
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}")
    pending = _pending_task(con, run, target)
    if pending:
        return _declare(con, run, pending, as_route, quote, restart)
    task, d = _live_dispatch(con, run, target)
    tid = task["id"]
    want = candidates.parse_route_override(as_route)
    if not want["model_id"]:
        raise Usage("invalid-route", f"--as {as_route!r}: expected <harness>/<model>[@effort]", next_step=USAGE,
                    exit_code=2)
    harness = d.get("harness") or (d.get("route") or {}).get("candidate", {}).get("harness")
    if want["harness"] and want["harness"] != harness:
        raise Refused("harness-fixed", f"{d['id']} runs on {harness}; a route change keeps the harness "
                      f"({want['harness']} requested)", scope=tid, preserved="the dispatch and its route",
                      next_step=f"office amend route {tid} --as {harness}/<model>[@effort], or office revoke {tid} "
                                f"then office dispatch {tid} --as {as_route}")
    effort = want["effort"] or d.get("effort")
    _require_known(harness, want["model_id"], effort)
    cand = candidates.declared_candidate(harness, want["model_id"], effort)
    after = routing.candidate_id(cand)
    before = d.get("triple")
    if after == before:
        raise Refused("route-unchanged", f"{d['id']} already records {before}", scope=tid)
    change = {"before": before, "after": after, "quote": quote, "restart": restart, "dispatch_id": d["id"]}
    extra = {"quote": quote, "restart": restart}
    lines = [f"{tid} {d['id']} route {before} -> {after} (recorded; lease and session kept)"]
    with db.transaction(con):
        cur = state.get_dispatch(con, d["id"])
        if cur.get("ended_at") or cur["status"] not in LIVE or _submitted(con, cur):
            raise Refused("state-changed", f"{d['id']} changed while the route was being recorded; re-run", scope=tid,
                          next_step=USAGE)
        _record(con, cur, cand, change)
        dispatch.abandon_trial(con, cur["id"], f"the user declared {after}: no longer a discovery trial")
        state.update_task(con, run["id"], tid, route_json=dumps(_declared_payload(cand, before, quote)))
        lines += _recheck_review(con, run, tid, cand)
        new = None
        if restart:
            new, how = _relaunch(con, run, task, cur, cand)
            change.update(relaunch=new, how=how)
            extra.update(relaunch=new, how=how)
            lines[0] = f"{tid} {d['id']} route {before} -> {after} (recorded)"
            stopped = (f"{d['id']} interrupted" if cur.get("launcher") not in (None, "external", "sync") else
                       f"{d['id']} is not under Office's process control: stop its agent by hand (its lease is "
                       "revoked, so a late submit is fenced out)")
            lines.append(f"{tid} -> {new} executor {how} on {after} launching; {stopped}")
        state.record_route_change(con, run, tid, before, after, kind="reroute", actor="user",
                                  reason=f"user route change: \"{quote.strip()}\"" + (f" (restarted as {new})" if new else ""),
                                  dispatch_id=d["id"], extra=extra)
    if restart:
        notes: list[str] = []
        dispatch._end_dispatch(con, run, state.get_dispatch(con, d["id"]), "route-restart",
                               f"restarted on {after} as {new}", notes=notes)
        lines += notes
        jobs.kick(con, run["id"])
    return Result(lines=lines, next="exceptions only; office status")


def _declared_payload(cand: dict, before: str | None, quote: str) -> dict:
    """The task's recorded route after a deliberate change: dispatch and every
    relaunch follow it (`declared`), with no fallback behind it."""
    triple = routing.candidate_id(cand)
    return {"candidate": cand, "declared": True, "override": True, "route_source": "declared",
            "selection_disclosure": {"triple": triple, "override": True,
                                     "reason": f"declared route change ({before or 'unrecorded'} -> {triple})"},
            "route_change": {"before": before, "after": triple, "quote": quote}}


def _pending_task(con, run: dict, target: str) -> dict | None:
    """The executor task `target` names when it has no agent yet (planned, or
    queued behind a stack): its route is declared, not re-recorded."""
    if target[:1] in "dD" and len(target) > 2:
        return None
    task = state.get_task(con, run["id"], target.upper())
    if task is None or task["role"] == "planner" or task["status"] not in PENDING:
        return None
    if task.get("current_dispatch_id"):
        d = state.get_dispatch(con, task["current_dispatch_id"])
        if d and not d.get("ended_at"):
            return None
    return task


def _declare(con, run: dict, task: dict, as_route: str, quote: str, restart: bool) -> Result:
    """Deliberately reroute a pending role: record the new route on the task so
    dispatch runs it (qualification still applies there: trust, capability,
    quota), and say so in a `route.changed` event."""
    tid = task["id"]
    if restart:
        raise Usage("nothing-to-restart", f"{tid} has no running agent; --restart applies to a live dispatch",
                    next_step=USAGE, exit_code=2)
    want = candidates.parse_route_override(as_route)
    rec = state.recorded_route(task)
    before = routing.candidate_id(rec["candidate"]) if rec else None
    if before is None:
        from office import dispatch
        plan = dispatch._planned_slate(con, run, tid)
        before = (plan or {}).get("primary")
    harness = want["harness"] or ((rec.get("candidate") or {}).get("harness") if rec else None)
    if not harness or not want["model_id"]:
        raise Usage("invalid-route", f"--as {as_route!r}: expected <harness>/<model>[@effort]", next_step=USAGE,
                    exit_code=2)
    effort = want["effort"] or (rec.get("candidate") or {}).get("effort")
    _require_known(harness, want["model_id"], effort)
    cand = candidates.declared_candidate(harness, want["model_id"], effort)
    after = routing.candidate_id(cand)
    if before and scoring.normalize_triple(after) == scoring.normalize_triple(before):
        raise Refused("route-unchanged", f"{tid} already runs {before}", scope=tid)
    with db.transaction(con):
        cur = state.get_task(con, run["id"], tid)
        if cur["status"] not in PENDING or cur.get("current_dispatch_id") != task.get("current_dispatch_id"):
            raise Refused("state-changed", f"{tid} changed while the route was being recorded; re-run", scope=tid,
                          next_step=USAGE)
        state.update_task(con, run["id"], tid, route_json=dumps(_declared_payload(cand, before, quote)))
        state.record_route_change(con, run, tid, before, after, kind="reroute", actor="user",
                                  reason=f"user route change: \"{quote.strip()}\"", extra={"quote": quote, "pending": True})
    return Result(lines=[f"{tid} route {before or 'unrecorded'} -> {after} (declared; dispatch runs it, no fallback)"],
                  next=f"office dispatch {tid}")


def _live_dispatch(con, run: dict, target: str) -> tuple[dict, dict]:
    d = None
    if target[:1] in "dD" and len(target) > 2:
        d = state.get_dispatch(con, target[:1].upper() + target[1:])
        if d is None or d["run_id"] != run["id"]:
            raise Usage("unknown-dispatch", f"no dispatch {target} in this run", next_step=USAGE)
        task = state.get_task(con, run["id"], d["task_id"]) if d.get("task_id") else None
    else:
        task = state.get_task(con, run["id"], target.upper())
        if task is None or task["role"] == "planner":
            raise Usage("unknown-task", f"{target} is not a task in this run", next_step=USAGE)
        if task.get("current_dispatch_id"):
            d = state.get_dispatch(con, task["current_dispatch_id"])
    if task is None or (d is not None and d.get("role") != "executor"):
        raise Refused("not-executor", f"{target} is not an executor dispatch; only a producer route is re-recorded")
    tid = task["id"]
    if d is not None and _submitted(con, d):
        raise Refused("submitted", f"{tid} {d['id']} already submitted; its route is part of the submitted evidence",
                      scope=tid, preserved="the submission and its route",
                      next_step=f"office rerun {tid} --fresh --reroute after its review, for a new route")
    if d is None or d.get("ended_at") or d["status"] not in LIVE or task.get("current_dispatch_id") != d["id"]:
        name = d["id"] if d else tid
        nxt = (f"office rerun {tid} --fresh --reroute" if d and task.get("current_dispatch_id") == d["id"]
               else f"office dispatch {tid} --as <harness>/<model>[@effort]")
        raise Refused("dispatch-not-live", f"{name} is not a live dispatch; a route change re-records a running agent",
                      scope=tid, next_step=nxt)
    return task, d


def _submitted(con, d: dict) -> bool:
    return con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (d["id"],)).fetchone() is not None


def _require_known(harness: str, model: str, effort: str | None) -> None:
    adapter = adapters.load_all().get(harness) or {}
    rows = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == harness]
    models = sorted({m for r in rows for m in (r.get("model_id"), r.get("invocation_model_id")) if m})
    if model not in models:
        raise Usage("unknown-model", f"{harness} has no model {model!r}",
                    next_step=f"known {harness} models: {', '.join(models) or 'none'}", exit_code=2)
    efforts = sorted(adapter.get("effort_mapping") or {}) \
        or sorted({r["effort"] for r in rows if r.get("effort")})
    if effort and effort not in efforts:
        raise Usage("unknown-effort", f"{harness} has no effort {effort!r}",
                    next_step=f"known {harness} efforts: {', '.join(efforts)}", exit_code=2)


def _record(con, d: dict, cand: dict, change: dict) -> None:
    """Rewrite the dispatch's route in place: lease, session and harness stay."""
    triple = routing.candidate_id(cand)
    reason = f"user route change ({change['before']} -> {triple})"
    route = {**(d.get("route") or {}), "candidate": cand, "override": True,
             "selection_disclosure": {"triple": triple, "reason": reason, "override": True},
             "route_change": {k: change[k] for k in ("before", "after", "quote")}}
    route.pop("launch", None)  # a --cli argv names the old model
    route.pop("discovery", None)  # a declared route is no trial and has no discovery fallback
    with_override = {**loads(d.get("override_json"), {}), "by": "user", "declared": True, "triple": triple,
                     "route_changed_from": change["before"], "route_changed_at": now_iso()}
    with_override.pop("cli", None)
    con.execute("UPDATE dispatches SET triple=?, invocation_model_id=?, model=?, effort=?, selection_reason=?, "
                "route_json=?, override_json=? WHERE id=?",
                (triple, cand.get("invocation_model_id"), cand.get("invocation_model_id"), cand.get("effort"), reason,
                 dumps(route), dumps(with_override), d["id"]))


def _recheck_review(con, run: dict, tid: str, cand: dict) -> list[str]:
    """A reviewer pinned to the producer's new model family is no longer
    independent: drop the pin so the pending review routes again."""
    task = state.get_task(con, run["id"], tid)
    pin = task.get("review_override") or {}
    if not pin.get("as"):
        return []
    family = candidates.model_family(cand.get("model_id"))
    if candidates.model_family(candidates.parse_route_override(pin["as"])["model_id"]) != family:
        return [f"{tid} pinned reviewer {pin['as']} stays independent of {family}"]
    state.update_task(con, run["id"], tid, review_override=None)
    state.emit(con, run, "review.rerouted", f"{tid} pinned reviewer {pin['as']} shares the producer's {family} family; "
               "the pending review routes again", task_id=tid, payload={"dropped": pin, "family": family})
    return [f"{tid} pinned reviewer {pin['as']} now shares the producer's {family} family; the pending review "
            "routes again (pin dropped)"]


def _relaunch(con, run: dict, task: dict, d: dict, cand: dict) -> tuple[str, str]:
    """Hand the task to a new dispatch on the new route. Caller holds the tx and
    ends `d` afterwards (the task no longer points at it, so its end relaunches nothing)."""
    from office import rerun
    resume = _resume_spec(d, cand)
    decision = {**state.get_dispatch(con, d["id"])["route"], "status": "selected",
                "selected": routing.candidate_id(cand)}
    extra = {"resume": resume} if resume else None
    new = dispatch.request_launch(con, run, task["id"], role="executor", decision=decision, extra=extra,
                                  base=d.get("base_commit"), replaces=d["id"])
    if resume:
        rerun._set_resumed_from(con, new, d["id"])
        return new, f"resuming session {resume['session_id']}"
    return new, "fresh session (worktree preserved)"


def _resume_spec(d: dict, cand: dict) -> dict | None:
    session = d.get("session_id")
    adapter = adapters.load_all().get(d.get("adapter_id") or d.get("harness") or "")
    if not (session and adapter and dispatch.herdr_usable()):
        return None
    argv = adapters.resume_argv(adapter, "worker", session_id=session, model=cand.get("invocation_model_id") or "",
                                effort=cand.get("effort") or "", cwd=Path(d.get("worktree") or "."))
    if argv is None:
        return None
    return {"parent": d["id"], "session_id": session, "argv": argv[0], "herdr_kind": argv[1],
            "findings": f"your route changed to {routing.candidate_id(cand)}; continue the task"}
