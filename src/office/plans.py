"""Plans, plan review, and the dispatch barrier.

Convergence contract (#337, office.contract), plan-review lifecycle (#418):

  Plan review reviews the initial plan. The cycle is open from the first
  submit until it closes; only an open cycle queues a reviewer
  (`can_auto_queue`). Once closed, no amendment, resume or late result
  reopens it: the planner/orchestrator owns every later revision. Only the
  user reopens review, explicitly (`office review plan --quote ...`), as a
  new bounded cycle recorded as user-requested.

  APPROVED    dispatch-safe: the cycle closes and every eligible executor may
              fan out. Its findings stay tracked until the orchestrator or
              planner fixes or dispositions them, without another review.
  RECHECK     the planner revises; tasks the blocking findings name (and their
              dependants) wait, plan-wide findings hold every task, and the
              same reviewer (when available) reviews the revision. At the
              round cap (3 substantive rounds unless the user chose another
              at start) the cycle closes unapproved: the orchestrator owns the
              outstanding findings and records a disposition for each.
  INTAKE_GAP  the named user decision is asked at once; what it affects waits.
  Reviewer unavailability or an unreadable reply is runtime status, never a
  verdict, and spends no round.

v3.1 contract — fork after the first plan-review verdict
(docs/v31-rolling-review-gates.md §2):

  PASS              -> launch ready executors; plan review ends.
  CHANGES_REQUIRED  -> orchestrator amends; eligible executors launch at once,
                       while the amended plan is re-reviewed concurrently.
  PLAN/BRIEF_DEFECT -> nothing launches in the affected scope until an
                       independent reviewer clears the defect by id.
  UNAVAILABLE       -> nothing launches until a substitute reviewer answers
                       or the user decides.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from office import briefs, candidates, contract, db, jobs, planfile, planpath, review_parse, routing, state
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
    planfile.grandfather_entries(parsed, (state.current_plan(con, run["id"]) or {}).get("tasks"))
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
        prior_graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
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
            amend.contract_from_planner(con, run, f"A{pending['seq']}" if pending else None, changes, new_version,
                                         prior_graph)
            if pending:
                con.execute("UPDATE amendments SET to_plan_version=? WHERE run_id=? AND seq=?",
                            (new_version, run["id"], pending["seq"]))
        if parsed.questions:
            state.emit(con, run, "plan.questions", f"PLAN QUESTIONS p{new_version}: " + " | ".join(parsed.questions[:4]),
                       payload={"questions": parsed.questions})
            res.add(f"plan p{new_version} submitted with {len(parsed.questions)} question(s) for the user")
        elif contract.is_convergence(run):
            res.add(f"plan p{new_version} submitted | " + review_after_revision(con, run, new_version))
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
    for tid, entry in pv["tasks"].items():
        problem = (entry.get("route_plan") or {}).get("planner_error")
        if problem:
            res.notices.append(f"{tid} route: {problem}; the ranked slate stands")
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


def sync_tasks(con, run: dict, planned: list[dict], plan_version: int, *, rerun_checks: bool = False) -> dict:
    """Make the tasks table match a plan version. Caller holds the tx.

    With `rerun_checks`, an accepted task whose only change is its `checks:` keeps its contract version and
    is reported under `checks_only` instead of `acceptance`: it is not reopened, its checks are rerun
    (`rerun_accepted_checks`)."""
    existing = {t["id"]: t for t in state.tasks(con, run["id"])}
    seen = set()
    changes = {"added": [], "contract": [], "acceptance": [], "checks_only": [], "cancelled": []}
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
            if p.get("descriptor"):
                state.update_task(con, run["id"], p["id"], descriptor=p["descriptor"])
            changes["added"].append(p["id"])
            continue
        contract = (cur["scope"] != p["scope"] or (cur["interfaces"] or []) != p["interfaces"])
        checks_changed = cur["checks"] != checks
        other_acceptance = (cur["accept"] != p["accept"] or cur["visual"] != p["visual"]
                            or cur["depends"] != p["depends"] or cur["title"] != p["title"])
        acceptance = checks_changed or other_acceptance
        checks_only = (rerun_checks and checks_changed and not (contract or other_acceptance)
                       and cur["status"] == "accepted" and cur["accepted_revision_id"]
                       and cur["accepted_revision_id"] == cur["current_revision_id"])
        fields = dict(title=p["title"], scope=p["scope"], depends=p["depends"], interfaces=p["interfaces"],
                      accept=p["accept"], checks=checks, visual=p["visual"], descriptor=p.get("descriptor") or {})
        if contract:
            fields["contract_version"] = plan_version
            changes["contract"].append(p["id"])
        if checks_only:
            fields["acceptance_version"] = plan_version
            changes["checks_only"].append(p["id"])
        elif contract or acceptance:
            fields["acceptance_version"] = plan_version
            fields["contract_version"] = plan_version
            if not contract:
                changes["acceptance"].append(p["id"])
        if cur["status"] == "cancelled":
            fields["status"] = "planned"
        if cur["scope"] and not p["scope"]:
            from office import prs
            blocker = prs.pr_blocker(con, run, cur)
            if blocker:
                # A PR-free task is never synced or landed, so its PR would be orphaned.
                raise Refused("scope-has-pr", f"{p['id']} cannot change to `scope: none`: {blocker}",
                              scope=p["id"], preserved="plan unchanged",
                              next_step=f"resolve {p['id']}'s PR (merge or close it), keep {p['id']}'s file scope, "
                                        "and put the comment-only work in a new task with `scope: none`")
        state.update_task(con, run["id"], p["id"], **fields)
    for tid, cur in existing.items():
        if tid not in seen and cur["status"] != "cancelled":
            state.update_task(con, run["id"], tid, status="cancelled", pause_reason=f"removed in plan p{plan_version}")
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='task removed' WHERE run_id=? AND task_id=? "
                        "AND released_at IS NULL AND revoked_at IS NULL", (now, run["id"], tid))
            changes["cancelled"].append(tid)
    return changes


def rerun_accepted_checks(con, run: dict, task_id: str, amendment_id: str) -> bool:
    """Run an accepted task's (amended) checks again on its accepted revision. The task keeps its status
    until a verdict: a pass leaves it accepted, a failure delivers the finding and reopens it. No executor
    is launched. Returns whether a checks gate was queued. Caller holds the tx."""
    from office import gates
    task = state.get_task(con, run["id"], task_id)
    rev_id = task["current_revision_id"]
    if not task["checks"]:
        state.emit(con, run, "gate.rerun", f"{task_id} stays accepted on {rev_id}: {amendment_id} left it no checks to run",
                   task_id=task_id)
        return False
    gid = gates._new_gate(con, run, task, rev_id, "checks", f"checks:{rev_id}", "queued")
    state.enqueue(con, run, "run_checks", {"gate_id": gid, "task_id": task_id, "revision_id": rev_id},
                  dedup_key=f"checks:{gid}", max_attempts=2)
    state.emit(con, run, "gate.rerun", f"{task_id} checks re-run on {rev_id} ({amendment_id} changed them; no relaunch)",
               task_id=task_id)
    return True


# ------------------------------------------------------------------ review state

def review_required(run: dict) -> bool:
    return bool((run.get("plan_review") or {}).get("required"))


def plan_review_ended(con, run: dict) -> bool:
    return bool((state.get_run(con, run["id"]).get("plan_review") or {}).get("ended"))


def plan_gates(con, run_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM gates WHERE run_id=? AND subject=? ORDER BY created_at", (run_id, PLAN_SUBJECT)).fetchall()
    return [dict(r) for r in rows]


def open_defects(con, run_id: str) -> list[dict]:
    """v3.1 plan defects. Convergence-contract findings carry no defect class."""
    rows = con.execute("SELECT * FROM findings WHERE run_id=? AND gate_kind='plan_review' AND state='open' "
                       "AND COALESCE(contract, 'v3.1')='v3.1' AND category IN (?,?,?,?,?)",
                       (run_id, *review_parse.DEFECT_CLASSES, "brief")).fetchall()
    return [dict(r) for r in rows]


def review_state(con, run: dict) -> dict:
    run = state.get_run(con, run["id"])
    if contract.is_convergence(run):
        return _review_state_convergence(con, run)
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
    if contract.is_convergence(run):
        return _queue_convergence_review(con, run, plan_version, exclude=exclude)
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
    if contract.is_convergence(run):
        _require_dispatchable_convergence(con, run)
        return
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
    if contract.is_convergence(run):
        for f in blocking_findings(con, run):
            ids = _task_ids_in(f.get("location") or "")
            if not ids or task_id in _with_dependants(con, run, ids):
                raise Refused("plan-recheck", f"{task_id} waits on plan finding {f['code']}: {f['summary'][:100]}",
                              scope=task_id, next_step="dispatch unaffected tasks; the plan revision is reviewed again")
        task = state.get_task(con, run["id"], task_id)
        if task["status"] == "paused" and (task.get("pause_reason") or "").startswith("plan "):
            raise Refused("plan-recheck", f"{task_id} is paused: {task['pause_reason']}", scope=task_id)
        return
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
    if contract.is_convergence(run):
        carried = [f for f in blocking_findings(con, run) if f["code"] != "INTAKE_GAP"]
        brief = briefs.plan_review_brief(run, plan, req["frozen"], [], rereview, carried=carried, round_no=gate["round"],
                                         max_rounds=round_cap(run))
    else:
        brief = briefs.plan_review_brief(run, plan, req["frozen"], open_defects(con, run["id"]), rereview)
    outcome = gate_engine.run_reviewer(con, run, gate, "plan_reviewer", brief, cwd=Path(run["repo_root"]),
                                       plan_review=True, exclude=job["payload"].get("exclude"),
                                       resume_from=job["payload"].get("resume_from"))
    with db.transaction(con):
        ingest_plan_review(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome.get("verdict")}


def ingest_plan_review(con, run: dict, gate_id: str, outcome: dict) -> None:
    """Record a plan-review result. Caller holds the tx."""
    from office import gates as gate_engine
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
    if gate_engine._already_decided(con, run, gate, outcome):
        return  # one round, one result (#403)
    if contract.is_convergence(run) and gate["status"] == "cancelled":
        # The cycle closed (or was waived) while this round ran: its result is
        # kept as audit evidence and reopens nothing (#418).
        state.emit(con, run, "plan.late_result", f"plan-review round {gate['id']} on p{gate['plan_version']} was "
                   f"cancelled ({gate.get('stale_reason') or 'closed'}); its "
                   f"{outcome.get('verdict') or outcome.get('status') or 'result'} was not applied", audience="runtime",
                   payload={"gate": gate["id"], "verdict": outcome.get("verdict"), "status": outcome.get("status"),
                            "summary": (outcome.get("summary") or "")[:500], "route": outcome.get("route")})
        return
    if contract.is_convergence(run):
        _ingest_convergence(con, run, gate_id, outcome)
        return
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
        if defect:
            # A DEFECT line that reuses an open finding's code makes it a defect;
            # left as a plain finding, --redirect and waive could not name it.
            con.execute("UPDATE findings SET category=?, severity='material', evidence=?, action=? WHERE id=?",
                        (category, f.get("evidence"), f.get("action"), existing["id"]))
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


# ------------------------------------------------------------------ convergence contract (#337)

PLAN_SCOPE = "plan"

# How a plan-review cycle opened (#418): at the first submit, or by the user's
# explicit `office review plan` once an earlier cycle closed.
INITIAL = "initial"
USER_REQUESTED = "user-requested"

# How a cycle closed. Every outcome is final: only the user reopens review.
APPROVED = "approved"
OWNED = "budget_exhausted_orchestrator_owned"
WAIVED = "waived"

# The bounds a user-chosen round cap must fall in (`office start
# --plan-review-rounds`, `office review plan --rounds`).
ROUND_CAP_LIMIT = 10


def _pr(run: dict) -> dict:
    return dict(run.get("plan_review") or {})


def _cycle(run: dict) -> int:
    return int(_pr(run).get("cycle") or 1)


def round_cap(run: dict) -> int:
    """Substantive rounds the current cycle may spend: a user-requested cycle's
    own budget, else the run's pinned initial cap (3 unless the user chose
    another at office start)."""
    return int(_pr(run).get("max_rounds") or (run.get("gates") or {}).get("plan_review_max_rounds")
               or contract.MAX_ROUNDS)


def check_round_cap(rounds: int) -> int:
    if not 1 <= int(rounds) <= ROUND_CAP_LIMIT:
        raise Usage("bad-rounds", f"plan-review rounds must be 1 to {ROUND_CAP_LIMIT} (got {rounds})")
    return int(rounds)


def can_auto_queue(run: dict) -> bool:
    """The one rule for queueing a plan reviewer (#418): the run funds plan
    review and its current cycle is open. The initial cycle opens with the
    run; once a cycle closes, no amendment, resume or late result reopens it.
    Only the user's explicit `office review plan` opens another."""
    return review_required(run) and not _pr(run).get("ended")


def owns_outstanding(run: dict) -> bool:
    """Whether the last cycle closed at its round cap, unapproved, leaving its
    blocking findings to the orchestrator."""
    return _pr(run).get("status") == OWNED


def close_cycle(con, run: dict, pr: dict, outcome: str, reason: str) -> dict:
    """Close the open cycle for good. A round still queued or running for it is
    cancelled; a result it returns later is kept as audit evidence and applied
    to nothing (`plan.late_result`). Caller holds the tx and saves `pr`."""
    capped = {**run, "plan_review": pr}
    pr.update({"ended": True, "ended_reason": reason, "status": outcome})
    pr.setdefault("history", []).append({
        "cycle": _cycle(capped), "lifecycle": pr.get("lifecycle") or INITIAL, "outcome": outcome,
        "rounds": substantive_rounds(con, capped), "max_rounds": round_cap(capped), "at": now_iso()})
    con.execute("UPDATE gates SET status='cancelled', stale_reason=? WHERE run_id=? AND kind='plan_review' "
                "AND status IN ('queued','running')", (f"plan review closed ({outcome})", run["id"]))
    return pr


def _cycle_gates(con, run: dict) -> list[dict]:
    return [g for g in plan_gates(con, run["id"]) if int(g.get("cycle") or 1) == _cycle(run)]


def substantive_rounds(con, run: dict) -> int:
    """Completed reviews in the current cycle. Unavailable routes, unreadable
    replies and other runtime failures never count (#337)."""
    return len([g for g in _cycle_gates(con, run) if g["status"] == "done" and g.get("review_status") == contract.COMPLETED])


def blocking_findings(con, run: dict) -> list[dict]:
    """What holds dispatch: open blocking plan findings, and an open intake gap
    (as a pseudo-finding naming what it affects)."""
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM findings WHERE run_id=? AND gate_kind='plan_review' AND state='open' AND contract=? "
        "ORDER BY created_at", (run["id"], contract.CONVERGENCE)).fetchall()]
    gap = _pr(state.get_run(con, run["id"])).get("intake_gap")
    if gap:
        rows.append({"code": "INTAKE_GAP", "location": gap.get("affects") or "", "summary": gap.get("decision") or "",
                     "blocking": 1})
    return rows


def held_tasks(con, run: dict) -> set[str]:
    """Tasks a blocking plan finding or intake gap holds (with dependants); every
    task when one names no task."""
    out: set[str] = set()
    for f in blocking_findings(con, run):
        ids = _task_ids_in(f.get("location") or "")
        out |= _with_dependants(con, run, ids) if ids else {t["id"] for t in state.tasks(con, run["id"])}
    return out


def _with_dependants(con, run: dict, ids: list[str]) -> set[str]:
    graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
    out = set(ids)
    for tid in ids:
        out |= planfile.dependants(graph, tid)
    return out


def _review_state_convergence(con, run: dict) -> dict:
    gates = plan_gates(con, run["id"])
    done = [g for g in gates if g["status"] == "done"]
    completed = [g for g in done if g.get("review_status") == contract.COMPLETED]
    pr = _pr(run)
    history = pr.get("history") or []
    return {
        "contract": contract.CONVERGENCE,
        "required": review_required(run),
        "ended": bool(pr.get("ended")),
        "ended_reason": pr.get("ended_reason"),
        "status": pr.get("status") or ("pending" if review_required(run) else "not-required"),
        "lifecycle": pr.get("lifecycle") or INITIAL,
        "initial": next((h for h in history if h.get("lifecycle") == INITIAL), None),
        "history": history,
        "max_rounds": round_cap(run),
        "rounds_by": (run.get("gates") or {}).get("plan_review_rounds_by") or "default",
        "first_verdict": completed[0]["verdict"] if completed else None,
        "first_version": completed[0]["plan_version"] if completed else None,
        "last_verdict": completed[-1]["verdict"] if completed else None,
        "last_status": done[-1].get("review_status") if done else None,
        "pending": any(g["status"] in ("queued", "running") for g in gates),
        "rounds_used": substantive_rounds(con, run),
        "cycle": _cycle(run),
        "open_defects": [],
        "blocking": blocking_findings(con, run),
        "nonblocking": [dict(r) for r in con.execute(
            "SELECT * FROM findings WHERE run_id=? AND gate_kind='plan_review' AND state='nonblocking' AND "
            "disposition IS NULL ORDER BY created_at", (run["id"],)).fetchall()],
        "intake_gap": pr.get("intake_gap"),
        "outstanding": pr.get("outstanding"),
    }


def review_after_revision(con, run: dict, version: int) -> str:
    """What a plan revision gets under the convergence contract. Caller holds
    the tx. Returns a short line for the submitter. Only an open cycle reviews
    it (`can_auto_queue`); after the cycle closes, the planner/orchestrator owns
    every revision, whatever it changes (#418)."""
    run = state.get_run(con, run["id"])
    if not review_required(run):
        state.emit(con, run, "plan.ready", f"PLAN READY p{version} (no plan review funded by this gear)")
        return "no plan review funded"
    pr = _pr(run)
    if pr.get("ended"):
        state.emit(con, run, "plan.review_closed", f"plan p{version}: plan review is closed "
                   f"({pr.get('ended_reason') or pr.get('status')}); the planner/orchestrator owns this revision, "
                   "no re-review", audience="runtime")
        return "plan review closed (owned by the planner/orchestrator; no re-review)"
    if pr.get("intake_gap") and not pr.get("intake_answered"):
        # The user's decision arrived as this revision: a fresh round budget reviews it.
        pr.update({"cycle": _cycle(run) + 1, "intake_answered": True})
        state.update_run(con, run["id"], plan_review=pr)
    gid = queue_plan_review(con, state.get_run(con, run["id"]), version)
    return "plan-review queued" if gid else "plan review not queued"


def request_review(con, run: dict, quote: str | None, rounds: int | None = None) -> Result:
    """The user's explicit request for another plan review after a cycle closed
    (#418): a new bounded cycle, recorded as user-requested with their words.
    It is the only way plan review reopens."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-request-review", "only the user requests another plan review")
    if not contract.is_convergence(run):
        raise Refused("legacy-contract", f"this run keeps the {contract.of(run)} review contract; it has no "
                      "user-requested plan review", next_step="office status")
    if not quote or len(re.sub(r"\s+", "", quote)) < 2:
        raise Usage("user-quote-required", "another plan review is the user's request; record their words",
                    next_step='office review plan --quote "<user\'s words>" [--rounds N]')
    rounds = check_round_cap(contract.MAX_ROUNDS if rounds is None else rounds)
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        if not run["plan_version"]:
            raise Refused("no-plan", "there is no plan to review", next_step="office submit the plan first")
        pr = _pr(run)
        if review_required(run) and not pr.get("ended"):
            raise Refused("plan-review-open", f"plan review is already open ({pr.get('status') or 'pending'}, "
                          f"cycle {_cycle(run)})", scope="plan", next_step="office status")
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, authorized_by, quote, "
                    "created_at) VALUES(?,?,?,?,?,?,?,?)", ("Z" + uuid.uuid4().hex[:8], run["id"], "decision",
                                                            "plan:review", run["requirements_version"], "user",
                                                            quote.strip(), now_iso()))
        cycle = _cycle(run) + (1 if plan_gates(con, run["id"]) else 0)
        version = run["plan_version"]
        pr.update({"required": True, "ended": False, "ended_reason": None, "status": "pending", "cycle": cycle,
                   "lifecycle": USER_REQUESTED, "max_rounds": rounds, "outstanding": None,
                   "requested": {"quote": quote.strip(), "plan_version": version, "at": now_iso()}})
        state.update_run(con, run["id"], plan_review=pr)
        run = state.get_run(con, run["id"])
        queue_plan_review(con, run, version)
        state.emit(con, run, "plan.review_requested", f"user-requested plan review of p{version}: cycle {cycle}, "
                   f"up to {rounds} substantive round(s)", payload={"quote": quote.strip(), "rounds": rounds})
    jobs.kick(con, run["id"])
    return Result(lines=[f"plan review requested by the user: p{version}, cycle {cycle}, up to {rounds} round(s)"],
                  next="no action; the verdict returns here (office status)")


def recheck_reviewer(con, run: dict) -> str | None:
    """The reviewer dispatch a plan RECHECK sequence continues with (#337)."""
    prev = [g for g in _cycle_gates(con, run) if g.get("reviewer_dispatch_id") and g.get("review_status") == contract.COMPLETED]
    return prev[-1]["reviewer_dispatch_id"] if prev else None


def _queue_convergence_review(con, run: dict, plan_version: int, *, exclude: list[str] | None = None) -> str | None:
    run = state.get_run(con, run["id"])
    if not can_auto_queue(run):
        return None  # the cycle is closed: only the user reopens plan review (#418)
    gates = plan_gates(con, run["id"])
    if any(g["plan_version"] == plan_version and g["status"] in ("queued", "running") for g in gates):
        return None
    pr = _pr(run)
    rounds = substantive_rounds(con, run)
    if rounds >= round_cap(run):
        return None
    payload = {"exclude": list(exclude or [])}
    # Same reviewer across a RECHECK sequence when it is still available (#337).
    prev = recheck_reviewer(con, run)
    if prev and not payload["exclude"]:
        payload["resume_from"] = prev
        from office import gates as gate_engine
        gate_engine.recheck_continuity(con, run, prev)  # a fallback is announced before the round runs
    gate_id = "G" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, round, escalated, created_at, "
                "contract, cycle) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (gate_id, run["id"], PLAN_SUBJECT, plan_version, "plan_review", f"plan:{plan_version}", "queued",
                 rounds + 1, 0, now_iso(), contract.CONVERGENCE, _cycle(run)))
    state.enqueue(con, run, "plan_review", {"gate_id": gate_id, "plan_version": plan_version, **payload},
                  dedup_key=f"plan_review:{gate_id}", max_attempts=2)
    if pr.get("status") in (None, "unavailable", "attention"):
        pr["status"] = "pending"
        state.update_run(con, run["id"], plan_review=pr)
    return gate_id


def _require_dispatchable_convergence(con, run: dict) -> None:
    rs = review_state(con, run)
    if not rs["required"] or rs["ended"]:
        return
    if rs["status"] == "unavailable":
        raise Refused("plan-review-unavailable", "no plan reviewer route could review the plan (runtime status, not a "
                      "verdict)", scope="plan", next_step='office resume retries the reviewer chain; or the user may '
                      'waive: office approve waive plan-review --quote "<words>"')
    if rs["status"] == "attention":
        raise Refused("plan-review-attention", "the plan reviewer answered but left no readable reply file",
                      scope="plan", next_step='re-prompt the reviewer in its pane, office resume, or the user may waive: '
                                            'office approve waive plan-review --quote "<words>"')
    if rs["first_verdict"] is None:
        raise Refused("plan-review-pending", "the first plan review has not returned", scope="plan",
                      next_step="no action; the verdict will return here (office status)")
    whole = [f for f in rs["blocking"] if not _task_ids_in(f.get("location") or "")]
    if whole:
        f = whole[0]
        what = "intake gap" if f["code"] == "INTAKE_GAP" else f"blocking plan finding {f['code']}"
        raise Refused("plan-recheck", f"{what} holds the whole plan: {f['summary'][:120]}", scope="whole plan",
                      next_step=_after_plan_next(con, run))


def _plan_finding(con, run: dict, gate: dict, f: dict, reviewer: str | None, *, state_: str) -> None:
    existing = con.execute("SELECT id FROM findings WHERE run_id=? AND gate_kind='plan_review' AND code=? "
                           "AND state IN ('open','nonblocking') AND contract=?",
                           (run["id"], f["code"], contract.CONVERGENCE)).fetchone()
    cols = dict(summary=f["summary"], location=f.get("location"), action=f.get("action"), level=f.get("level"),
                severity=f.get("severity"), blocking=int(bool(f.get("blocking"))), seam=f.get("seam"),
                root_cause=f.get("root_cause"), state=state_, gate_id=gate["id"], updated_at=now_iso())
    if existing:
        sets = ", ".join(f"{k}=?" for k in cols)
        con.execute(f"UPDATE findings SET {sets} WHERE id=?", (*cols.values(), existing["id"]))
        return
    con.execute("INSERT INTO findings(id, dispatch_id, reviewer_dispatch_id, status, severity, summary, created_at, run_id, "
                "gate_id, gate_kind, code, location, category, action, state, origin_gate_id, updated_at, level, contract, "
                "scope, blocking, seam, root_cause) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("F" + uuid.uuid4().hex[:10], reviewer, reviewer, "open", f.get("severity"), f["summary"], now_iso(),
                 run["id"], gate["id"], "plan_review", f["code"], f.get("location"), "plan", f.get("action"), state_,
                 gate["id"], now_iso(), f.get("level"), contract.CONVERGENCE, PLAN_SCOPE, int(bool(f.get("blocking"))),
                 f.get("seam"), f.get("root_cause")))


def _pause_affected(con, run: dict, findings: list[dict], reason: str) -> list[str]:
    """Pause running work that a blocking plan finding names (plus dependants);
    a plan-wide one pauses every unaccepted task. Caller holds the tx."""
    targets: set[str] = set()
    all_ids = [t["id"] for t in state.tasks(con, run["id"])]
    for f in findings:
        ids = _task_ids_in(f.get("location") or "")
        targets |= _with_dependants(con, run, ids) if ids else set(all_ids)
    paused = []
    for t in state.tasks(con, run["id"]):
        if t["id"] in targets and t["status"] not in ("accepted", "cancelled", "planned", "paused"):
            state.update_task(con, run["id"], t["id"], status="paused", pause_reason=reason)
            paused.append(t["id"])
    return paused


def _unpause_plan_holds(con, run: dict) -> None:
    from office import gates
    for t in state.tasks(con, run["id"]):
        if t["status"] == "paused" and (t.get("pause_reason") or "").startswith("plan "):
            state.update_task(con, run["id"], t["id"], status=gates.derive_status(con, run, t), pause_reason=None)
    # Revisions whose checks ended while the plan held them are accepted now.
    gates.reevaluate_submitted(con, run)


def _ingest_convergence(con, run: dict, gate_id: str, outcome: dict) -> None:
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
    status = outcome.get("status") or contract.UNAVAILABLE
    verdict = outcome.get("verdict") if status == contract.COMPLETED else None
    parsed: review_parse.Parsed | None = outcome.get("parsed")
    reviewer = outcome.get("dispatch_id")
    con.execute("UPDATE gates SET status='done', verdict=?, review_status=?, route=?, finished_at=?, summary=?, "
                "reviewer_dispatch_id=?, independence=?, next_action=?, contract=? WHERE id=?",
                (verdict, status, outcome.get("route"), now_iso(), outcome.get("summary"), reviewer, contract.INDEPENDENT,
                 parsed.next_action if parsed else None, contract.CONVERGENCE, gate_id))
    pr = _pr(run)
    v = f"p{gate['plan_version']}"
    if status != contract.COMPLETED:
        # Runtime status, not a verdict: no round is spent.
        pr["status"] = "attention" if status == contract.INVALID_RESULT else "unavailable"
        state.update_run(con, run["id"], plan_review=pr)
        kind = "plan.attention" if status == contract.INVALID_RESULT else "plan.unavailable"
        state.emit(con, run, kind, f"PLAN REVIEW {status} {v} (runtime status, not a verdict; no round spent): "
                   f"{(outcome.get('summary') or 'no qualifying reviewer answered')[:240]}")
        return
    for code in parsed.resolved:
        con.execute("UPDATE findings SET state='resolved', updated_at=? WHERE run_id=? AND gate_kind='plan_review' AND code=? "
                    "AND state='open'", (now_iso(), run["id"], code))
    for r in parsed.retracted:
        con.execute("UPDATE findings SET state='retracted', updated_at=? WHERE run_id=? AND gate_kind='plan_review' "
                    "AND code=? AND state IN ('open','nonblocking')", (now_iso(), run["id"], r["code"]))
    restated = {f["code"] for f in parsed.findings}
    for f in parsed.findings:
        _plan_finding(con, run, gate, f, reviewer, state_="open" if f.get("blocking") else "nonblocking")
    # A completed review supersedes earlier blocking findings it did not restate.
    for row in con.execute("SELECT id, code FROM findings WHERE run_id=? AND gate_kind='plan_review' AND state='open' "
                           "AND contract=?", (run["id"], contract.CONVERGENCE)).fetchall():
        if row["code"] not in restated:
            con.execute("UPDATE findings SET state='resolved', updated_at=? WHERE id=?", (now_iso(), row["id"]))
    pr.pop("intake_gap", None)
    pr.pop("intake_answered", None)
    pr["reviewer"] = reviewer
    nonblocking = [f for f in parsed.findings if not f.get("blocking")]
    label = " (user-requested review)" if pr.get("lifecycle") == USER_REQUESTED else ""
    if verdict == "APPROVED":
        close_cycle(con, run, pr, APPROVED, f"APPROVED on {v}{label}")
        pr["outstanding"] = None
        state.update_run(con, run["id"], plan_review=pr)
        _unpause_plan_holds(con, run)
        state.emit(con, run, "plan.approved", f"PLAN APPROVED {v}{label}: dispatch-safe; plan review is closed"
                   + (f"; {len(nonblocking)} non-blocking finding(s) to fix or disposition (no re-review): "
                      + ", ".join(f["code"] for f in nonblocking) if nonblocking else "")
                   + (f"; reviewer recommends: {parsed.next_action[:160]}" if parsed.next_action else ""),
                   payload={"findings": parsed.findings, "next_action": parsed.next_action})
        return
    if verdict == "INTAKE_GAP":
        pr.update({"status": "intake_gap", "intake_gap": {"decision": parsed.decision, "why": parsed.why,
                                                          "affects": parsed.affects, "gate": gate_id, "version": v}})
        state.update_run(con, run["id"], plan_review=pr)
        paused = _pause_affected(con, run, [{"location": parsed.affects or ""}], "plan intake gap")
        state.emit(con, run, "plan.intake_gap", f"PLAN INTAKE_GAP {v}: the user must decide: {parsed.decision} "
                   f"(affects {parsed.affects}; why evidence cannot settle it: {parsed.why})"
                   + (f"; paused {', '.join(paused)}" if paused else ""),
                   payload={"decision": parsed.decision, "why": parsed.why, "affects": parsed.affects})
        return
    blocking = [f for f in parsed.findings if f.get("blocking")]
    rounds, cap = substantive_rounds(con, run), round_cap(run)
    if rounds >= cap:
        # #418: the cycle closes unapproved. The verdict stays RECHECK; the
        # orchestrator owns the findings and records a disposition for each.
        pr["outstanding"] = _outstanding(con, run, blocking, parsed)
        close_cycle(con, run, pr, OWNED, f"round budget spent ({rounds}/{cap}) with RECHECK on {v}{label}")
        state.update_run(con, run["id"], plan_review=pr)
        paused = _pause_affected(con, run, blocking, "plan findings await the orchestrator's disposition")
        state.emit(con, run, "plan.budget_exhausted", f"PLAN RECHECK {v} after {rounds}/{cap} substantive rounds: "
                   "plan review is closed, unapproved; the orchestrator owns "
                   + ", ".join(f["code"] for f in blocking) + " (fix each in the plan or accept it, then record a "
                   "disposition)" + (f"; paused {', '.join(paused)}" if paused else ""),
                   payload=pr["outstanding"])
        return
    pr["status"] = "recheck"
    state.update_run(con, run["id"], plan_review=pr)
    paused = _pause_affected(con, run, blocking, "plan recheck")
    from office import gates as gate_engine
    who = gate_engine.recheck_continuity(con, run, reviewer)
    state.emit(con, run, "plan.recheck", f"PLAN RECHECK {v} (round {rounds}/{cap}){label}: "
               + "; ".join(f"{f['code']} {f.get('location') or ''} {f['summary'][:80]}" for f in blocking[:4])
               + (f"; paused {', '.join(paused)}" if paused else "") + f"; next round: {who}",
               payload={"findings": parsed.findings, "next_action": parsed.next_action})


def _outstanding(con, run: dict, blocking: list[dict], parsed) -> dict:
    """What the orchestrator owns once a cycle closes at its round cap (#418)."""
    return {
        "owner": "orchestrator",
        "remaining": [{k: f.get(k) for k in ("code", "level", "location", "summary", "seam")} for f in blocking],
        "attempts": [{"round": g["round"], "plan_version": g["plan_version"], "verdict": g["verdict"],
                      "summary": (g.get("summary") or "")[:160]} for g in _cycle_gates(con, run)
                     if g.get("review_status") == contract.COMPLETED],
        "reviewer_next": parsed.next_action,
    }
