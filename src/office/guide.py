"""Compact status and the next legal action.

Every command ends with one `next:` line derived from canonical state, so an
agent never needs an `office next` command or the Office source to proceed.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import re

import os
from pathlib import Path

from office import amend, contract, paths, planpath, plans, route_learning, state
from office.result import Result
from office.util import atomic_write_text, short, sha256_obj

ORCH_EVENT_KINDS = None  # every orchestrator-audience event

IDLE_STALL_DEFAULT_S = 60.0
BUG_HINT_LIMIT = 5


def idle_stall_s() -> float:
    """Seconds an executor's agent may sit idle before it is a stall; env override in seconds."""
    try:
        return float(os.environ.get("OFFICE_EXECUTOR_IDLE_STALL_S", IDLE_STALL_DEFAULT_S))
    except ValueError:
        return IDLE_STALL_DEFAULT_S


def _counts(tasks: list[dict]) -> dict:
    out: dict[str, list[str]] = {}
    for t in tasks:
        out.setdefault(t["status"], []).append(t["id"])
    return out


def unaccepted_dependencies(task: dict, by_id: dict) -> list[str]:
    """`depends:` is an acceptance barrier (#405): every dependency of `task`
    that is not accepted yet, as "T1 submitted". A submitted, checking,
    changes-required or blocked dependency still holds its dependents. The one
    readiness rule `office status`, `resume` and `wait` all print `next:` from.
    (Building on an unaccepted task stays an explicit choice: `office dispatch
    T1 T2` stacks T2 to start when T1 is accepted.)"""
    out = []
    for dep in task["depends"]:
        d = by_id.get(dep)
        if not d or d["status"] != "accepted":
            out.append(f"{dep} {d['status'] if d else 'unknown'}")
    return out


def ready_tasks(con, run: dict) -> list[str]:
    tasks = state.tasks(con, run["id"])
    by_id = {t["id"]: t for t in tasks}
    return [t["id"] for t in tasks if t["status"] == "planned" and not unaccepted_dependencies(t, by_id)]


def trial_notes(con, tasks: list[dict]) -> list[str]:
    """One note per task whose current dispatch is a live discovery trial (#494): the route is
    named as a trial, with the known-working route it falls back to."""
    notes = []
    for t in tasks:
        trial = route_learning.live_trial(con, t.get("current_dispatch_id"))
        if trial:
            notes.append(f"{t['id']} runs a discovery trial of {trial['route']}, fallback {trial['fallback'] or 'none'}")
    return notes


def held_by_dependencies(con, run: dict) -> list[str]:
    """Planned tasks a dependency still holds, as "T2 waits for T1 (submitted)"."""
    tasks = state.tasks(con, run["id"])
    by_id = {t["id"]: t for t in tasks}
    out = []
    for t in tasks:
        deps = unaccepted_dependencies(t, by_id) if t["status"] == "planned" else []
        if deps:
            out.append(f"{t['id']} waits for {', '.join(d.split()[0] for d in deps)} to be accepted "
                       f"({'; '.join(deps)})")
    return out


def next_action(con, run: dict) -> str:
    run = state.get_run(con, run["id"])
    if state.is_terminal(run):
        return "none; the run is " + run["phase"]
    if not run["plan_version"]:
        if run.get("planner_mode") == "dedicated":
            return "no action; the planner is working (office status)"
        return f"write {planpath.display(run)} (office submit --help shows the format), then office submit"
    plan = state.current_plan(con, run["id"])
    from office import planfile
    if planfile.parse(plan["body"]).questions and not plans._answered(con, run):
        return 'ask the user the plan questions (native question tool), then office amend plan --contract -- "<answers>"'
    rs = plans.review_state(con, run)
    if contract.is_convergence(run):
        nxt = _plan_next_convergence(con, run, rs)
        if nxt:
            return nxt
    elif rs["required"] and not rs["ended"] and rs["first_verdict"] is None:
        return "no action; plan review is running"
    if rs["first_verdict"] == "CHANGES_REQUIRED" and run["plan_version"] <= (rs["first_version"] or 0) and not rs["ended"]:
        return f'amend the plan: office amend plan -- "<changes>" (edit {planpath.rel(run)} for task changes); safe work may launch right after'
    live_ids = {t["id"] for t in state.tasks(con, run["id"]) if t["status"] != "cancelled"}
    for d in rs["open_defects"]:
        named = set(re.findall(r"\bT\d+\b", d.get("location") or ""))
        if named and not (named & live_ids):
            continue  # every task it names was cancelled
        code = d["code"]
        if rs["pending"]:
            return f"no action; plan re-review is running (open defect {code})"
        redirect = (f'office amend plan --contract --redirect {code} --root-cause "<requirement or assumption>" '
                    f'--quote "<user\'s words>" [--requirement "<new requirement>"] [--reviewer same|fresh] -- "<fix>"')
        waive = f'if the user judges it wrong: office approve waive {code} --quote "<words>"'
        head = f"plan defect {code} blocks {d.get('location') or 'the plan'}; "
        if d["category"] == "brief":
            return head + ('request the fix: office amend plan --contract -- "<fix>"' if run.get("planner_mode") == "dedicated"
                           else f"fix {planpath.rel(run)}, then office amend plan --contract -- \"<fix>\"")
        if run.get("planner_mode") == "dedicated":
            return head + (f'have the planner trace it to its requirement or assumption: office amend plan --contract -- '
                           f'"resolve {code}". If the planner asks the user (plan Questions), ask them (native question '
                           f"tool), then {redirect}; {waive}")
        return head + (f"trace it to the requirement or assumption behind it. Plan-only cause: fix {planpath.rel(run)}, "
                       f'then office amend plan --contract -- "<fix>". Requirement or assumption cause: ask the user '
                       f"(native question tool) how to redirect it, revise {planpath.rel(run)}, then {redirect}; {waive}")
    if not state.active_authorization(con, run, "plan"):
        return f'ask the user (native question tool) for authorization of r{run["requirements_version"]}, then office approve plan --quote "<user\'s words>"'
    for e in run.get("envelope") or []:
        if e.get("needs_authorization"):
            return f'new authority entry {e["id"]} ({e["action"]}) needs authorization: ask the user (native question tool), then office approve {e["id"]} --quote "<words>"'
    tasks = state.tasks(con, run["id"])
    c = _counts(tasks)
    stranded = _stranded_amendments(con, run)
    if stranded:
        from office import rerun
        a = stranded[0]
        return (f"{a['task_id']} awaits amendment {a['amendment_id']} ack, but its executor {a['dispatch_id']} "
                f"has ended; {rerun.recovery_hint(con, run, a['task_id'], fresh=True)} to deliver/ack it")
    for status in ("blocked", "paused"):
        if c.get(status):
            tid = c[status][0]
            t = next(x for x in tasks if x["id"] == tid)
            from office import gates as gates_review
            block = gates_review.unavailable_review_block(con, run, t)
            if block:
                return (f"{tid} code review of {block['revision_id']} returned no verdict "
                        f"({block['verdict'] or block.get('review_status')}; runtime status, no round spent). Re-run only the "
                        f"review on a fallback reviewer: office review {tid} [--as <harness>/<model>[@effort]]; or review it "
                        f"yourself as the orchestrator (recorded degraded, non-independent; refused for the session that "
                        f"produced it): office review {tid} --report <file>; office resume retries the same reviewer chain. "
                        f"A waiver is the user's last resort: office approve waive {tid}:code --quote \"<words>\"")
            return f"resolve {tid} ({t.get('pause_reason') or status}); office inspect task {tid}"
    from office import gates as gates_mod
    if contract.is_convergence(run):
        # A lane RECHECK routes one consolidated repair set: name every owner at once.
        from office import convergence
        nxt = convergence.next_action(con, run)
        if nxt:
            return nxt
    # A rerun is only actionable if its dependency barrier and scope lease will
    # admit it. Otherwise `office wait` would wake to suggest a refused command.
    from office import dispatch, rerun
    by_id = {t["id"]: t for t in tasks}
    held_repairs = []
    for t in tasks:
        if t["status"] == "changes_required" and not gates_mod.worker_live(con, t.get("current_dispatch_id")):
            dependencies = unaccepted_dependencies(t, by_id)
            holder = dispatch.scope_holder(con, run, t)
            if dependencies or holder:
                why = "; ".join(x for x in (
                    f"unaccepted dependencies: {', '.join(dependencies)}" if dependencies else "",
                    f"scope held by {holder}'s live session" if holder else "",
                ) if x)
                held_repairs.append(f"{t['id']} waits for {why}")
                continue
            # Findings wait for the orchestrator's choice (R8); nothing relaunches on its own.
            if (t.get("pause_reason") or "").endswith("the session ended without submitting"):
                return (f"{t['id']} {t['pause_reason']}: {rerun.recovery_hint(con, run, t['id'])} "
                        "(resubmit on the amended contract)")
            if (t.get("pause_reason") or "").startswith("restack:"):
                return f"{t['id']} {t['pause_reason']}: {rerun.recovery_hint(con, run, t['id'])}"
            awaiting_ack = con.execute(
                "SELECT amendment_id FROM deliveries WHERE run_id=? AND task_id=? "
                "AND status IN ('queued','delivered') ORDER BY created_at LIMIT 1",
                (run["id"], t["id"])).fetchone()
            if awaiting_ack:
                return (f"{t['id']} awaits amendment {awaiting_ack['amendment_id']} ack; "
                        f"{rerun.recovery_hint(con, run, t['id'])} to deliver/ack it")
            outstanding = con.execute("SELECT 1 FROM findings WHERE run_id=? AND task_id=? AND "
                                      + contract.TASK_WORK_FINDINGS + " LIMIT 1",
                                      (run["id"], t["id"])).fetchone()
            if not outstanding:
                return (f"{t['id']} requires continuation (no open review findings); "
                        f"office inspect task {t['id']}, then {rerun.recovery_hint(con, run, t['id'])}")
            return f"findings on {t['id']} wait for you: {rerun.recovery_hint(con, run, t['id'])}"
    if held_repairs:
        return "no action; " + "; ".join(held_repairs) + "; office wait for the holder or dependency to finish"
    for t in tasks:
        if t["status"] == "changes_required":
            # Its live worker has the findings; the repair path stays the next action.
            return (f"no action; {t['id']}'s worker is fixing its findings (if it stops: "
                    f"office rerun {t['id']} --resume | --fresh)")
    for t in tasks:
        if t["status"] in ("submitted", "accepted"):
            stale = gates_mod.stale_dependency(con, run, t)
            if stale:
                return f"{t['id']} {stale}"
    ready = ready_tasks(con, run)
    if ready:
        return "choose execution strategy; office dispatch " + " ".join(ready) + (" --parallel" if len(ready) > 1 else "")
    live = [t for t in tasks if t["status"] not in ("accepted", "cancelled", "planned")]
    if live:
        notes = [*trial_notes(con, live), *held_by_dependencies(con, run)[:1]]
        return "exceptions only; office status" + (f" ({'; '.join(notes)})" if notes else "")
    if contract.is_convergence(run):
        from office import convergence
        nxt = convergence.next_action(con, run, dispositions=True)
        if nxt:
            return nxt
    from office import integration
    integ = integration.status(con, run)
    if integ["required"] and integ["status"] not in ("accepted",):
        if integ["status"] == "conflict":
            return f"integration conflict: {integ.get('detail', '')}; office inspect run"
        if integ["status"] in ("blocked", "unavailable"):
            return (f"integration {integ['status']}: {integ.get('detail', '')}; fix the cause, then office resume "
                    "to retry integration")
        if integ["status"] == "converging":
            return f"no action; lane convergence is running ({integ.get('detail', '')})"
        return "no action; integration verification is running"
    if ((run.get("landing") or {}).get("prs") or {}).get("enabled"):
        from office import land
        return f"office land (end state: {land.end_state(con, run)['mode']})"
    branch = integ.get("branch")
    from office import closeout
    return (f"land it: push {branch} and open a PR (merge to main stays with the user), "
            f"{closeout.DOCS_STEP}office close --handoff <pr-url>"
            if branch else "office close --handoff <ref>")


def _plan_next_convergence(con, run: dict, rs: dict) -> str | None:
    """The plan-review step under the convergence contract (#337, #418), or None."""
    if not rs["required"]:
        return None
    st = rs["status"]
    dedicated = run.get("planner_mode") == "dedicated"
    revise = ('have the planner revise it: office amend plan --contract -- "<the findings>"' if dedicated
              else f'revise {planpath.rel(run)}, then office amend plan --contract -- "<what changed>"')
    owned = [f for f in rs["blocking"] if f["code"] != "INTAKE_GAP"]
    if st == plans.OWNED and owned:
        last = (rs.get("history") or [{}])[-1]
        return (f"plan review closed unapproved after {last.get('rounds')}/{last.get('max_rounds')} rounds; you own "
                f"{', '.join(f['code'] for f in owned[:4])} (office inspect run): fix each in the plan ({revise}; it "
                "is not re-reviewed) or accept it, then office disposition plan:<code> fixed|dismissed|follow-up -- "
                '"<rationale>". A fix that changes requirements or authority still needs the user')
    if rs["ended"]:
        return None
    if st == "unavailable":
        return ("plan review is UNAVAILABLE (runtime status, not a verdict; no round spent): office resume retries "
                'the reviewer chain, or the user may waive: office approve waive plan-review --quote "<words>"')
    if st == "attention":
        return ("the plan reviewer left no readable reply (INVALID_RESULT, no round spent): re-prompt it in its pane "
                "or office resume")
    if rs["pending"] or rs["first_verdict"] is None:
        return "no action; plan review is running"
    if st == "intake_gap":
        gap = rs.get("intake_gap") or {}
        return (f"plan INTAKE_GAP: ask the user (native question tool): {gap.get('decision')} (affects "
                f"{gap.get('affects')}); record their answer (office amend requirements --quote ...), then {revise}")
    if st in ("recheck", "pending"):
        codes = ", ".join(f["code"] for f in rs["blocking"][:4])
        held = plans.held_tasks(con, run)
        ready = [t for t in ready_tasks(con, run) if t not in held]
        from office import gates as gates_mod
        who = gates_mod.resume_blocker(con, run, state.get_dispatch(con, plans.recheck_reviewer(con, run) or ""), "reviewer")
        return (f"plan RECHECK ({codes}): {revise}; "
                + ("the same reviewer reviews it" if who is None else f"a fresh reviewer on the same route reviews it "
                   f"(the last reviewer cannot be resumed: {who})")
                + (f"; unaffected work may start: office dispatch {' '.join(ready)}" if ready else ""))
    return None


VERDICT_LINES = 3  # closed verdicts one status shows and marks read (#919)


def status(con, run: dict, *, resumed: bool = False, verbose: bool = False,
           probe_panes: bool = False) -> Result:
    worker = os.environ.get("OFFICE_DISPATCH_ID")
    if worker:
        return worker_status(con, run, worker)
    amend.confirm_launch_deliveries(con, run)
    run = state.get_run(con, run["id"])
    tasks = state.tasks(con, run["id"])
    c = _counts(tasks)
    res = Result()
    res.add(f"{short(run['id'])} {run['phase']} | req r{run['requirements_version']} | plan p{run['plan_version']}")
    from office import upgrade
    stale = upgrade.notice(run)
    if stale:
        res.add(stale)
    if not state.is_terminal(run):
        from office import config as cfg
        drift = cfg.config_drift(run)
        if drift:
            res.add(drift)
        routing_drift = cfg.routing_inputs_drift(run)
        if routing_drift:
            res.add(routing_drift)
    if tasks:
        parts = [f"accepted {len(c.get('accepted', []))}/{len([t for t in tasks if t['status'] != 'cancelled'])}"]
        # `live` is a task with a live or launching session. A task whose session ended is
        # `submitted` (its revision waits on gates, its lane or its dependencies) or `idle`
        # (it waits for the orchestrator: next: names the rerun), never live (#398).
        from office import gates as gates_mod
        open_ids = [t["id"] for t in tasks if t["status"] in ("running", "launching", "submitted", "changes_required")]
        live_ids = {i for i in open_ids if gates_mod.live_task_session(con, run["id"], i)}
        groups = {"live": [i for i in open_ids if i in live_ids],
                  "submitted": [i for i in c.get("submitted", []) if i not in live_ids],
                  "idle": [i for i in open_ids if i not in live_ids and i not in c.get("submitted", [])]}
        for label in ("live", "submitted", "idle", "queued", "paused", "blocked"):
            ids = groups.get(label, c.get(label, []))
            if ids:
                parts.append(f"{label} {','.join(ids)}")
        res.add(" | ".join(parts))
    routes = effective_routes(con, run, tasks)
    if routes:
        res.add("routes: " + ", ".join(f"{tid} {r}" for tid, r in routes.items()))
    if not state.is_terminal(run):
        from office import gates as review_gates
        for line in review_gates.review_route_lines(con, run["id"], limit=3):
            res.add(line)
        from office import routechange
        routechange.sweep_recovered(con, run)
        for line in routechange.recovery_lines(con, run):
            res.add(line)
    from office import risk as risk_mod
    rline = risk_mod.line(run)
    if rline and (risk_mod.classification(run.get("risk")) == risk_mod.UNKNOWN or (run.get("gates") or {}).get("lightweight")):
        res.add(rline)
    elif rline:
        res.verbose.append(rline)
    rs = plans.review_state(con, run)
    if rs["required"]:
        pr = "plan review " + ("closed (" + (rs["ended_reason"] or "") + ")" if rs["ended"] else
                               (_plan_review_live(con, run) if rs["pending"] else (rs["last_verdict"] or "pending")))
        if rs.get("lifecycle") and not rs["ended"]:
            pr += f" | {rs['lifecycle']} review, round {rs['rounds_used']}/{rs['max_rounds']}"
        res.add(pr)
    for t in tasks:
        if t["status"] in ("paused", "blocked"):
            res.add(f"blocker: {t['id']} {t.get('pause_reason') or t['status']}")
        elif t["status"] in ("running", "launching", "submitted", "changes_required"):
            waiting = _waiting_on(con, run, t)
            if waiting:
                res.add(f"{t['id']} waiting: {waiting}")
    for t in tasks:
        if t["status"] in ("running", "launching", "submitted", "changes_required"):
            note = _quota_unknown_note(con, t)
            if note:
                res.add(note)
    from office import dispatch as dispatch_mod
    for line in dispatch_mod.headless_fallbacks(con, run):
        res.add(line)
    replies = [] if state.is_terminal(run) else dispatch_mod.reply_pending(con, run)
    hard = dispatch_mod.reply_hard_bound()
    for r in replies:
        if r["quiet"] < hard:
            res.add(f"{r['id']} reply is written; Office ingests it when it settles, no action needed")
    convergence = contract.is_convergence(run)
    stuck_reply = next((n for n in (_stuck_reply_next(r, convergence) for r in replies if r["quiet"] >= hard) if n), None)
    from office import root_wake
    for line in root_wake.lines(con, run):
        res.add(line)
    from office import ctx_alert
    ctx_alert.notice(con, run)
    from office import questions
    for q in questions.recorded(con, run):
        res.add(f"question: {q}")
    for line in _amendment_stalls(con, run):
        res.add(f"blocker: {line}")
    episodes, verdict_lines = [], []
    if not state.is_terminal(run):
        from office import db, watchdog
        verdicts = watchdog.unread_verdicts(con, run)
        episodes = watchdog.review_episodes(con, run, probe=probe_panes, verdicts=verdicts)
        # A verdict is read only once its line is printed: an episode line, or one of at most a few verdict lines.
        # Session hooks cut the output by bytes, so a longer list would lose lines that were marked read.
        in_episode = {ep["episode"] for ep in episodes if ep["kind"] == "review.closed_unread"}
        pending = [v for v in verdicts if v["gate"]["id"] not in in_episode]
        shown = pending[:VERDICT_LINES]
        verdict_lines = [watchdog.verdict_line(v["gate"]) for v in shown]
        if len(pending) > len(shown):
            verdict_lines.append(f"{len(pending) - len(shown)} more closed verdicts unread; the next office status shows them")
        printed = in_episode | {v["gate"]["id"] for v in shown}
        if episodes or verdicts:
            with db.transaction(con):
                watchdog.record_review_episodes(con, run, episodes)
                watchdog.mark_review_verdicts_read(con, run, [v for v in verdicts if v["gate"]["id"] in printed])
    # Right after the header: a caller that truncates the output (session hooks) never loses a marked-read verdict.
    res.lines[1:1] = [*verdict_lines, *(f"blocker: {ep['text']}" for ep in episodes)]
    if probe_panes:
        # One `herdr agent list` call, made only by `office status` itself: never from hooks
        # (session start) or `office wait`, which scans panes on its own.
        for b in questions.blocked_unrecorded(con, run):
            res.add(f"blocker: {b}")
    events = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=6)
    for e in events:
        res.add(f"· {e['summary']}")
    if events:
        from office import db
        with db.transaction(con):
            state.consume_events(con, run["id"], events)
    # A successful check result is never evidence that the undispatched pytest
    # tiers passed. Keep the omission visible even after its original event was read.
    detail = ((run.get("landing") or {}).get("integration") or {}).get("detail") or ""
    if "FULL SUITE NOT VERIFIED" in detail:
        res.add(f"verification warning: {detail}")
    res.next = next_action(con, run)
    if episodes:
        res.next = episodes[0]["next"]
    elif stuck_reply:
        res.next = stuck_reply
    elif (res.next.startswith(WAITING_ACTIONS) and not state.is_terminal(run) and _work_in_flight(con, run)
          and not root_wake.armed(con, run) and not wait_in_flight(run["id"])):
        # Nothing for the root to do and no one will wake it: it must block on `office wait`.
        res.next = "office wait (work is running and no root wake is armed)"
    res.data = {"run_id": run["id"], "phase": run["phase"], "office_version": run["office_version"],
                "requirements_version": run["requirements_version"], "plan_version": run["plan_version"],
                "tasks": {t["id"]: t["status"] for t in tasks}, "routes": routes, "plan_review": {k: v for k, v in rs.items() if k != "open_defects"},
                "open_defects": [d["code"] for d in rs["open_defects"]], "next": res.next}
    res.data["root_wake"] = root_wake.metrics(con, run)
    if resumed:
        res.verbose.append("resumed: pending jobs and deliveries reconstructed from runs.db")
    return res


LANE_REVIEW_SPEC = {"convergence_review": "convergence", "visual": "visual"}  # gate kind -> `<scope>:<kind>` argument


def _stuck_reply_next(reply: dict, convergence: bool) -> str | None:
    """The next command for a stuck reviewer once its reply stayed unsettled past the hard bound (#918). Where a
    same-revision re-run exists it names that (revoke, then review). A v3.1 plan review and a task visual review have
    none, so they name the manual step and the reply file the orchestrator reads the verdict from. None otherwise."""
    did, kind = reply["id"], reply["kind"]
    if kind == "plan_review" and convergence:
        return f"office revoke {did}, then office review plan --as <harness>/<model> to re-run the plan review"
    if reply["scope"] and kind in LANE_REVIEW_SPEC:
        spec = f"{reply['scope']}:{LANE_REVIEW_SPEC[kind]}"
        return f"office revoke {spec}, then office review {spec} to re-run the review on the same revision"
    if kind == "code_review" and reply["task_id"]:
        return (f"office revoke {did}, then office review {reply['task_id']} "
                "to relaunch the review on the same revision")
    if kind == "plan_review" or (kind == "visual" and reply["task_id"]):
        return f"office revoke {did}, then office resume; the reply is at {reply['path']}"
    return None


def add_bug_hints(con, run: dict, res: Result) -> Result:
    """Attach the run's current `bug?:` lines to a status-shaped result as `data["bug_hints"]`; the CLI prints
    them and `--json` carries them as a `bug_hints` list. Read-only and best effort. A worker is not told:
    it never files an Office defect. The session-start hook calls `status` directly and never gets hints."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        return res
    from office import bughint
    try:
        res.data["bug_hints"] = bughint.lines(bughint.collect(con, run["id"]), limit=BUG_HINT_LIMIT)
    except Exception:  # a hint must never fail the command it rides on
        pass
    return res


def _quota_unknown_note(con, task: dict) -> str | None:
    """A live task whose current dispatch was routed while its harness quota was
    unknown: unknown is scored conservatively, never as unlimited (#450)."""
    did = task.get("current_dispatch_id")
    row = con.execute("SELECT route_json FROM dispatches WHERE id=?", (did,)).fetchone() if did else None
    try:
        unknown = json.loads(row["route_json"] or "{}").get("quota_unknown") if row else None
    except ValueError:
        return None
    if not unknown:
        return None
    return (f"{task['id']} routed on {unknown.get('status', 'unknown')} quota: {unknown.get('harness')}: "
            f"{unknown.get('cause')}")
def effective_routes(con, run: dict, tasks: list[dict]) -> dict[str, str]:
    """Each unfinished task's effective route (#426): the one recorded on the
    task, else the plan's primary it will dispatch on. Shown so a swap is visible."""
    from office import plan_view, routing
    pv = plan_view.load(con, run["id"], run["plan_version"]) if run.get("plan_version") else None
    out = {}
    for t in tasks:
        if t["status"] in ("accepted", "cancelled"):
            continue
        trial = route_learning.live_trial(con, t.get("current_dispatch_id"))
        if trial:
            out[t["id"]] = f"{trial['route']} (trial, fallback {trial['fallback'] or 'none'})"
            continue
        rec = state.recorded_route(t)
        if rec:
            chosen = routing.candidate_id(rec["candidate"])
            preferred = rec.get("preferred_route")
            out[t["id"]] = chosen + (" (declared)" if rec.get("declared") else "") + \
                (f" (preferred {preferred}: {rec.get('selection_reason') or 'fell back'})"
                 if preferred and preferred != chosen else "")
            continue
        plan = (((pv or {}).get("tasks") or {}).get(t["id"]) or {}).get("route_plan") or {}
        if plan.get("primary"):
            out[t["id"]] = f"{plan['primary']} (planned)"
    return out


def _plan_review_live(con, run: dict) -> str:
    """A pending plan-review round, named by the reviewer actually working it."""
    g = [g for g in plans.plan_gates(con, run["id"]) if g["status"] in ("queued", "running")][-1]
    d = con.execute("SELECT id, pane_id FROM dispatches WHERE gate_id=? AND ended_at IS NULL ORDER BY started_at DESC "
                    "LIMIT 1", (g["id"],)).fetchone()
    if d is None:
        return f"queued (p{g['plan_version']} {g['id']}; no reviewer running yet)"
    return f"running (p{g['plan_version']} reviewer {d['id']}" + (f" pane {d['pane_id']})" if d["pane_id"] else ")")


def _snapshot(con, run: dict) -> tuple:
    run = state.get_run(con, run["id"])
    tasks = tuple((t["id"], t["status"], t.get("current_revision_id")) for t in state.tasks(con, run["id"]))
    return run["phase"], run["requirements_version"], run["plan_version"], tasks


def stalls(con, run: dict, since: str = "", acts: dict | None = None, episodes: list[dict] | None = None) -> list[str]:
    """Work Office believes is in progress with nothing left to advance it. `acts` is the pane
    activity `questions.scan` already read, by dispatch id, so no pane is read twice. `episodes` is
    `watchdog.review_episodes` already computed by the caller; it is read here when absent."""
    out = []
    from office import gates as gates_mod
    for g in con.execute("SELECT * FROM gates WHERE run_id=? AND status IN ('queued','running')", (run["id"],)).fetchall():
        if gates_mod.superseded_integration_gate(con, run, g):
            continue
        # Each gate is advanced by its own job or reviewer, not by unrelated work in the run,
        # judged by the ownership truth reclaim uses: a live or unknown owner is no stall (#404).
        owner, detail = gates_mod.owner_state(con, run, g)
        if owner in ("none", "dead"):
            out.append(f"{g['task_id'] or 'plan'} {g['kind']} gate {g['id']} is {g['status']} but {detail}")
    for j in con.execute("SELECT kind, error FROM outbox WHERE run_id=? AND status='failed' AND finished_at > ?",
                         (run["id"], since)).fetchall():
        out.append(f"job {j['kind']} failed: {(j['error'] or '')[:120]}")
    out.extend(_amendment_stalls(con, run))
    out.extend(_idle_executors(con, run, acts))
    from office import watchdog
    if episodes is None:
        episodes = watchdog.review_episodes(con, run, probe=True)
    # Only a running reviewer is a stall (exit 3). Queued and closed-unread episodes are news (exit 0).
    out.extend(ep["text"] for ep in episodes if ep["kind"] == "review.stalled")
    return out


def _stranded_amendments(con, run: dict) -> list[dict]:
    """Unapplied deliveries still bound to the task's ended executor (#718).

    A live nonresumable worker can acknowledge normally. A replacement dispatch
    takes over its own deliveries, so an old ended recipient does not stall it.
    Dependencies and scope holders still own their normal admission barriers:
    their completion can release the deferred amendment without intervention.
    """
    rows = con.execute(
        "SELECT dl.task_id, dl.amendment_id, dl.dispatch_id FROM deliveries dl "
        "JOIN tasks t ON t.run_id=dl.run_id AND t.id=dl.task_id AND t.current_dispatch_id=dl.dispatch_id "
        "JOIN dispatches d ON d.id=dl.dispatch_id AND d.role='executor' "
        "WHERE dl.run_id=? AND dl.status IN ('queued','delivered') AND d.ended_at IS NOT NULL "
        "AND t.status NOT IN ('accepted','cancelled') ORDER BY dl.created_at", (run["id"],)).fetchall()
    from office import dispatch
    by_id = {t["id"]: t for t in state.tasks(con, run["id"])}
    return [dict(row) for row in rows
            if not unaccepted_dependencies(by_id[row["task_id"]], by_id)
            and not dispatch.scope_holder(con, run, by_id[row["task_id"]])]


def _undelivered_amendments(con, run: dict) -> list[dict]:
    """Unacked deliveries bound to the task's live executor for longer than `idle_stall_s()` (#738).

    Age runs from the latest of the delivery's creation, its first delivery and its
    recipient's start: a relaunch rebinds old deliveries to the new dispatch, and that
    dispatch has had no chance to show them yet."""
    from datetime import timezone
    from office.util import now_iso, parse_iso

    def moment(value: str):
        at = parse_iso(value)
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    rows = con.execute(
        "SELECT dl.task_id, dl.amendment_id, dl.dispatch_id, dl.created_at, dl.delivered_at, d.started_at, d.launcher "
        "FROM deliveries dl "
        "JOIN tasks t ON t.run_id=dl.run_id AND t.id=dl.task_id AND t.current_dispatch_id=dl.dispatch_id "
        "JOIN dispatches d ON d.id=dl.dispatch_id AND d.role='executor' "
        "WHERE dl.run_id=? AND dl.status IN ('queued','delivered') AND d.ended_at IS NULL "
        "AND t.status NOT IN ('accepted','cancelled') ORDER BY dl.created_at", (run["id"],)).fetchall()
    now, threshold, out = moment(now_iso()), idle_stall_s(), []
    for row in rows:
        since = max(moment(v) for v in (row["created_at"], row["delivered_at"], row["started_at"]) if v)
        if (now - since).total_seconds() >= threshold:
            out.append({**dict(row), "age": int((now - since).total_seconds())})
    return out


def _amendment_stalls(con, run: dict) -> list[str]:
    from office import rerun
    out = [f"{a['task_id']} amendment {a['amendment_id']} awaits ack from ended executor {a['dispatch_id']}; "
           f"next: {rerun.recovery_hint(con, run, a['task_id'], fresh=True)} to deliver/ack it"
           for a in _stranded_amendments(con, run)]
    for a in _undelivered_amendments(con, run):
        prompt = (f'office prompt {a["dispatch_id"]} -- "apply amendment {a["amendment_id"]}, then office ack '
                  f'{a["amendment_id"]}", or ' if a["launcher"] == "herdr" else "")
        out.append(f"{a['task_id']} amendment {a['amendment_id']} is unacked by live executor {a['dispatch_id']} "
                   f"after {a['age']}s; next: {prompt}{rerun.recovery_hint(con, run, a['task_id'])} to deliver/ack it")
    return out


def _idle_executors(con, run: dict, acts: dict | None = None) -> list[str]:
    """Executors whose agent stopped without submitting. Nothing else ends that
    wait: the session is alive, so no exit is recorded and no job is pending.
    Liveness unknown (herdr unreachable) is never a stall."""
    from office import db, dispatch, rerun, watchdog
    from office.util import now_iso, parse_iso
    threshold = idle_stall_s()
    out = []
    for row in con.execute("SELECT d.* FROM dispatches d JOIN tasks t ON t.run_id=d.run_id AND t.id=d.task_id "
                           "AND t.current_dispatch_id=d.id WHERE d.run_id=? AND d.role='executor' "
                           "AND d.status IN ('launching','running') AND d.ended_at IS NULL "
                           "AND d.launcher IN ('herdr','process','process-fallback') "
                           "AND t.status IN ('launching','running','changes_required')", (run["id"],)).fetchall():
        d = dict(row)
        act = acts[d["id"]] if acts is not None and d["id"] in acts else rerun.agent_activity(d)
        if act is None or act.get("question"):
            # An agent waiting on a question is reported as a `question:` line, not a stall.
            continue
        who = f"{d['task_id']} executor {d['id']}"
        tid = d["task_id"]
        actions = f"office rerun {tid} --resume|--fresh or office revoke {tid}"
        refused = con.execute("SELECT summary FROM events WHERE run_id=? AND dispatch_id=? "
                              "AND kind IN ('submit.refused','submit.rejected') ORDER BY seq DESC LIMIT 1",
                              (run["id"], d["id"])).fetchone()
        why = f"; its last submit was refused: {refused[0][:160]}" if refused else ""
        if not act["alive"]:
            what = f"process {d['pid']}" if d["launcher"] != "herdr" else f"herdr agent {d['pane_id'] or ''}".strip()
            out.append(f"{who}: {what} is gone but the dispatch is still running{why}; next: {actions}")
            continue
        limit = dispatch._usage_limit(act["text"])
        if limit is not None:
            out.append(_usage_limit_stall(con, run, d, act, limit, who))
            continue
        if any(d.get(k) for k in ("stall_kind", "resets_at", "limit_label", "limit_fingerprint")):
            with db.transaction(con):
                con.execute("UPDATE dispatches SET stall_kind=NULL, resets_at=NULL, limit_label=NULL, "
                            "limit_fingerprint=NULL WHERE id=?", (d["id"],))
        changed = act["hash"] is not None and d.get("idle_hash") not in (None, act["hash"])
        # Herdr's explicit idle state plus the executor's terminal status line
        # wins over a stale spinner/busy marker above it in scrollback (#671).
        # Idle alone is not enough: agy reports idle mid-turn, so a busy marker
        # below the status line means the agent kept working.
        final = _final_status(act["text"])
        if final is not None and act["busy"] and (act.get("status") != "idle" or dispatch._pane_busy(final["below"])):
            final = None
        if final is not None:
            tail = _write_pane_tail(run, d, act)
            out.append(f"{who}: ended its turn with SUBMIT={final['submit']}{why}; NEXT={final['next']}; "
                       f"pane tail in {tail}; next: {actions}")
            continue
        if act["busy"] or changed:
            # A spinner, even a ticking spinner, is not progress by itself (#701).
            # Watchdog reports only after unchanged pane, worktree and log evidence.
            if act["busy"]:
                hang = watchdog.check(con, run, d, role="executor", activity=act)
                if hang:
                    out.append(hang)
            idle_since = None
        else:
            idle_since = d.get("idle_since") or now_iso()
        if (idle_since, act["hash"] or d.get("idle_hash")) != (d.get("idle_since"), d.get("idle_hash")):
            with db.transaction(con):
                con.execute("UPDATE dispatches SET idle_since=?, idle_hash=? WHERE id=?",
                            (idle_since, act["hash"] or d.get("idle_hash"), d["id"]))
        if idle_since is None:
            continue
        idle = (parse_iso(now_iso()) - parse_iso(idle_since)).total_seconds()
        if idle < threshold:
            continue
        if watchdog.child_state(d) is not False:
            # A live child (a background monitor, `office wait`) is work in flight: restart the idle clock (#895).
            with db.transaction(con):
                con.execute("UPDATE dispatches SET idle_since=? WHERE id=?", (now_iso(), d["id"]))
            continue
        tail = _write_pane_tail(run, d, act)
        prompt = f'office prompt {d["id"]} -- "<message>", ' if d["launcher"] == "herdr" else ""
        out.append(f"{who}: idle {int(idle)}s without submitting{why}; pane tail in {tail}; "
                   f"next: {prompt}{actions}")
    return out


def _write_pane_tail(run: dict, d: dict, act: dict) -> Path:
    tail = paths.run_dir(run["id"]) / "dispatches" / d["id"] / "pane-tail.txt"
    if act["text"] is not None:
        atomic_write_text(tail, "\n".join(act["text"].splitlines()[-40:]) + "\n")
    return tail


# The executor's closing status line: "TASK=.. SUBMIT=<..> NEXT=<..>". A pane
# wraps it, so the line runs on until a blank line.
_FINAL_SUBMIT = re.compile(r"\bSUBMIT=(not attempted|refused\b.*?)\s+NEXT=(.*)", re.S)


def _final_status(text: str | None) -> dict | None:
    """The pane's last status line when it says the executor ended without a
    submit ({"submit", "next", "below"}), else None. `below` is the pane text
    after the status-line block. A status line followed by a newer turn (an
    echoed prompt or new assistant output) is history, not the end."""
    from office import dispatch
    lines = (text or "").splitlines()
    start = next((i for i in range(len(lines) - 1, -1, -1) if "SUBMIT=" in lines[i]), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].strip()), len(lines))
    if any(dispatch._LIMIT_ACTIVITY.match(line) for line in lines[end:]):
        return None
    m = _FINAL_SUBMIT.search(" ".join(line.strip() for line in lines[start:end]))
    if not m:
        return None
    return {"submit": m.group(1).strip(), "next": " ".join(m.group(2).split())[:300],
            "below": "\n".join(lines[end:])}


def _usage_limit_stall(con, run: dict, d: dict, act: dict, limit: dict, who: str) -> str:
    """A pane stopped on a Claude usage limit is a stall whatever its busy
    markers say (scrollback keeps a spinner line). Records the stop on the
    dispatch, writes pane-tail.txt, and names the manual `continue`."""
    from datetime import datetime, timedelta, timezone
    from office import db, routing, route_policy
    from office.util import parse_iso
    # The pane cannot prove two limits with the same screen and label are one
    # episode (the same work can hit the same limit again), so every poll stores
    # and shows the reset parsed now. When a stored limit with the same screen
    # has already reset, the output says so: the agent may only need `continue`.
    resets_at = limit["resets_at"]
    fields = {"stall_kind": "usage_limit", "resets_at": resets_at.isoformat() if resets_at else None,
              "limit_label": limit["label"], "limit_fingerprint": limit["fingerprint"]}
    # Persist the availability wall once per episode, not per status/wait poll.
    # This is a temporary provider quota cooldown, never a role-policy deny.
    with db.transaction(con):
        latest = state.get_dispatch(con, d["id"]) or d
        now = datetime.now(timezone.utc)
        stored = parse_iso(latest["resets_at"]) if latest.get("resets_at") else None
        reset_advanced = (stored is not None and stored <= now and resets_at is not None and resets_at > stored)
        passed = (stored is not None and stored <= now and (resets_at is None or resets_at > stored)
                  and latest.get("limit_fingerprint") == limit["fingerprint"]
                  and latest.get("limit_label") == limit["label"])
        reset_resolved = (latest.get("stall_kind") == "usage_limit"
                          and latest.get("limit_fingerprint") == limit["fingerprint"]
                          and latest.get("limit_label") == limit["label"]
                          and stored is None and resets_at is not None and resets_at > now)
        # A repeated poll of the stored episode is not a new one. A later episode is: another screen or label,
        # or the persisted reset has passed and a later reset is now parsed.
        episode = (latest.get("stall_kind") != "usage_limit"
                   or latest.get("limit_fingerprint") != limit["fingerprint"]
                   or latest.get("limit_label") != limit["label"]
                   or reset_advanced)
        updates = fields.copy()
        if (resets_at is None and stored is not None
                and latest.get("stall_kind") == "usage_limit"
                and latest.get("limit_fingerprint") == limit["fingerprint"]
                and latest.get("limit_label") == limit["label"]):
            updates["resets_at"] = latest["resets_at"]
        if any(latest.get(k) != v for k, v in updates.items()):
            con.execute("UPDATE dispatches SET stall_kind=?, resets_at=?, limit_label=?, limit_fingerprint=? "
                        "WHERE id=?", (*updates.values(), d["id"]))
        if episode or reset_resolved:
            expires_at = resets_at if resets_at and resets_at > now else (
                now + timedelta(minutes=route_policy.ROUTE_FAILURE_TTL_MINUTES))
            expires = expires_at.isoformat()
            model = d.get("invocation_model_id") or d.get("model")
            keys = [routing.demotion_key(d.get("harness"), model, d.get("effort"))]
            pool = routing.shared_quota_pool_key(d.get("harness"), model)
            if pool:
                keys.append(pool)
            for key in keys:
                con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?) "
                            "ON CONFLICT(route) DO UPDATE SET expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                            (key, expires, now.isoformat()))
            if episode:
                state.emit(con, run, "route.failed",
                           f"{d['task_id']} route {d.get('triple')} hit usage_limit; availability cooldown "
                           f"until {expires[:16]}Z" + (f" (shared {pool})" if pool else ""),
                           task_id=d["task_id"], dispatch_id=d["id"],
                           payload={"route": d.get("triple"), "harness": d.get("harness"), "model": model,
                                    "effort": d.get("effort"), "reason": "usage_limit", "classification": "usage_limit",
                                    "demoted_until": expires, "quota_pool": pool, "declared": bool(d.get("override_json"))})
    tail = paths.run_dir(run["id"]) / "dispatches" / d["id"] / "pane-tail.txt"
    atomic_write_text(tail, "\n".join(act["text"].splitlines()[-40:]) + "\n")
    when = (f"resets {resets_at.strftime('%Y-%m-%dT%H:%MZ')}" + (f" ({limit['tz']} {limit['local']})" if limit["tz"] else "")
            if resets_at else f"resets {limit['label'] or 'at an unknown time'} (time not resolved)")
    if passed:
        when += (f"; the same limit screen was stored with reset {stored.strftime('%Y-%m-%dT%H:%MZ')}, now past, "
                 f"so if this is that limit `continue` may already work")
    return f"{who}: usage_limit, {when}; pane tail in {tail}; next: office prompt {d['id']} -- continue"


SIGNAL_LINES = 10


def worker_signals(con, run: dict) -> tuple[list[str], int]:
    """(stall lines, last seq) for workers that stopped on something only the orchestrator resolves (a
    preflight stop, a refused submit). Every unread signal counts, however many other events are
    unread before it; the caller advances the signal cursor to `last seq` once it has reported them."""
    import json
    out, last = [], 0
    signals = state.unread_signals(con, run["id"])
    for e in signals[-SIGNAL_LINES:]:  # the newest; a worker that varies its text cannot flood the orchestrator
        p = json.loads(e["payload_json"] or "{}")
        who = " ".join(x for x in (e["task_id"], e["dispatch_id"]) if x)
        out.append(f"{who} {p.get('source', 'worker')}: {p.get('reason', e['summary'])}; next: {p.get('next', 'office status')}")
    if len(signals) > SIGNAL_LINES:
        out.insert(0, f"{len(signals) - SIGNAL_LINES} earlier worker signals not shown (office inspect events)")
    if signals:
        last = signals[-1]["seq"]
    return out, last


WAIT_ACTION_KIND = "wait.action_reported"

WAIT_STALL_KIND = "wait.stall_reported"


def _fresh_stall(con, run: dict, stuck: list[str]) -> tuple[bool, str | None]:
    if not stuck:
        return False, None
    # Ignore numeric duration as idle age advances every second with no new evidence.
    # Every duration ("idle 40s", "180s elapsed past review wall budget 3600s") grows
    # or is fixed config; neither is new stall evidence.
    stable = [re.sub(r"\b\d+s\b", "Xs", line) for line in stuck]
    signature = sha256_obj((_snapshot(con, run), tuple(sorted(stable))))
    row = con.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                      (run["id"], WAIT_STALL_KIND)).fetchone()
    prior = json.loads(row["payload_json"] or "{}").get("signature") if row else None
    return signature != prior, signature


def _action_signature(con, run: dict) -> str | None:
    """The currently actionable guidance, including its source state.

    Office wait runs in separate CLI processes. A persistent event prevents the
    same unchanged next: line from waking each invocation into a busy loop.
    """
    action = next_action(con, state.get_run(con, run["id"]))
    if action.startswith(WAITING_ACTIONS):
        return None
    return sha256_obj((_snapshot(con, run), action))


def _fresh_action(con, run: dict) -> tuple[bool, str | None]:
    signature = _action_signature(con, run)
    if signature is None:
        return False, None
    row = con.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                      (run["id"], WAIT_ACTION_KIND)).fetchone()
    prior = json.loads(row["payload_json"] or "{}").get("signature") if row else None
    return signature != prior, signature


def _needs_orchestrator(con, run: dict) -> bool:
    """Read-only actionable state check (also used by status regressions)."""
    return _fresh_action(con, run)[0]


def _work_in_flight(con, run: dict) -> bool:
    """Persisted work that can advance without a new orchestrator command."""
    queries = (
        "SELECT 1 FROM outbox WHERE run_id=? AND status IN ('queued','claimed') LIMIT 1",
        "SELECT 1 FROM gates WHERE run_id=? AND status IN ('queued','running') LIMIT 1",
        "SELECT 1 FROM dispatches WHERE run_id=? AND status IN ('launching','running') "
        "AND ended_at IS NULL LIMIT 1",
    )
    return any(con.execute(q, (run["id"],)).fetchone() is not None for q in queries)


def _wait_clock():
    """Monotonic deadline and sleeper: one seam for deterministic wait tests."""
    import time
    return time.monotonic, time.sleep


def _wait_marker(run_id: str) -> Path:
    return paths.run_dir(run_id) / "office-wait.lock"


@contextlib.contextmanager
def waiting(run_id: str):
    """Holds an exclusive lock on the run's wait marker while the root's `office wait` runs. The OS
    releases it when the process exits, so a crash never leaves a wait that looks in flight. An
    executor's own wait does not count: only the orchestrator's wait can stand in for a wake."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        yield
        return
    marker = _wait_marker(run_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another root wait holds it: never block here, so this wait keeps its own --timeout.
            yield
            return
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def wait_in_flight(run_id: str) -> bool:
    marker = _wait_marker(run_id)
    if not marker.exists():
        return False
    with marker.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def wait(con, run: dict, *, timeout: float, poll: float = 10.0) -> Result:
    """Block until something needs the orchestrator, then print status.
    Exit 0: a task, phase, plan or requirements change, or a new actionable
    orchestrator event. Exit 3: a stall, or an already-reported orchestrator
    action with no work in flight to advance it (your move, #735).
    Exit 5: an agent is waiting on a question (a `question:` line names it and
    the `office answer` command). Exit 124: timeout with nothing new. A review gate
    queued past its bound or closed unread (#919) is news: exit 0, once per episode. A watcher
    keys on the exit code, never on matching status text."""
    from office import db, dispatch, jobs, lifecycle, questions, watchdog
    from office.util import now_iso
    start = _snapshot(con, run)
    started_at = now_iso()  # only a job that fails while waiting is news
    now, sleep = _wait_clock()
    deadline = now() + timeout
    first = True
    while True:
        dispatch.reap_orphans(con, run)
        with db.transaction(con):
            lifecycle.reconcile(con, run)
            jobs.reclaim(con, run["id"])
        jobs.kick(con, run["id"])
        amend.confirm_launch_deliveries(con, run)
        signals, signals_seq = worker_signals(con, run)
        from office import ctx_alert
        ctx_alert.notice(con, run)
        asked, _, acts = questions.scan(con, run)
        episodes = watchdog.review_episodes(con, run, probe=True)
        stuck = stalls(con, run, since=started_at, acts=acts, episodes=episodes) + signals
        fresh_stall, stall_signature = _fresh_stall(con, run, stuck)
        review_news = watchdog.fresh_review_episodes(con, run, episodes)
        changed = _snapshot(con, run) != start
        # Only an *actionable* orchestrator event warrants an exit 0: a task paused on a
        # refused submit, a gate failure, a recheck the orchestrator must route. A pure
        # lifecycle notification (a launch.session, a setup.done) carries no command to
        # run, so an event older than the cursor must not wake an otherwise-idle wait into
        # exit 0, which an orchestrator reads as "act now" and busy-loops on. Stalls and
        # signals that DO carry a next step wake via the stall pass; real state
        # transitions still wake via `changed`. (#296) Scan *all* unread events, not a
        # bounded prefix: many older informational events must not hide a newer urgent
        # one (e.g. a late pr.error behind 64 launch.session/setup.done notes).
        news = [e for e in state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=None)
                if _urgent(e)]
        # Something already waiting on the orchestrator ends the wait at once;
        # otherwise a blocker present at the start would sit until the timeout.
        # A scope lease may become dormant when its holder ends without a new
        # task revision or status event. Recheck actionable work on every poll:
        # the initial snapshot alone cannot observe that transition (#561).
        pending = _needs_orchestrator(con, run)
        action_signature = _action_signature(con, run) if pending else None
        fresh_action, prior_action = _fresh_action(con, run) if not pending else (True, None)
        your_move = (not pending and not fresh_action and prior_action is not None
                     and not _work_in_flight(con, run))
        first = False
        terminal = state.is_terminal(state.get_run(con, run["id"]))
        if asked or fresh_stall or changed or news or review_news or pending or your_move or terminal or now() >= deadline:
            res = status(con, run)
            if pending:
                with db.transaction(con):
                    state.emit(con, run, WAIT_ACTION_KIND, "orchestrator action reported by office wait",
                               audience="runtime", payload={"signature": action_signature})
            if news:
                # status shows a bounded prefix of events; a newer urgent event
                # behind a long informational backlog must be shown and consumed
                # now, rather than wake every subsequent wait with the same news.
                res.lines[1:1] = [f"· {e['summary']}" for e in news[-6:]
                                  if f"· {e['summary']}" not in res.lines]
                with db.transaction(con):
                    state.consume_events(con, run["id"], news)
            if signals:
                with db.transaction(con):
                    state.advance_cursor(con, run["id"], state.SIGNAL_CONSUMER, signals_seq)
            if fresh_stall:
                with db.transaction(con):
                    state.emit(con, run, WAIT_STALL_KIND, "worker stall reported by office wait",
                               audience="runtime", payload={"signature": stall_signature})
                res.lines[1:1] = [f"stall: {s}" for s in stuck]
                res.exit_code = 3
            elif your_move and not (changed or news):
                res.lines.insert(1, "stall: no work is in flight; the pending orchestrator action still needs you")
                res.exit_code = 3
            if asked:
                # status() already printed every recorded question; the new ones lead and set the exit.
                res.lines = [res.lines[0], *[f"question: {q}" for q in asked],
                             *[ln for ln in res.lines[1:] if ln.removeprefix("question: ") not in asked]]
                res.exit_code = questions.EXIT
            elif not (fresh_stall or changed or news or review_news or pending or your_move or terminal):
                res.lines.insert(1, f"wait: nothing new in {int(timeout)}s")
                res.exit_code = 124
            return add_bug_hints(con, run, res)
        sleep(poll)


def _waiting_on(con, run: dict, task: dict) -> str:
    """What a live task is actually waiting on, so `live` never hides a stall:
    an amendment its session must ack, a submission held for one, or gates."""
    rid = run["id"]
    # Only a held revision newer than the current one is still waiting; an older
    # one was replaced by a later submission.
    held = con.execute("SELECT id FROM revisions WHERE run_id=? AND task_id=? AND status='amendment_pending' "
                       "AND seq > COALESCE((SELECT seq FROM revisions WHERE id=?), 0) ORDER BY seq DESC LIMIT 1",
                       (rid, task["id"], task.get("current_revision_id"))).fetchone()
    parts = []
    for dl in con.execute("SELECT amendment_id, dispatch_id FROM deliveries WHERE run_id=? AND task_id=? "
                          "AND status IN ('queued','delivered') ORDER BY created_at", (rid, task["id"])).fetchall():
        holder = dl["dispatch_id"]
        stale = holder and holder != task["current_dispatch_id"]
        parts.append(f"{dl['amendment_id']} ack by {holder or 'next session'}"
                     + (" (not the current session; it can ack it)" if stale else ""))
    if held:
        parts.insert(0, f"{held['id']} held for amendment")
    if task.get("current_revision_id"):
        from office import gates as gates_mod
        for g in con.execute("SELECT * FROM gates WHERE revision_id=? AND status IN ('queued','running','waiting')",
                             (task["current_revision_id"],)).fetchall():
            owner = gates_mod.owner_state(con, run, g) if g["status"] == "running" else ("", "")
            # An owner whose liveness cannot be read is shown, never reclaimed.
            parts.append(f"{g['kind']} {g['status']}" + (f" ({owner[1]})" if owner[0] == "unknown" else ""))
        if task["status"] in ("submitted", "accepted") and not parts:
            from office import gates
            stale = gates.stale_dependency(con, run, task)
            if stale:
                parts.append(stale)
    return "; ".join(parts)


def worker_status(con, run: dict, dispatch_id: str) -> Result:
    from office import raising
    d = state.get_dispatch(con, dispatch_id)
    task = state.get_task(con, run["id"], d["task_id"]) if d and d.get("task_id") else None
    if task is None:
        return Result(lines=[f"{short(run['id'])} {run['phase']}"], next="no action")
    res = Result()
    res.add(f"{task['id']} {task['status']} | rev {task.get('current_revision_id') or '-'} | plan p{run['plan_version']}")
    findings = con.execute("SELECT code, severity, location, summary, action FROM findings WHERE run_id=? AND task_id=? "
                           "AND " + contract.TASK_WORK_FINDINGS + " ORDER BY created_at", (run["id"], task["id"])).fetchall()
    for f in findings[:8]:
        res.add(f"{f['code']} {f['severity']} {f['location'] or ''} — {f['summary'][:140]}"
                + (f" -> {f['action'][:80]}" if f["action"] else ""))
    block = amend.pending_block(con, run, dispatch_id)
    res.lines.extend(block)
    res.lines.extend(amend.pending_messages(con, run, dispatch_id))
    if block:
        res.next = "apply the amendment at a safe boundary, then office ack <id>"
    elif task["status"] == "changes_required":
        res.next = "fix the findings, then office submit"
    elif task["status"] in ("submitted",):
        res.next = "no action; verification is running"
    elif raising.open_raises(con, run, dispatch_id=dispatch_id):
        res.next = ("stop and wait; you raised it and the orchestrator answers: the answer arrives as a message in "
                    "this session. Do not submit unfinished work")
    elif task["status"] in ("paused", "blocked"):
        res.next = f"stop; {task.get('pause_reason') or task['status']}"
    elif task["status"] == "accepted":
        res.next = "none; the task is accepted"
    else:
        res.next = "continue the task; office submit when ready"
    return res


def piggyback(con, run: dict, res: Result) -> None:
    """Attach pending deliveries (workers) or new orchestrator events."""
    worker = os.environ.get("OFFICE_DISPATCH_ID")
    if worker:
        res.notices.extend(amend.pending_block(con, run, worker))
        res.notices.extend(amend.pending_messages(con, run, worker))
        return
    from office import db
    unread = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=200)
    held = _held_seqs(con, run["id"])
    shown = _notice_batch([e for e in unread if e["seq"] not in held], limit=4)
    if shown:
        res.notices.extend(f"· {e['summary']}" for e in shown)
        with db.transaction(con):
            _consume(con, run["id"], unread, held | {e["seq"] for e in shown})
            signals = [e["seq"] for e in shown if e["kind"] == state.SIGNAL_KIND]
            if signals:
                state.advance_cursor(con, run["id"], state.SIGNAL_CONSUMER, max(signals))


# Kinds that mean something failed or waits on the orchestrator. Everything else is informational.
_URGENT_KINDS = frozenset({
    "task.blocked", "task.paused", "task.findings_queued", "task.scope_requested", "task.raised", "task.restack_needed",
    "task.amend_undelivered", "submit.refused", "submit.rejected", "integration.conflict", "integration.stale", "integration.failed",
    "gate.unavailable", "gate.changes_required", "gate.attention", "gate.escalated", "gate.brief_defect",
    "plan.unavailable", "plan.changes_required", "plan.attention", "plan.escalated", "plan.defect",
    "plan.questions", "plan.contract_requested", "pr.error", "lease.revoked",
    # convergence contract (#337)
    "gate.recheck", "plan.recheck", "plan.intake_gap", "plan.budget_exhausted", "convergence.recheck",
    "convergence.intake_gap", "convergence.escalation", "convergence.unavailable", "convergence.blocked",
    "convergence.conflict",
    # review gates past a bound (#919)
    "review.queued_too_long", "review.stalled", "review.closed_unread"})
_SEEN = "orchestrator:seen:"
# next: prefixes that leave the root nothing to do until an event or a running task moves on.
WAITING_ACTIONS = ("exceptions only", "no action", "none;")


def _urgent(event: dict) -> bool:
    return event["kind"] in _URGENT_KINDS or event["kind"] == "ctx.high" or event["kind"].endswith(".failed")


def _notice_batch(pending: list[dict], *, limit: int) -> list[dict]:
    """The events to show: failures and blockers first, then informational notices in order,
    `limit` in all. What does not fit stays unread for the next command."""
    return ([e for e in pending if _urgent(e)] + [e for e in pending if not _urgent(e)])[:limit]


def _held_seqs(con, run_id: str) -> set[int]:
    """Events shown while an older one was still waiting: the cursor is one number, so each is
    remembered on its own until the cursor passes it."""
    return {int(r[0][len(_SEEN):]) for r in con.execute(
        "SELECT consumer FROM cursors WHERE run_id=? AND consumer LIKE ?", (run_id, _SEEN + "%"))}


def _consume(con, run_id: str, unread: list[dict], consumed: set[int]) -> None:
    """Move the orchestrator cursor over the leading run of consumed events; remember the rest by seq."""
    through = 0
    for e in unread:
        if e["seq"] not in consumed:
            break
        through = e["seq"]
    if through:
        state.advance_cursor(con, run_id, "orchestrator", through)
        con.execute("DELETE FROM cursors WHERE run_id=? AND consumer LIKE ? AND CAST(substr(consumer, ?) AS INTEGER)<=?",
                    (run_id, _SEEN + "%", len(_SEEN) + 1, through))
    for seq in consumed:
        if seq > through:
            state.advance_cursor(con, run_id, _SEEN + str(seq), seq)
