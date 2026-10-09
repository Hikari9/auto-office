"""Compact status and the next legal action.

Every command ends with one `next:` line derived from canonical state, so an
agent never needs an `office next` command or the Office source to proceed.
"""
from __future__ import annotations

import json
import re

import os
from pathlib import Path

from office import amend, contract, paths, planpath, plans, state
from office.result import Result
from office.util import atomic_write_text, short

ORCH_EVENT_KINDS = None  # every orchestrator-audience event

IDLE_STALL_DEFAULT_S = 60.0


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
        return f"write {planpath.rel(run)} (office submit --help shows the format), then office submit"
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
    for status in ("blocked", "paused"):
        if c.get(status):
            tid = c[status][0]
            t = next(x for x in tasks if x["id"] == tid)
            return f"resolve {tid} ({t.get('pause_reason') or status}); office inspect task {tid}"
    from office import gates as gates_mod
    if contract.is_convergence(run):
        # A lane RECHECK routes one consolidated repair set: name every owner at once.
        from office import convergence
        nxt = convergence.next_action(con, run)
        if nxt:
            return nxt
    for t in tasks:
        if t["status"] == "changes_required" and not gates_mod.worker_live(con, t.get("current_dispatch_id")):
            # Findings wait for the orchestrator's choice (R8); nothing relaunches on its own.
            return f"findings on {t['id']} wait for you: office rerun {t['id']} --resume | --fresh"
    for t in tasks:
        if t["status"] == "changes_required":
            # Its live worker has the findings; the repair path stays the next action.
            return (f"no action; {t['id']}'s worker is fixing its findings (if it stops: "
                    f"office rerun {t['id']} --resume | --fresh)")
    for t in tasks:
        if t["status"] == "submitted":
            stale = gates_mod.stale_dependency(con, run, t)
            if stale:
                return f"{t['id']} {stale}"
    ready = ready_tasks(con, run)
    if ready:
        return "choose execution strategy; office dispatch " + " ".join(ready) + (" --parallel" if len(ready) > 1 else "")
    live = [t for t in tasks if t["status"] not in ("accepted", "cancelled", "planned")]
    if live:
        held = held_by_dependencies(con, run)
        return "exceptions only; office status" + (f" ({held[0]})" if held else "")
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


def status(con, run: dict, *, resumed: bool = False, verbose: bool = False) -> Result:
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
    if tasks:
        parts = [f"accepted {len(c.get('accepted', []))}/{len([t for t in tasks if t['status'] != 'cancelled'])}"]
        for label, keys in (("live", ("running", "launching", "submitted", "changes_required")), ("queued", ("queued",)),
                            ("paused", ("paused",)), ("blocked", ("blocked",))):
            ids = [i for k in keys for i in c.get(k, [])]
            if ids:
                parts.append(f"{label} {','.join(ids)}")
        res.add(" | ".join(parts))
    routes = effective_routes(con, run, tasks)
    if routes:
        res.add("routes: " + ", ".join(f"{tid} {r}" for tid, r in routes.items()))
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
    from office import questions
    for q in questions.recorded(con, run):
        res.add(f"question: {q}")
    events = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=6)
    for e in events:
        res.add(f"· {e['summary']}")
    if events:
        from office import db
        with db.transaction(con):
            state.consume_events(con, run["id"], events)
    res.next = next_action(con, run)
    res.data = {"run_id": run["id"], "phase": run["phase"], "office_version": run["office_version"],
                "requirements_version": run["requirements_version"], "plan_version": run["plan_version"],
                "tasks": {t["id"]: t["status"] for t in tasks}, "routes": routes, "plan_review": {k: v for k, v in rs.items() if k != "open_defects"},
                "open_defects": [d["code"] for d in rs["open_defects"]], "next": res.next}
    if resumed:
        res.verbose.append("resumed: pending jobs and deliveries reconstructed from runs.db")
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
        rec = state.recorded_route(t)
        if rec:
            out[t["id"]] = routing.candidate_id(rec["candidate"]) + (" (declared)" if rec.get("declared") else "")
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


def stalls(con, run: dict, since: str = "", acts: dict | None = None) -> list[str]:
    """Work Office believes is in progress with nothing left to advance it. `acts` is the pane
    activity `questions.scan` already read, by dispatch id, so no pane is read twice."""
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
    out.extend(_idle_executors(con, run, acts))
    return out


def _idle_executors(con, run: dict, acts: dict | None = None) -> list[str]:
    """Executors whose agent stopped without submitting. Nothing else ends that
    wait: the session is alive, so no exit is recorded and no job is pending.
    Liveness unknown (herdr unreachable) is never a stall."""
    from office import db, dispatch, rerun
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
        final = None if act["busy"] else _final_status(act["text"])
        if final is not None:
            tail = _write_pane_tail(run, d, act)
            out.append(f"{who}: ended its turn with SUBMIT={final['submit']}{why}; NEXT={final['next']}; "
                       f"pane tail in {tail}; next: {actions}")
            continue
        if act["busy"] or changed:
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
    submit ({"submit", "next"}), else None. A status line followed by a newer
    turn (an echoed prompt or new assistant output) is history, not the end."""
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
    return {"submit": m.group(1).strip(), "next": " ".join(m.group(2).split())[:300]}


def _usage_limit_stall(con, run: dict, d: dict, act: dict, limit: dict, who: str) -> str:
    """A pane stopped on a Claude usage limit is a stall whatever its busy
    markers say (scrollback keeps a spinner line). Records the stop on the
    dispatch, writes pane-tail.txt, and names the manual `continue`."""
    from datetime import datetime, timezone
    from office import db
    from office.util import parse_iso
    # The pane cannot prove two limits with the same screen and label are one
    # episode (the same work can hit the same limit again), so every poll stores
    # and shows the reset parsed now. When a stored limit with the same screen
    # has already reset, the output says so: the agent may only need `continue`.
    resets_at = limit["resets_at"]
    prior = parse_iso(d["resets_at"]) if d.get("resets_at") else None
    passed = (prior is not None and prior <= datetime.now(timezone.utc) and prior != resets_at
              and d.get("limit_fingerprint") == limit["fingerprint"] and d.get("limit_label") == limit["label"])
    fields = {"stall_kind": "usage_limit", "resets_at": resets_at.isoformat() if resets_at else None,
              "limit_label": limit["label"], "limit_fingerprint": limit["fingerprint"]}
    if any(d.get(k) != v for k, v in fields.items()):
        with db.transaction(con):
            con.execute("UPDATE dispatches SET stall_kind=?, resets_at=?, limit_label=?, limit_fingerprint=? "
                        "WHERE id=?", (*fields.values(), d["id"]))
    tail = paths.run_dir(run["id"]) / "dispatches" / d["id"] / "pane-tail.txt"
    atomic_write_text(tail, "\n".join(act["text"].splitlines()[-40:]) + "\n")
    when = (f"resets {resets_at.strftime('%Y-%m-%dT%H:%MZ')}" + (f" ({limit['tz']} {limit['local']})" if limit["tz"] else "")
            if resets_at else f"resets {limit['label'] or 'at an unknown time'} (time not resolved)")
    if passed:
        when += (f"; the same limit screen was stored with reset {prior.strftime('%Y-%m-%dT%H:%MZ')}, now past, "
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


def _needs_orchestrator(con, run: dict) -> bool:
    if any(t["status"] in ("paused", "blocked", "changes_required") for t in state.tasks(con, run["id"])):
        return True
    return not next_action(con, state.get_run(con, run["id"])).startswith(("exceptions only", "no action"))


def wait(con, run: dict, *, timeout: float, poll: float = 10.0) -> Result:
    """Block until something needs the orchestrator, then print status.
    Exit 0: a task, phase, plan or requirements change, or a new orchestrator
    event. Exit 3: a stall (work marked in progress that nothing can advance).
    Exit 5: an agent is waiting on a question (a `question:` line names it and
    the `office answer` command). Exit 124: timeout with nothing new. A watcher
    keys on the exit code, never on matching status text."""
    import time
    from office import db, dispatch, jobs, lifecycle, questions
    from office.util import now_iso
    start = _snapshot(con, run)
    started_at = now_iso()  # only a job that fails while waiting is news
    deadline = time.time() + timeout
    first = True
    while True:
        dispatch.reap_orphans(con, run)
        with db.transaction(con):
            lifecycle.reconcile(con, run)
            jobs.reclaim(con, run["id"])
        jobs.kick(con, run["id"])
        amend.confirm_launch_deliveries(con, run)
        signals, signals_seq = worker_signals(con, run)
        asked, _, acts = questions.scan(con, run)
        stuck = stalls(con, run, since=started_at, acts=acts) + signals
        changed = _snapshot(con, run) != start
        news = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=1)
        # Something already waiting on the orchestrator ends the wait at once;
        # otherwise a blocker present at the start would sit until the timeout.
        pending = first and _needs_orchestrator(con, run)
        first = False
        if asked or stuck or changed or news or pending or state.is_terminal(state.get_run(con, run["id"])) \
                or time.time() >= deadline:
            res = status(con, run)
            if signals:
                with db.transaction(con):
                    state.advance_cursor(con, run["id"], state.SIGNAL_CONSUMER, signals_seq)
            if stuck:
                res.lines[1:1] = [f"stall: {s}" for s in stuck]
                res.exit_code = 3
            if asked:
                # status() already printed every recorded question; the new ones lead and set the exit.
                res.lines = [res.lines[0], *[f"question: {q}" for q in asked],
                             *[ln for ln in res.lines[1:] if ln.removeprefix("question: ") not in asked]]
                res.exit_code = questions.EXIT
            elif not (stuck or changed or news or pending):
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
        from office import gates as gates_mod
        for g in con.execute("SELECT * FROM gates WHERE revision_id=? AND status IN ('queued','running','waiting')",
                             (task["current_revision_id"],)).fetchall():
            owner = gates_mod.owner_state(con, run, g) if g["status"] == "running" else ("", "")
            # An owner whose liveness cannot be read is shown, never reclaimed.
            parts.append(f"{g['kind']} {g['status']}" + (f" ({owner[1]})" if owner[0] == "unknown" else ""))
        if task["status"] == "submitted" and not parts:
            from office import gates
            stale = gates.stale_dependency(con, run, task)
            if stale:
                parts.append(stale)
    return "; ".join(parts)


def worker_status(con, run: dict, dispatch_id: str) -> Result:
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
    "task.blocked", "task.paused", "task.findings_queued", "task.scope_requested", "task.restack_needed",
    "task.amend_undelivered", "submit.refused", "submit.rejected", "integration.conflict", "integration.failed",
    "gate.unavailable", "gate.changes_required", "gate.attention", "gate.escalated", "gate.brief_defect",
    "plan.unavailable", "plan.changes_required", "plan.attention", "plan.escalated", "plan.defect",
    "plan.questions", "plan.contract_requested", "pr.error", "lease.revoked",
    # convergence contract (#337)
    "gate.recheck", "plan.recheck", "plan.intake_gap", "plan.budget_exhausted", "convergence.recheck",
    "convergence.intake_gap", "convergence.escalation", "convergence.unavailable", "convergence.blocked",
    "convergence.conflict"})
_SEEN = "orchestrator:seen:"


def _urgent(event: dict) -> bool:
    return event["kind"] in _URGENT_KINDS or event["kind"].endswith(".failed")


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
