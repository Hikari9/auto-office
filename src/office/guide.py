"""Compact status and the next legal action.

Every command ends with one `next:` line derived from canonical state, so an
agent never needs an `office next` command or the Office source to proceed.
"""
from __future__ import annotations

import re

import os

from office import amend, planpath, plans, state
from office.result import Result
from office.util import short

ORCH_EVENT_KINDS = None  # every orchestrator-audience event


def _counts(tasks: list[dict]) -> dict:
    out: dict[str, list[str]] = {}
    for t in tasks:
        out.setdefault(t["status"], []).append(t["id"])
    return out


def ready_tasks(con, run: dict) -> list[str]:
    tasks = state.tasks(con, run["id"])
    by_id = {t["id"]: t for t in tasks}
    ready = []
    for t in tasks:
        if t["status"] != "planned":
            continue
        ok = True
        for dep in t["depends"]:
            d = by_id.get(dep)
            if not d or d["status"] not in ("accepted", "submitted", "changes_required") or not d.get("current_revision_id"):
                ok = False
        if ok:
            ready.append(t["id"])
    return ready


def next_action(con, run: dict) -> str:
    run = state.get_run(con, run["id"])
    if state.is_terminal(run):
        return "none; the run is " + run["phase"]
    if not run["plan_version"]:
        if run.get("planner_mode") == "dedicated":
            return "no action; the planner is working (office status)"
        return f"write {planpath.rel(run)} (office submit --help shows the format), then office submit"
    plan = state.current_plan(con, run["id"])
    from office import planfile
    if planfile.parse(plan["body"]).questions and not plans._answered(con, run):
        return 'ask the user the plan questions (native question tool), then office amend plan --contract -- "<answers>"'
    rs = plans.review_state(con, run)
    if rs["required"] and not rs["ended"] and rs["first_verdict"] is None:
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
    for status in ("blocked", "paused"):
        if c.get(status):
            tid = c[status][0]
            t = next(x for x in tasks if x["id"] == tid)
            return f"resolve {tid} ({t.get('pause_reason') or status}); office inspect task {tid}"
    from office import gates as gates_mod
    for t in tasks:
        if t["status"] == "changes_required" and not gates_mod.worker_live(con, t.get("current_dispatch_id")):
            # Findings wait for the orchestrator's choice (R8); nothing relaunches on its own.
            return f"findings on {t['id']} wait for you: office rerun {t['id']} --resume | --fresh"
    ready = ready_tasks(con, run)
    if ready:
        return "choose execution strategy; office dispatch " + " ".join(ready) + (" --parallel" if len(ready) > 1 else "")
    live = [t for t in tasks if t["status"] not in ("accepted", "cancelled", "planned")]
    if live:
        return "exceptions only; office status"
    from office import integration
    integ = integration.status(con, run)
    if integ["required"] and integ["status"] not in ("accepted",):
        if integ["status"] == "conflict":
            return f"integration conflict: {integ.get('detail', '')}; office inspect run"
        if integ["status"] in ("blocked", "unavailable"):
            return (f"integration {integ['status']}: {integ.get('detail', '')}; fix the cause, then office resume "
                    "to retry integration")
        return "no action; integration verification is running"
    if ((run.get("landing") or {}).get("prs") or {}).get("enabled"):
        from office import land
        return f"office land (end state: {land.end_state(con, run)['mode']})"
    branch = integ.get("branch")
    return (f"land it: push {branch} and open a PR (merge to main stays with the user), then office close --handoff <pr-url>"
            if branch else "office close --handoff <ref>")


def status(con, run: dict, *, resumed: bool = False, verbose: bool = False) -> Result:
    worker = os.environ.get("OFFICE_DISPATCH_ID")
    if worker:
        return worker_status(con, run, worker)
    run = state.get_run(con, run["id"])
    tasks = state.tasks(con, run["id"])
    c = _counts(tasks)
    res = Result()
    res.add(f"{short(run['id'])} {run['phase']} | req r{run['requirements_version']} | plan p{run['plan_version']}")
    from office import upgrade
    stale = upgrade.notice(run)
    if stale:
        res.add(stale)
    if tasks:
        parts = [f"accepted {len(c.get('accepted', []))}/{len([t for t in tasks if t['status'] != 'cancelled'])}"]
        for label, keys in (("live", ("running", "launching", "submitted", "changes_required")), ("queued", ("queued",)),
                            ("paused", ("paused",)), ("blocked", ("blocked",))):
            ids = [i for k in keys for i in c.get(k, [])]
            if ids:
                parts.append(f"{label} {','.join(ids)}")
        res.add(" | ".join(parts))
    rs = plans.review_state(con, run)
    if rs["required"]:
        pr = "plan review " + ("ended (" + (rs["ended_reason"] or "") + ")" if rs["ended"] else
                               ("running" if rs["pending"] else (rs["last_verdict"] or "pending")))
        res.add(pr)
    for t in tasks:
        if t["status"] in ("paused", "blocked"):
            res.add(f"blocker: {t['id']} {t.get('pause_reason') or t['status']}")
        elif t["status"] in ("running", "launching", "submitted", "changes_required"):
            waiting = _waiting_on(con, run, t)
            if waiting:
                res.add(f"{t['id']} waiting: {waiting}")
    events = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=6)
    for e in events:
        res.add(f"· {e['summary']}")
    if events:
        from office import db
        with db.transaction(con):
            state.advance_cursor(con, run["id"], "orchestrator", events[-1]["seq"])
    res.next = next_action(con, run)
    res.data = {"run_id": run["id"], "phase": run["phase"], "office_version": run["office_version"],
                "requirements_version": run["requirements_version"], "plan_version": run["plan_version"],
                "tasks": {t["id"]: t["status"] for t in tasks}, "plan_review": {k: v for k, v in rs.items() if k != "open_defects"},
                "open_defects": [d["code"] for d in rs["open_defects"]], "next": res.next}
    if resumed:
        res.verbose.append("resumed: pending jobs and deliveries reconstructed from runs.db")
    return res


def _snapshot(con, run: dict) -> tuple:
    run = state.get_run(con, run["id"])
    tasks = tuple((t["id"], t["status"], t.get("current_revision_id")) for t in state.tasks(con, run["id"]))
    return run["phase"], run["requirements_version"], run["plan_version"], tasks


def stalls(con, run: dict, since: str = "") -> list[str]:
    """Work Office believes is in progress with nothing left to advance it."""
    out = []
    live_jobs = con.execute("SELECT COUNT(*) FROM outbox WHERE run_id=? AND status IN ('queued','claimed')",
                            (run["id"],)).fetchone()[0]
    for g in con.execute("SELECT id, task_id, kind, status FROM gates WHERE run_id=? AND status IN ('queued','running')",
                         (run["id"],)).fetchall():
        if not live_jobs:
            out.append(f"{g['task_id'] or 'plan'} {g['kind']} gate {g['id']} is {g['status']} but no job is queued or running")
    for j in con.execute("SELECT kind, error FROM outbox WHERE run_id=? AND status='failed' AND finished_at > ?",
                         (run["id"], since)).fetchall():
        out.append(f"job {j['kind']} failed: {(j['error'] or '')[:120]}")
    return out


def _needs_orchestrator(con, run: dict) -> bool:
    if any(t["status"] in ("paused", "blocked", "changes_required") for t in state.tasks(con, run["id"])):
        return True
    return not next_action(con, state.get_run(con, run["id"])).startswith(("exceptions only", "no action"))


def wait(con, run: dict, *, timeout: float, poll: float = 10.0) -> Result:
    """Block until something needs the orchestrator, then print status.
    Exit 0: a task, phase, plan or requirements change, or a new orchestrator
    event. Exit 3: a stall (work marked in progress that nothing can advance).
    Exit 124: timeout with nothing new. A watcher keys on the exit code, never
    on matching status text."""
    import time
    from office import db, jobs, lifecycle
    from office.util import now_iso
    start = _snapshot(con, run)
    started_at = now_iso()  # only a job that fails while waiting is news
    deadline = time.time() + timeout
    first = True
    while True:
        with db.transaction(con):
            lifecycle.reconcile(con, run)
            jobs.reclaim(con, run["id"])
        jobs.kick(con, run["id"])
        stuck = stalls(con, run, since=started_at)
        changed = _snapshot(con, run) != start
        news = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=1)
        # Something already waiting on the orchestrator ends the wait at once;
        # otherwise a blocker present at the start would sit until the timeout.
        pending = first and _needs_orchestrator(con, run)
        first = False
        if stuck or changed or news or pending or state.is_terminal(state.get_run(con, run["id"])) \
                or time.time() >= deadline:
            res = status(con, run)
            if stuck:
                res.lines[1:1] = [f"stall: {s}" for s in stuck]
                res.exit_code = 3
            elif not (changed or news or pending):
                res.lines.insert(1, f"wait: nothing new in {int(timeout)}s")
                res.exit_code = 124
            return res
        time.sleep(poll)


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
        for g in con.execute("SELECT kind, status FROM gates WHERE revision_id=? AND status IN ('queued','running','waiting')",
                             (task["current_revision_id"],)).fetchall():
            parts.append(f"{g['kind']} {g['status']}")
    return "; ".join(parts)


def worker_status(con, run: dict, dispatch_id: str) -> Result:
    d = state.get_dispatch(con, dispatch_id)
    task = state.get_task(con, run["id"], d["task_id"]) if d and d.get("task_id") else None
    if task is None:
        return Result(lines=[f"{short(run['id'])} {run['phase']}"], next="no action")
    res = Result()
    res.add(f"{task['id']} {task['status']} | rev {task.get('current_revision_id') or '-'} | plan p{run['plan_version']}")
    findings = con.execute("SELECT code, severity, location, summary, action FROM findings WHERE run_id=? AND task_id=? "
                           "AND state='open' ORDER BY created_at", (run["id"], task["id"])).fetchall()
    for f in findings[:8]:
        res.add(f"{f['code']} {f['severity']} {f['location'] or ''} — {f['summary'][:140]}"
                + (f" -> {f['action'][:80]}" if f["action"] else ""))
    block = amend.pending_block(con, run, dispatch_id)
    res.lines.extend(block)
    if block:
        res.next = "apply the amendment at a safe boundary, then office ack <id>"
    elif task["status"] == "changes_required":
        res.next = "fix the findings, then office submit"
    elif task["status"] in ("submitted",):
        res.next = "no action; verification is running"
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
        block = amend.pending_block(con, run, worker)
        if block:
            res.notices.extend(block)
        return
    events = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=4)
    if events:
        res.notices.extend(f"· {e['summary']}" for e in events)
        from office import db
        with db.transaction(con):
            state.advance_cursor(con, run["id"], "orchestrator", events[-1]["seq"])
