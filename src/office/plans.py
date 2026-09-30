"""Plans, rolling plan review, and the dispatch barrier.

Fork after the first plan-review verdict (docs/v31-rolling-review-gates.md §2):

  PASS              -> launch ready executors; plan review ends.
  CHANGES_REQUIRED  -> orchestrator amends; eligible executors launch at once,
                       while the amended plan is re-reviewed concurrently.
  PLAN/BRIEF_DEFECT -> nothing launches in the affected scope until an
                       independent reviewer clears the defect by id.
  UNAVAILABLE       -> nothing launches until a substitute reviewer answers
                       or the user decides.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from office import briefs, candidates, db, jobs, planfile, planpath, review_parse, routing, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, sha256_bytes, short

PLAN_SUBJECT = "plan"


# ------------------------------------------------------------------ submit

def submit_plan(con, run: dict, plan_path: Path, *, submitter: str, dispatch_id: str | None = None,
                redirect: dict | None = None) -> Result:
    """`redirect` is a validated defect redirect (office.redirect) this revision follows."""
    if not plan_path.is_file():
        raise Usage("no-plan-file", f"no plan at {plan_path}", next_step=f"write {plan_path}, then office submit")
    text = planfile.strip_generated(plan_path.read_text(encoding="utf-8"))
    parsed = planfile.parse(text)
    if parsed.errors:
        raise Refused("plan-invalid", "plan has problems: " + "; ".join(parsed.errors[:6]),
                      scope="plan", preserved=f"{plan_path} is unchanged",
                      next_step=f"fix {plan_path}, then office submit (office submit --help shows the format)",
                      data={"errors": parsed.errors})
    frozen = state.current_requirements(con, run["id"])["frozen"]
    end_errors, end_warnings = planfile.end_state_problems(
        parsed.requirements.get("end_state") or frozen.get("end_state"),
        {**(frozen.get("deploy") or {}), **(parsed.requirements.get("deploy") or {})})
    if end_errors:
        raise Refused("plan-invalid", "plan has problems: " + "; ".join(end_errors), scope="plan",
                      preserved=".office/PLAN.md is unchanged", next_step="add the confirmed deploy command, then office submit")
    parsed.warnings.extend(end_warnings)
    digest = sha256_bytes(text.encode())
    current = state.current_plan(con, run["id"])
    if current and current["content_hash"] == digest and redirect:
        raise Refused("plan-unchanged", "a redirect submits the revision that follows it; the plan is unchanged",
                      scope="plan", next_step=f"revise {plan_path} to follow the redirect, then submit again")
    if current and current["content_hash"] == digest:
        return Result(lines=[f"plan p{current['version']} already submitted"], next=_after_plan_next(con, run))
    from office import visual
    visual_errors, visual_warnings = visual.preflight(parsed.tasks)
    if visual_errors:
        raise Refused("plan-visual-uncapturable", "the visual gate could never capture: " + "; ".join(visual_errors[:4]),
                      scope="plan", preserved=f"{plan_path} is unchanged",
                      next_step=f"fix the visual block in {plan_path}, then office submit",
                      data={"errors": visual_errors})
    parsed.warnings[:0] = visual_warnings
    res = Result()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        if redirect:
            from office import redirect as redirect_mod
            for line in redirect_mod.record(con, run, redirect):
                res.add(line)
            run = state.get_run(con, run["id"])
        new_version = (run["plan_version"] or 0) + 1
        kind = "initial" if new_version == 1 else "contract"
        _apply_requirements(con, run, parsed.requirements, submitter)
        run = state.get_run(con, run["id"])
        con.execute("INSERT INTO plans(run_id, version, kind, body, tasks_json, requirements_json, created_by, created_at, "
                    "content_hash, parent_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run["id"], new_version, kind, text, dumps(parsed.tasks), dumps(parsed.requirements),
                     dispatch_id or submitter, now_iso(), digest, run["plan_version"] or None))
        changes = sync_tasks(con, run, parsed.tasks, new_version)
        state.update_run(con, run["id"], plan_version=new_version)
        run = state.get_run(con, run["id"])
        apply_run_checks(con, run, parsed.run_checks)
        run = state.get_run(con, run["id"])
        if dispatch_id:
            planner = state.get_task(con, run["id"], "P1")
            if planner:
                state.update_task(con, run["id"], "P1", status="submitted")
                con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND task_id='P1' AND released_at IS NULL",
                            (now_iso(), run["id"]))
        if new_version > 1:
            from office import amend
            pending = con.execute("SELECT seq FROM amendments WHERE run_id=? AND class='contract' AND to_plan_version IS NULL "
                                  "ORDER BY seq DESC LIMIT 1", (run["id"],)).fetchone()
            amend.contract_from_planner(con, run, f"A{pending['seq']}" if pending else None, changes, new_version)
            if pending:
                con.execute("UPDATE amendments SET to_plan_version=? WHERE run_id=? AND seq=?",
                            (new_version, run["id"], pending["seq"]))
        if parsed.questions:
            state.emit(con, run, "plan.questions", f"PLAN QUESTIONS p{new_version}: " + " | ".join(parsed.questions[:4]),
                       payload={"questions": parsed.questions})
            res.add(f"plan p{new_version} submitted with {len(parsed.questions)} question(s) for the user")
        elif review_required(run) and (not plan_review_ended(con, run) or new_version > 1):
            # After plan review has ended, only contract revisions get a delta review (Q14).
            queue_plan_review(con, run, new_version, escalated=plan_review_ended(con, run))
            res.add(f"plan p{new_version} submitted | plan-review queued")
        else:
            state.emit(con, run, "plan.ready", f"PLAN READY p{new_version}" + ("" if review_required(run) else
                       " (no plan review funded by this gear)"))
            res.add(f"plan p{new_version} submitted" + ("" if review_required(run) else " | no plan review funded"))
        if changes.get("cancelled"):
            res.add(f"removed tasks: {', '.join(changes['cancelled'])}")
    show_diagram(con, state.get_run(con, run["id"]), new_version, parsed.tasks, plan_path, res)
    jobs.kick(con, run["id"])
    for w in parsed.warnings[:3]:
        res.notices.append(w)
    res.next = ("no action; findings will be delivered" if dispatch_id else _after_plan_next(con, state.get_run(con, run["id"])))
    return res


def show_diagram(con, run: dict, version: int, tasks: list[dict], plan_path: Path | None, res: Result) -> None:
    """Route-preview the plan outside the submit transaction (routing probes
    quota), store it, and show the diagram, or only its delta on a revision."""
    from office import plan_view
    pv = plan_view.preview(con, run, tasks)
    with db.transaction(con):
        plan_view.store(con, run, version, pv)
    full = plan_view.render(run, version, pv)
    if plan_path is not None:
        plan_view.write_into(plan_path, version, full)
    prev = plan_view.load(con, run["id"], version - 1) if version > 1 else None
    if prev:
        res.lines.extend(plan_view.diff(prev, pv, version - 1))
        res.lines.append("full diagram: office inspect plan")
    else:
        res.lines.extend(["", *full])
    res.data["diagram"] = pv


def apply_run_checks(con, run: dict, run_checks: list[str]) -> None:
    """Store the latest plan's run-level checks (the newest plan always wins)
    and, if they changed while every task is accepted, retrigger integration
    so the composed result is re-checked against the new checks. Caller holds
    the tx."""
    from office import integration
    landing = dict(run.get("landing") or {})
    old = list(landing.get("run_checks") or [])
    new = list(run_checks)
    landing["run_checks"] = new
    state.update_run(con, run["id"], landing=landing)
    if old == new:
        return
    run = state.get_run(con, run["id"])
    if integration.accepted_set(con, run) is None:
        return
    state.emit(con, run, "integration.recheck", "run checks changed; integration re-check queued")
    integration.retrigger(con, run)


def _apply_requirements(con, run: dict, proposed: dict, submitter: str) -> None:
    cur = state.current_requirements(con, run["id"])
    frozen = dict(cur["frozen"])
    merged = dict(frozen)
    for key in ("done_criteria", "blast_radius", "non_goals", "named_actions", "end_state"):
        if proposed.get(key):
            merged[key] = proposed[key]
    if proposed.get("deploy"):
        merged["deploy"] = {**(frozen.get("deploy") or {}), **proposed["deploy"]}
    if proposed.get("goal"):
        merged["goal"] = proposed["goal"]
    if merged == frozen:
        return
    if state.active_authorization(con, run, "plan"):
        raise Refused("requirements-change", "the plan changes frozen requirements after the user authorized them",
                      scope="requirements", preserved="the authorized plan and requirements",
                      next_step='only the user may change requirements: office amend requirements --quote "<user\'s words>" -- "<change>"')
    version = cur["version"] + 1
    con.execute("INSERT INTO requirements(run_id, version, frozen_json, source, quote, created_at) VALUES(?,?,?,?,?,?)",
                (run["id"], version, dumps(merged), f"planner:{submitter}", None, now_iso()))
    envelope = [{"id": f"X{i + 1}", **a} for i, a in enumerate(merged.get("named_actions") or [])]
    state.update_run(con, run["id"], requirements_version=version, envelope=envelope)


def sync_tasks(con, run: dict, planned: list[dict], plan_version: int) -> dict:
    """Make the tasks table match a plan version. Caller holds the tx."""
    existing = {t["id"]: t for t in state.tasks(con, run["id"])}
    seen = set()
    changes = {"added": [], "contract": [], "acceptance": [], "cancelled": []}
    now = now_iso()
    for p in planned:
        seen.add(p["id"])
        checks = p["checks"] or []
        cur = existing.get(p["id"])
        if cur is None:
            con.execute(
                "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
                "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run["id"], p["id"], p["title"], "executor", dumps(p["scope"]), dumps(p["depends"]),
                 dumps(p["interfaces"]), dumps(p["accept"]), dumps(checks), dumps(p["visual"]) if p["visual"] else None,
                 "planned", plan_version, plan_version, plan_version, now, now))
            changes["added"].append(p["id"])
            continue
        contract = (cur["scope"] != p["scope"] or (cur["interfaces"] or []) != p["interfaces"])
        acceptance = (cur["accept"] != p["accept"] or cur["checks"] != checks or cur["visual"] != p["visual"]
                      or cur["depends"] != p["depends"] or cur["title"] != p["title"])
        fields = dict(title=p["title"], scope=p["scope"], depends=p["depends"], interfaces=p["interfaces"],
                      accept=p["accept"], checks=checks, visual=p["visual"])
        if contract:
            fields["contract_version"] = plan_version
            changes["contract"].append(p["id"])
        if contract or acceptance:
            fields["acceptance_version"] = plan_version
            fields["contract_version"] = plan_version
            if not contract:
                changes["acceptance"].append(p["id"])
        if cur["status"] == "cancelled":
            fields["status"] = "planned"
        state.update_task(con, run["id"], p["id"], **fields)
    for tid, cur in existing.items():
        if tid not in seen and cur["status"] != "cancelled":
            state.update_task(con, run["id"], tid, status="cancelled", pause_reason=f"removed in plan p{plan_version}")
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='task removed' WHERE run_id=? AND task_id=? "
                        "AND released_at IS NULL AND revoked_at IS NULL", (now, run["id"], tid))
            changes["cancelled"].append(tid)
    return changes


# ------------------------------------------------------------------ review state

def review_required(run: dict) -> bool:
    return bool((run.get("plan_review") or {}).get("required"))


def plan_review_ended(con, run: dict) -> bool:
    return bool((state.get_run(con, run["id"]).get("plan_review") or {}).get("ended"))


def plan_gates(con, run_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM gates WHERE run_id=? AND subject=? ORDER BY created_at", (run_id, PLAN_SUBJECT)).fetchall()
    return [dict(r) for r in rows]


def open_defects(con, run_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM findings WHERE run_id=? AND gate_kind='plan_review' AND state='open' "
                       "AND category IN (?,?,?,?,?)", (run_id, *review_parse.DEFECT_CLASSES, "brief")).fetchall()
    return [dict(r) for r in rows]


def review_state(con, run: dict) -> dict:
    run = state.get_run(con, run["id"])
    gates = plan_gates(con, run["id"])
    done = [g for g in gates if g["status"] == "done"]
    pending = [g for g in gates if g["status"] in ("queued", "running")]
    first = done[0] if done else None
    return {
        "required": review_required(run),
        "ended": bool((run.get("plan_review") or {}).get("ended")),
        "ended_reason": (run.get("plan_review") or {}).get("ended_reason"),
        "first_verdict": first["verdict"] if first else None,
        "first_version": first["plan_version"] if first else None,
        "last_verdict": done[-1]["verdict"] if done else None,
        "pending": bool(pending),
        "rounds_used": len([g for g in gates if g["status"] in ("done", "queued", "running") and not g["escalated"]])
                       - _round_base(run),
        "open_defects": open_defects(con, run["id"]),
    }


def _round_base(run: dict) -> int:
    """Rounds spent before the latest defect redirect; a redirect resets the budget."""
    return int((run.get("plan_review") or {}).get("round_base") or 0)


def budget_rounds(con, run: dict) -> int:
    """Non-escalated plan-review rounds counted against plan_review_max_rounds."""
    run = state.get_run(con, run["id"])
    return len([g for g in plan_gates(con, run["id"]) if not g["escalated"]]) - _round_base(run)


def queue_plan_review(con, run: dict, plan_version: int, *, escalated: bool = False, exclude: list[str] | None = None) -> str | None:
    """Queue one plan-review round. Caller holds the tx."""
    if (state.get_run(con, run["id"]).get("plan_review") or {}).get("ended_reason") == "waived by the user":
        return None  # the user waived plan review; no further round runs
    gates = plan_gates(con, run["id"])
    rounds = budget_rounds(con, run)
    maximum = int((run.get("gates") or {}).get("plan_review_max_rounds") or 1)
    if not escalated and rounds >= maximum:
        return None
    if any(g["plan_version"] == plan_version and g["status"] in ("queued", "running") for g in gates):
        return None
    from office import redirect
    payload = {"exclude": list(exclude or [])}
    choice = redirect.take_next_reviewer(con, run)
    if choice and choice["mode"] == "same" and choice.get("dispatch"):
        payload["resume_from"] = choice["dispatch"]
    elif choice and choice.get("route") and choice["route"] not in payload["exclude"]:
        payload["exclude"].append(choice["route"])
    gate_id = "G" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, round, escalated, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (gate_id, run["id"], PLAN_SUBJECT, plan_version, "plan_review", f"plan:{plan_version}", "queued",
                 rounds + 1, 1 if escalated else 0, now_iso()))
    state.enqueue(con, run, "plan_review", {"gate_id": gate_id, "plan_version": plan_version, **payload},
                  dedup_key=f"plan_review:{gate_id}", max_attempts=2)
    return gate_id


# ------------------------------------------------------------------ barriers

def require_dispatchable(con, run: dict) -> None:
    run = state.get_run(con, run["id"])
    if not run["plan_version"]:
        raise Refused("no-plan", "there is no plan to dispatch from",
                      next_step=f"write {planpath.rel(run)} then office submit" if run.get("planner_mode") != "dedicated"
                      else "wait for the planner; office status")
    plan = state.current_plan(con, run["id"])
    if planfile.parse(plan["body"]).questions and not _answered(con, run):
        raise Refused("plan-questions", "the plan has unanswered questions for the user", scope="plan",
                      next_step='relay them to the user, then office amend plan --contract -- "<answers>"')
    if not state.active_authorization(con, run, "plan"):
        raise Refused("authorization-required",
                      f"requirements r{run['requirements_version']} are not authorized by the user", scope="run",
                      preserved="the plan", next_step='ask the user (native question tool) for authorization, then office approve plan --quote "<user\'s words>"')
    rs = review_state(con, run)
    if rs["required"] and not rs["ended"]:
        if rs["first_verdict"] is None:
            raise Refused("plan-review-pending", "the first plan review has not returned", scope="plan",
                          next_step="no action; the verdict will return here (office status)")
        if rs["last_verdict"] == "ATTENTION":
            raise Refused("plan-review-attention", "the plan reviewer answered but left no readable reply file",
                          scope="plan", next_step='re-prompt the reviewer in its pane to write the file, or the user may '
                                                'waive: office approve waive plan-review --quote "<words>"')
        if rs["first_verdict"] == "UNAVAILABLE" and rs["last_verdict"] in (None, "UNAVAILABLE"):
            raise Refused("plan-review-unavailable", "no plan reviewer could review the plan", scope="plan",
                          next_step='resolve the reviewer route, or the user may waive: office approve waive plan-review --quote "<words>"')
        if rs["first_verdict"] == "CHANGES_REQUIRED" and run["plan_version"] <= rs["first_version"]:
            raise Refused("plan-amendment-required", "the first plan review asked for changes that are not yet in the plan",
                          scope="plan", next_step=f'office amend plan -- "<what changed>" (edit {planpath.rel(run)} first for task changes)')
    for d in rs["open_defects"]:
        if not _task_ids_in(d.get("location") or ""):
            raise Refused("plan-defect", f"open plan defect {d['code']}: {d['summary'][:120]}", scope="whole plan",
                          next_step="the planner must fix it; an independent reviewer clears it (office status)")


def require_scope_clear(con, run: dict, task_id: str) -> None:
    for d in open_defects(con, run["id"]):
        ids = _task_ids_in(d.get("location") or "")
        if not ids or task_id in ids:
            raise Refused("plan-defect", f"{task_id} is blocked by open plan defect {d['code']}: {d['summary'][:100]}",
                          scope=task_id, next_step="dispatch unaffected tasks; the defect clears only by independent review")
    task = state.get_task(con, run["id"], task_id)
    if task["status"] == "paused" and task.get("pause_reason", "").startswith("plan defect"):
        raise Refused("plan-defect", f"{task_id} is paused: {task['pause_reason']}", scope=task_id)


def _answered(con, run) -> bool:
    return con.execute("SELECT 1 FROM amendments WHERE run_id=? AND class='contract' AND status='committed'",
                       (run["id"],)).fetchone() is not None


def _task_ids_in(text: str) -> list[str]:
    import re
    return re.findall(r"\bT\d+\b", text or "")


def _after_plan_next(con, run: dict) -> str:
    from office import guide
    return guide.next_action(con, run)


# ------------------------------------------------------------------ plan review job

def job_plan_review(con, run: dict, job: dict) -> dict:
    from office import gates as gate_engine
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (job["payload"]["gate_id"],)).fetchone())
    if gate["status"] not in ("queued", "running"):
        return {"skipped": gate["status"]}
    plan = state.get_plan(con, run["id"], gate["plan_version"])
    req = state.current_requirements(con, run["id"])
    rereview = gate["round"] > 1 or len(plan_gates(con, run["id"])) > 1
    brief = briefs.plan_review_brief(run, plan, req["frozen"], open_defects(con, run["id"]), rereview)
    outcome = gate_engine.run_reviewer(con, run, gate, "plan_reviewer", brief, cwd=Path(run["repo_root"]),
                                       plan_review=True, exclude=job["payload"].get("exclude"),
                                       resume_from=job["payload"].get("resume_from"))
    with db.transaction(con):
        ingest_plan_review(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome.get("verdict")}


def ingest_plan_review(con, run: dict, gate_id: str, outcome: dict) -> None:
    """Record a plan-review result. Caller holds the tx."""
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
    verdict = outcome["verdict"]
    con.execute("UPDATE gates SET status='done', verdict=?, route=?, finished_at=?, summary=? WHERE id=?",
                (verdict, outcome.get("route"), now_iso(), outcome.get("summary"), gate_id))
    parsed: review_parse.Parsed | None = outcome.get("parsed")
    reviewer = outcome.get("dispatch_id")
    if parsed is not None:
        for code in parsed.cleared:
            _clear_defect(con, run, code, gate)
        for f in parsed.findings:
            _record_plan_finding(con, run, gate, f, reviewer, defect=False)
        for d in parsed.defects:
            _record_plan_finding(con, run, gate, d, reviewer, defect=True)
    pr = dict(run.get("plan_review") or {})
    if verdict == "PASS":
        pr.update({"ended": True, "ended_reason": f"PASS on p{gate['plan_version']}"})
        state.emit(con, run, "plan.pass", f"PLAN PASS p{gate['plan_version']}")
    elif verdict == "CHANGES_REQUIRED":
        codes = ", ".join(f["code"] for f in (parsed.findings if parsed else []) if f["severity"] == "material")
        state.emit(con, run, "plan.changes_required",
                   f"PLAN CHANGES_REQUIRED p{gate['plan_version']}: {codes or 'see findings'}",
                   payload={"findings": [f for f in (parsed.findings if parsed else [])]})
    elif verdict in ("PLAN_DEFECT", "BRIEF_DEFECT"):
        paused = pause_for_defects(con, run)
        state.emit(con, run, "plan.defect", f"PLAN {verdict} p{gate['plan_version']}"
                   + (f"; paused {', '.join(paused)}" if paused else ""))
    elif verdict == "ATTENTION":
        # The plan reviewer answered but left no readable reply file after
        # re-prompting; its pane is kept and the orchestrator decides (R11).
        state.emit(con, run, "plan.attention", f"PLAN REVIEW p{gate['plan_version']} needs attention: "
                   f"{outcome.get('summary', '')[:240]}")
        state.update_run(con, run["id"], plan_review=pr)
        return
    else:
        state.emit(con, run, "plan.unavailable", f"PLAN REVIEW UNAVAILABLE p{gate['plan_version']}: "
                   f"{outcome.get('summary') or 'no qualifying reviewer answered'}")
    rounds = budget_rounds(con, run)
    maximum = int((run.get("gates") or {}).get("plan_review_max_rounds") or 1)
    if verdict != "PASS" and rounds >= maximum and not pr.get("ended"):
        if open_defects(con, run["id"]) and not run.get("escalations_used"):
            state.update_run(con, run["id"], escalations_used=1)
            queue_plan_review(con, run, state.get_run(con, run["id"])["plan_version"], escalated=True,
                              exclude=[outcome.get("route")] if outcome.get("route") else None)
            state.emit(con, run, "plan.escalated", "plan review budget spent with an open defect; one escalation queued")
        elif not open_defects(con, run["id"]):
            pr.update({"ended": True, "ended_reason": "round budget spent"})
    state.update_run(con, run["id"], plan_review=pr)


def _record_plan_finding(con, run, gate, f, reviewer, *, defect: bool) -> None:
    fid = "F" + uuid.uuid4().hex[:10]
    category = f.get("category") if defect else "plan"
    if defect and gate.get("kind") == "plan_review" and f.get("category") not in review_parse.DEFECT_CLASSES:
        category = "brief"
    existing = con.execute("SELECT id FROM findings WHERE run_id=? AND gate_kind='plan_review' AND code=? AND state='open'",
                           (run["id"], f["code"])).fetchone()
    if existing:
        con.execute("UPDATE findings SET summary=?, location=?, updated_at=?, gate_id=? WHERE id=?",
                    (f["summary"], f.get("location"), now_iso(), gate["id"], existing["id"]))
        return
    con.execute("INSERT INTO findings(id, dispatch_id, reviewer_dispatch_id, status, severity, summary, evidence_hash, created_at, "
                "run_id, gate_id, gate_kind, code, location, category, action, state, origin_gate_id, evidence, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, reviewer, reviewer, "open", f.get("severity", "material"), f["summary"], None, now_iso(),
                 run["id"], gate["id"], "plan_review", f["code"], f.get("location"), category, f.get("action"),
                 "open" if (defect or f.get("severity") == "material") else "minor", gate["id"], f.get("evidence"), now_iso()))


def _clear_defect(con, run, code: str, gate: dict) -> None:
    row = con.execute("SELECT f.id, g.plan_version AS origin FROM findings f JOIN gates g ON g.id=f.origin_gate_id "
                      "WHERE f.run_id=? AND f.gate_kind='plan_review' AND f.code=? AND f.state='open'",
                      (run["id"], code)).fetchone()
    if row is None:
        return
    if gate["plan_version"] <= row["origin"]:
        return  # clearance must come from a revision that contains a fix
    con.execute("UPDATE findings SET state='resolved', updated_at=? WHERE id=?", (now_iso(), row["id"]))
    state.emit(con, run, "plan.defect_cleared", f"plan defect {code} cleared by independent review on p{gate['plan_version']}")
    unpause_cleared(con, run)


def pause_for_defects(con, run: dict) -> list[str]:
    """Pause tasks named by open defects plus their dependants; a plan-wide
    defect pauses every unaccepted task. Caller holds the tx."""
    all_tasks = state.tasks(con, run["id"])
    graph = {t["id"]: t["depends"] for t in all_tasks}
    targets: set[str] = set()
    for d in open_defects(con, run["id"]):
        ids = _task_ids_in(d.get("location") or "")
        if not ids:
            targets |= {t["id"] for t in all_tasks}
        for tid in ids:
            targets.add(tid)
            targets |= planfile.dependants(graph, tid)
    paused = []
    for t in all_tasks:
        if t["id"] in targets and t["status"] not in ("accepted", "cancelled", "planned", "paused"):
            state.update_task(con, run["id"], t["id"], status="paused", pause_reason="plan defect")
            paused.append(t["id"])
    return paused


def unpause_cleared(con, run: dict) -> None:
    if open_defects(con, run["id"]):
        return
    for t in state.tasks(con, run["id"]):
        if t["status"] == "paused" and t.get("pause_reason") == "plan defect":
            # Back to where it was: a submitted revision still has gates to finish.
            from office import gates
            state.update_task(con, run["id"], t["id"], status=gates.derive_status(con, run, t), pause_reason=None)
