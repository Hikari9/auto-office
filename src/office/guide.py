"""Compact status and the next legal action.

Every command ends with one `next:` line derived from canonical state, so an
agent never needs an `office next` command or the Office source to proceed.
"""
from __future__ import annotations

import os

from office import amend, plans, state
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
        return "write .office/PLAN.md (office submit --help shows the format), then office submit"
    plan = state.current_plan(con, run["id"])
    from office import planfile
    if planfile.parse(plan["body"]).questions and not plans._answered(con, run):
        return 'ask the user the plan questions (native question tool), then office amend plan --contract -- "<answers>"'
    rs = plans.review_state(con, run)
    if rs["required"] and not rs["ended"] and rs["first_verdict"] is None:
        return "no action; plan review is running"
    if rs["first_verdict"] == "CHANGES_REQUIRED" and run["plan_version"] <= (rs["first_version"] or 0) and not rs["ended"]:
        return 'amend the plan: office amend plan -- "<changes>" (edit .office/PLAN.md for task changes); safe work may launch right after'
    for d in rs["open_defects"]:
        return f"plan defect {d['code']} blocks {d.get('location') or 'the plan'}; " + (
            'request the fix: office amend plan --contract -- "<fix>"' if run.get("planner_mode") == "dedicated"
            else "fix .office/PLAN.md, then office amend plan --contract -- \"<fix>\"")
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
    res.add(f"{short(run['id'])} {run['phase']} | req r{run['requirements_version']} | plan p{run['plan_version']}"
            + ("" if run["office_version"] else ""))
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
