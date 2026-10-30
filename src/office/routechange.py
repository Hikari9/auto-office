"""`office amend route`: re-record a live dispatch's producer route (#301).

The user switched the model in a running pane, or wants it switched. Without
`--restart` only the record changes: the dispatch keeps its lease, session and
harness, and receipts plus the review independence check name the model that
actually works. With `--restart` the new route is recorded first, then the
current agent is interrupted and the task relaunched on the same harness with
the new model/effort, resuming its session where the harness can.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from office import adapters, candidates, db, dispatch, jobs, routing, scoring, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, loads, now_iso, parse_iso

USAGE = 'office amend route <task|dispatch> --as <harness>/<model>[@effort] --quote "<user\'s words>" [--restart]'
LIVE = ("launching", "running")
PENDING = ("planned", "queued", "changes_required")  # no live agent: a route change declares the route the next dispatch/rerun runs
DECLARABLE = (*PENDING, "accepted")


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
    ceiling = dispatch.enforce_effort_ceiling(task, as_route, quote, flag="amend route --as", effort=effort)
    cand = candidates.declared_candidate(harness, want["model_id"], effort, repo_root=run.get("repo_root"),
                                         run_id=run["id"])
    after = routing.candidate_id(cand)
    before = d.get("triple")
    if after == before:
        raise Refused("route-unchanged", f"{d['id']} already records {before}", scope=tid)
    change = {"before": before, "after": after, "quote": quote, "restart": restart, "dispatch_id": d["id"],
              "ceiling": ceiling}
    extra = {"quote": quote, "restart": restart}
    lines = [f"{tid} {d['id']} route {before} -> {after} (recorded; lease and session kept)"]
    with db.transaction(con):
        cur = state.get_dispatch(con, d["id"])
        if cur.get("ended_at") or cur["status"] not in LIVE or _submitted(con, cur):
            raise Refused("state-changed", f"{d['id']} changed while the route was being recorded; re-run", scope=tid,
                          next_step=USAGE)
        _record(con, cur, cand, change)
        dispatch.abandon_trial(con, cur["id"], f"the user declared {after}: no longer a discovery trial")
        state.update_task(con, run["id"], tid, route_json=dumps(_declared_payload(cand, before, quote, ceiling)))
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


def _declared_payload(cand: dict, before: str | None, quote: str, ceiling: dict | None = None) -> dict:
    """The task's recorded route after a deliberate change: dispatch and every
    relaunch follow it (`declared`), with no fallback behind it."""
    triple = routing.candidate_id(cand)
    return {"candidate": cand, "declared": True, "override": True, "route_source": "declared",
            **({"override_ceiling": ceiling} if ceiling else {}),
            "route_authority": {"actor": "user", "quote": quote, "reason": f'user route change: "{quote}"'},
            "selection_disclosure": {"triple": triple, "override": True,
                                     "reason": f"declared route change ({before or 'unrecorded'} -> {triple})"},
            "route_change": {"before": before, "after": triple, "quote": quote}}


def _pending_task(con, run: dict, target: str) -> dict | None:
    """The executor task `target` names when it has no agent yet (planned, or
    queued behind a stack): its route is declared, not re-recorded."""
    if target[:1] in "dD" and len(target) > 2:
        return None
    task = state.get_task(con, run["id"], target.upper())
    if task is None or task["role"] == "planner" or task["status"] not in DECLARABLE:
        return None
    if task.get("current_dispatch_id") and task["status"] != "accepted":
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
        plan = dispatch._planned_slate(con, run, tid)
        before = (plan or {}).get("primary")
    harness = want["harness"] or ((rec.get("candidate") or {}).get("harness") if rec else None)
    if not harness or not want["model_id"]:
        raise Usage("invalid-route", f"--as {as_route!r}: expected <harness>/<model>[@effort]", next_step=USAGE,
                    exit_code=2)
    effort = want["effort"] or (rec.get("candidate") or {}).get("effort")
    _require_known(harness, want["model_id"], effort)
    ceiling = dispatch.enforce_effort_ceiling(task, as_route, quote, flag="amend route --as", effort=effort)
    cand = candidates.declared_candidate(harness, want["model_id"], effort, repo_root=run.get("repo_root"),
                                         run_id=run["id"])
    after = routing.candidate_id(cand)
    if before and scoring.normalize_triple(after) == scoring.normalize_triple(before):
        raise Refused("route-unchanged", f"{tid} already runs {before}", scope=tid)
    with db.transaction(con):
        cur = state.get_task(con, run["id"], tid)
        if cur["status"] != task["status"] or cur.get("current_dispatch_id") != task.get("current_dispatch_id"):
            raise Refused("state-changed", f"{tid} changed while the route was being recorded; re-run", scope=tid,
                          next_step=USAGE)
        state.update_task(con, run["id"], tid, route_json=dumps(_declared_payload(cand, before, quote, ceiling)))
        state.record_route_change(con, run, tid, before, after, kind="reroute", actor="user",
                                  reason=f"user route change: \"{quote.strip()}\"", extra={"quote": quote, "pending": True})
    if task["status"] == "accepted":
        return Result(lines=[f"{tid} route {before or 'unrecorded'} -> {after} (declared for the next reopen; "
                             "accepted producer evidence kept)"], next="the next amendment or review that reopens this task uses it")
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
                    next_step=(f"known {harness} models: {', '.join(models) or 'none'}; "
                               "add a catalog row: office model add <harness>/<model> "
                               "--effort <efforts>; names reported by <harness> models "
                               "need a catalog row"), exit_code=2)
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
             "route_authority": {"actor": "user", "quote": change["quote"], "reason": reason},
             "route_change": {k: change[k] for k in ("before", "after", "quote")},
             **({"override_ceiling": change["ceiling"]} if change.get("ceiling") else {})}
    route.pop("launch", None)  # a --cli argv names the old model
    route.pop("discovery", None)  # a declared route is no trial and has no discovery fallback
    with_override = {**loads(d.get("override_json"), {}), "by": "user", "declared": True, "triple": triple,
                     "quote": change["quote"],
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


# ------------------------------------------------------------------ route available again (#921)

RECOVERED_KIND = "route.recovered"
# Evidence that a run fell back from or avoided a route: a runtime failure (its demotion) and a review that left its route.
_EVIDENCE_SQL = ("SELECT seq, kind, task_id, dispatch_id, created_at, payload_json FROM events WHERE run_id=? AND "
                 "(kind='route.failed' OR (kind='route.changed' AND json_extract(payload_json,'$.notice')='review')) "
                 "ORDER BY seq")
# A candidate id, `harness@major/model_id@effort` (routing.candidate_id).
_CANDIDATE = re.compile(r"(?P<harness>[^@/]+)@[^/]*/(?P<model>[^@]+)@(?P<effort>.+)\Z")


def _route_of(row, payload: dict) -> str | None:
    """The route triple a piece of evidence is about: the failed route, or the route a review left."""
    return payload.get("route") if row["kind"] == "route.failed" else payload.get("before")


def _invocation_model(con, run_id: str, route: str, harness: str, model_id: str, effort: str) -> str:
    """The invocation model a candidate ran as. Cooldowns and probes key on it, and it differs from the model id for
    aliased routes (sonnet runs as claude-sonnet-5). A dispatch of the route records it. A route never dispatched (a
    skipped primary) has no dispatch row, so the shipped catalog resolves the alias instead."""
    row = con.execute("SELECT invocation_model_id FROM dispatches WHERE run_id=? AND triple=? "
                      "AND invocation_model_id IS NOT NULL ORDER BY started_at DESC LIMIT 1", (run_id, route)).fetchone()
    if row:
        return row[0]
    from office import route_probe  # imported here: route_probe imports candidates, which imports this package's routing
    catalog = route_probe.catalog_row({"harness": harness, "model_id": model_id, "effort": effort})
    return (catalog or {}).get("invocation_model_id") or model_id


def _route_parts(con, run_id: str, row, payload: dict) -> tuple[str | None, str | None, str | None]:
    """(harness, invocation model, effort) of the route the evidence names. A runtime failure carries its invocation
    model; a review change carries only a candidate id, resolved through the route's dispatches."""
    if row["kind"] == "route.failed":
        return payload.get("harness"), payload.get("model"), payload.get("effort")
    route = _route_of(row, payload) or ""
    match = _CANDIDATE.fullmatch(route)
    if not match:
        return None, None, None
    harness = match["harness"]
    effort = None if match["effort"] == "None" else match["effort"]  # candidate_id writes a missing effort as "None"
    return harness, _invocation_model(con, run_id, route, harness, match["model"], effort), effort


def _role(con, row, payload: dict) -> str | None:
    """The role whose routing decided the evidence's route: the review stage, or the failed dispatch's role."""
    if row["kind"] == "route.changed":
        return payload.get("stage")
    d = con.execute("SELECT role FROM dispatches WHERE id=?", (row["dispatch_id"],)).fetchone()
    return d[0] if d else None


def _available_basis(con, run_id: str, row, payload: dict, now: datetime) -> tuple[str, datetime] | None:
    """(why, the instant the route became available) when the evidence's route is available again, else None. A route
    the user declared is the user's choice and is never recovered here. Routing rejects a route while any of its
    cooldown keys is live, so none may be. A runtime failure is then recovered: its demotion lapsed, at the latest
    expiry. A review change set no cooldown, so it needs a re-probe that passed after it, and that pass is the instant."""
    if payload.get("declared") or payload.get("actor") == "user":  # the user's declaration or re-pin is the decision
        return None
    harness, model, effort = _route_parts(con, run_id, row, payload)
    if not model:
        return None
    keys = (*routing._demotion_keys({"harness": harness, "invocation_model_id": model, "effort": effort}),
            routing.cooldown_key(harness, model))
    expiries = [parse_iso(expires) for (expires,) in con.execute(
        f"SELECT expires_at FROM route_cooldowns WHERE route IN ({','.join('?' * len(keys))})", keys).fetchall()]
    if any(expiry > now for expiry in expiries):
        return None
    if row["kind"] == "route.failed":
        # The cooldown rows are the lapse. A successful dispatch deletes them, so then the demotion's own deadline decides.
        if expiries:
            return "demotion lapsed", max(expiries)
        deadline = parse_iso(payload["demoted_until"]) if payload.get("demoted_until") else parse_iso(row["created_at"])
        return None if deadline > now else ("demotion lapsed", deadline)
    passed = con.execute("SELECT probed_at FROM route_probes WHERE COALESCE(harness,'')=? "
                         "AND COALESCE(invocation_model_id,'')=? AND COALESCE(effort,'')=? AND result='pass' "
                         "AND probed_at IS NOT NULL ORDER BY probed_at DESC LIMIT 1",
                         (harness or "", model, effort or "")).fetchone()
    if not passed or parse_iso(passed[0]) <= parse_iso(row["created_at"]):
        return None
    return "re-probe passed", max([parse_iso(passed[0])] + expiries)


def _available_text(route: str, basis: str) -> str:
    return f"preferred route {route} is available again ({basis}); the next review, rerun or dispatch ranks it normally"


def _recorded(con, run_id: str, episode: int) -> bool:
    return con.execute("SELECT 1 FROM events WHERE run_id=? AND kind=? AND json_extract(payload_json,'$.episode')=?",
                       (run_id, RECOVERED_KIND, episode)).fetchone() is not None


def _latest_episodes(con, run_id: str) -> dict[str, tuple]:
    """route -> (newest evidence row, its payload). A route is judged by its newest episode only."""
    latest: dict[str, tuple] = {}
    for row in con.execute(_EVIDENCE_SQL, (run_id,)).fetchall():
        payload = loads(row["payload_json"], {})
        if route := _route_of(row, payload):
            latest[route] = (row, payload)
    return latest


def _recoverable(con, run_id: str, now: datetime) -> list[tuple]:
    """(route, evidence row, payload, (why, instant)) for each route whose newest episode is available and unrecorded.
    Read-only."""
    found = []
    for route, (row, payload) in _latest_episodes(con, run_id).items():
        if _recorded(con, run_id, row["seq"]):
            continue
        if (basis := _available_basis(con, run_id, row, payload, now)) is not None:
            found.append((route, row, payload, basis))
    return found


def sweep_recovered(con, run: dict, *, now: datetime | None = None) -> list[int]:
    """Record one `route.recovered` per route a non-terminal run fell back from or avoided that is available again.
    Keyed by the evidence's seq, so each episode is recorded once. Only events are written: no dispatch, task or
    route changes, and the next review, rerun or dispatch ranks the route under the normal policy. Returns new seqs."""
    if state.is_terminal(run):
        return []
    now = now or datetime.now(timezone.utc)
    if not _recoverable(con, run["id"], now):  # read-only: a sweep with nothing to record takes no writer lock
        return []
    seqs = []
    with db.transaction(con):  # judged again under the writer lock, so a concurrent sweep cannot record the same episode
        for route, row, payload, (basis, at) in _recoverable(con, run["id"], now):
            harness, model, effort = _route_parts(con, run["id"], row, payload)
            seqs.append(state.emit(
                con, run, RECOVERED_KIND, _available_text(route, basis), task_id=row["task_id"],
                payload={"route": route, "harness": harness, "model": model, "effort": effort,
                         "role": _role(con, row, payload), "basis": basis, "episode": row["seq"],
                         "evidence": row["kind"], "available_at": at.isoformat(timespec="microseconds")}))
    return seqs


def _considered_since(con, run_id: str, at: str, route: str, role: str | None) -> bool:
    """Whether routing since `at` (the instant the route became available) used or passed over the route: a dispatch
    or review gate on the route itself, a review that left it, or a dispatch of the same role that routing chose for
    (not pinned to a declared route, which skips the ranking)."""
    return con.execute(
        "SELECT 1 FROM dispatches WHERE run_id=? AND started_at>? AND (triple=? OR (role IS ? AND override_json IS NULL)) "
        "UNION ALL SELECT 1 FROM gates WHERE run_id=? AND created_at>? AND route=? "
        "UNION ALL SELECT 1 FROM events WHERE run_id=? AND created_at>? AND kind='route.changed' "
        "AND json_extract(payload_json,'$.notice')='review' AND json_extract(payload_json,'$.before')=? LIMIT 1",
        (run_id, at, route, role, run_id, at, route, run_id, at, route)).fetchone() is not None


def recovery_lines(con, run: dict) -> list[str]:
    """`preferred route X is available again` for each recovered route that routing has not used or passed over since
    it became available. A route's newest recovery alone decides its line."""
    if state.is_terminal(run):
        return []
    lines, seen = [], set()
    for row in con.execute("SELECT created_at, payload_json FROM events WHERE run_id=? AND kind=? ORDER BY seq DESC",
                           (run["id"], RECOVERED_KIND)).fetchall():
        payload = loads(row["payload_json"], {})
        route = payload.get("route")
        if route in seen:
            continue
        seen.add(route)
        since = payload.get("available_at") or row["created_at"]
        if not _considered_since(con, run["id"], since, route, payload.get("role")):
            lines.append(_available_text(route, payload.get("basis")))
    return lines
