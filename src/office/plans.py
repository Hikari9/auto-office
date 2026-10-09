"""Plans, plan review, and the dispatch barrier.

Convergence contract (#337, office.contract):

  APPROVED    dispatch-safe: plan review ends and every eligible executor may
              fan out. Its findings stay tracked until the orchestrator or
              planner fixes or dispositions them; that cleanup gets no new
              review unless it moves a hard seam (requirements, ownership,
              dependency, interface, acceptance), which reopens review.
  RECHECK     the planner revises; tasks the blocking findings name (and their
              dependants) wait, plan-wide findings hold every task, and the
              same reviewer (when available) reviews the revision. Three
              substantive rounds, then the operator decides (office decide plan).
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
    lint = lint_plan(con, run, parsed.tasks, parsed.requirements.get("done_criteria") or frozen.get("done_criteria") or [],
                     blast=(parsed.requirements.get("blast_radius"),
                            frozen.get("blast_radius") or (run.get("risk") or {}).get("blast_radius")))
    if lint and not contract.is_convergence(run):
        # Runs pinned to v3.1 keep their semantics: their accepted plans are never newly refused, only warned.
        parsed.warnings[:0] = [f"plan lint: {problem}" for problem in lint[:3]]
        lint = []
    if lint:
        raise Refused("plan-lint", "plan criteria cannot be met as written: " + "; ".join(lint[:4]), scope="plan",
                      preserved=f"{plan_path} is unchanged", data={"problems": lint},
                      next_step=f"reword those criteria in {plan_path} (one location per deliverable, no PR text without "
                                "task PRs), then office submit")
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
        prev_tasks = (current or {}).get("tasks") if current else None
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
        elif contract.is_convergence(run):
            res.add(f"plan p{new_version} submitted | " + review_after_revision(con, run, prev_tasks, parsed.tasks,
                                                                               new_version))
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


def lint_plan(con, run: dict, tasks: list[dict], done: list[str], blast: tuple[str | None, str | None] = (None, None)) -> list[str]:
    """Deterministic problems in a plan's done and accept criteria, found before any reviewer sees it.

    1. With task PRs off, a criterion that needs a PR body, description or comment can never be met.
    2. A done criterion and an accept item that place the same deliverable (a write-up, summary, ...) in
       different places contradict each other (#363): reviewers then fault whichever location the work chose."""
    problems = []
    criteria = [("done", None, c) for c in done] + [("accept", t["id"], c) for t in tasks for c in t.get("accept") or []]
    if any(briefs.refers_to_pr_text(c) for _, _, c in criteria):
        from office import prs
        plan_blast, frozen_blast = blast
        pinned = ((run.get("landing") or {}).get("prs") or {})
        if plan_blast and plan_blast != frozen_blast and "local" in (plan_blast, frozen_blast) \
                and ("enabled" not in pinned or pinned.get("transient")):
            # Whether PRs exist turns on a blast radius this plan is about to change: detecting now would pin the
            # answer for the old one for the whole run. A plan going local has none; one leaving local is unknown.
            found = {"enabled": False, "reason": "blast radius is local"} if plan_blast == "local" else {"transient": True}
        else:
            found = prs.settings(con, run)  # asks GitHub once, outside any transaction, as dispatch does
        if not found.get("enabled") and not found.get("transient"):
            for kind, tid, text in criteria:
                if briefs.refers_to_pr_text(text):
                    problems.append(f"{tid + ' accept' if tid else 'done'} criterion {text[:80]!r} needs a PR body, "
                                    f"description or comment, but this run has no task PRs ({found.get('reason') or 'off'})")
    for done_text in done:
        for tid, text in ((t["id"], a) for t in tasks for a in t.get("accept") or []):
            shared = briefs.deliverables(done_text) & briefs.deliverables(text)
            where_done, where_accept = briefs.locations(done_text), briefs.locations(text)
            if shared and where_done and where_accept and not where_done & where_accept:
                problems.append(f"done {done_text[:70]!r} puts the {sorted(shared)[0]} in {', '.join(sorted(where_done))} "
                                f"but {tid} accept {text[:70]!r} puts it in {', '.join(sorted(where_accept))}")
    return problems


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
                      accept=p["accept"], checks=checks, visual=p["visual"])
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
        from office import convergence
        brief = briefs.plan_review_brief(run, plan, req["frozen"], [], rereview, carried=carried, round_no=gate["round"],
                                         used_codes=convergence.used_codes(con, run, PLAN_SCOPE))
    else:
        brief = briefs.plan_review_brief(run, plan, req["frozen"], open_defects(con, run["id"]), rereview)
    outcome = gate_engine.run_reviewer(con, run, gate, "plan_reviewer", brief, cwd=Path(run["repo_root"]),
                                       plan_review=True, exclude=job["payload"].get("exclude"),
                                       resume_from=job["payload"].get("resume_from"),
                                       review_override=_pr(state.get_run(con, run["id"])).get("review_pin"))
    with db.transaction(con):
        ingest_plan_review(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome.get("verdict")}


def ingest_plan_review(con, run: dict, gate_id: str, outcome: dict) -> None:
    """Record a plan-review result. Caller holds the tx."""
    if contract.is_convergence(run):
        _ingest_convergence(con, run, gate_id, outcome)
        return
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


def _pr(run: dict) -> dict:
    return dict(run.get("plan_review") or {})


def _cycle(run: dict) -> int:
    return int(_pr(run).get("cycle") or 1)


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
    return {
        "contract": contract.CONVERGENCE,
        "required": review_required(run),
        "ended": bool(pr.get("ended")),
        "ended_reason": pr.get("ended_reason"),
        "status": pr.get("status") or ("pending" if review_required(run) else "not-required"),
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
        "escalation": pr.get("escalation"),
    }


def _hard_seam_changes(prev: list[dict] | None, new: list[dict]) -> list[str]:
    """Which hard seams a plan revision moved: tasks added or removed, or a task's
    ownership envelope, dependencies, interfaces, acceptance, lane, shared
    boundary, or visual applicability. Checks, titles, routes and visual details
    are not seams."""
    if prev is None:
        return ["initial plan"]
    old = {t["id"]: t for t in prev}
    out = []
    for t in new:
        o = old.pop(t["id"], None)
        if o is None:
            out.append(f"{t['id']} added")
            continue
        for key, label in (("scope", "ownership"), ("depends", "dependency"), ("interfaces", "interface"),
                           ("accept", "acceptance"), ("lane", "lane"), ("converge", "shared boundary")):
            if (o.get(key) or None) != (t.get(key) or None):
                out.append(f"{t['id']} {label}")
        if bool((o.get("visual") or {}).get("none")) != bool((t.get("visual") or {}).get("none")) \
                or bool(o.get("visual")) != bool(t.get("visual")):
            out.append(f"{t['id']} visual applicability")
    out += [f"{tid} removed" for tid in old]
    return out


def review_after_revision(con, run: dict, prev_tasks: list[dict] | None, new_tasks: list[dict], version: int) -> str:
    """Decide what a plan revision needs under the convergence contract. Caller
    holds the tx. Returns a short line for the submitter."""
    run = state.get_run(con, run["id"])
    if not review_required(run):
        state.emit(con, run, "plan.ready", f"PLAN READY p{version} (no plan review funded by this gear)")
        return "no plan review funded"
    pr = _pr(run)
    if pr.get("ended_reason") == "waived by the user":
        state.emit(con, run, "plan.ready", f"PLAN READY p{version} (plan review waived)")
        return "plan review waived"
    seams = _hard_seam_changes(prev_tasks, new_tasks)
    if pr.get("reviewed_requirements") and pr["reviewed_requirements"] != run["requirements_version"]:
        seams.append(f"requirements r{pr['reviewed_requirements']} -> r{run['requirements_version']}")
    if pr.get("ended") and not seams:
        state.emit(con, run, "plan.cleanup", f"plan p{version}: APPROVED cleanup moves no hard seam; no re-review",
                   audience="runtime")
        return "APPROVED cleanup (no hard seam moved; no re-review)"
    if pr.get("status") == "escalated":
        return "the plan review is at its round cap; the operator decides (office decide plan ...)"
    if pr.get("ended"):
        # A hard-seam change after APPROVED is a contract amendment: a new review cycle.
        pr.update({"ended": False, "ended_reason": None, "status": "pending", "cycle": _cycle(run) + 1,
                   "reason": "hard seam moved after APPROVED: " + ", ".join(seams[:4])})
        state.update_run(con, run["id"], plan_review=pr)
        state.emit(con, run, "plan.rereview", f"plan p{version} moves a hard seam ({', '.join(seams[:3])}); "
                   "it is reviewed again before affected work proceeds")
    elif pr.get("intake_gap") and not pr.get("intake_answered"):
        # The user's decision arrived as this revision: a fresh cycle reviews it.
        pr.update({"cycle": _cycle(run) + 1, "intake_answered": True})
        state.update_run(con, run["id"], plan_review=pr)
    gid = queue_plan_review(con, state.get_run(con, run["id"]), version)
    return "plan-review queued" if gid else "plan review not queued"


def _queue_convergence_review(con, run: dict, plan_version: int, *, exclude: list[str] | None = None) -> str | None:
    run = state.get_run(con, run["id"])
    gates = plan_gates(con, run["id"])
    if any(g["plan_version"] == plan_version and g["status"] in ("queued", "running") for g in gates):
        return None
    pr = _pr(run)
    if pr.get("status") in ("escalated", "stopped"):
        return None
    rounds = substantive_rounds(con, run)
    if rounds >= contract.MAX_ROUNDS:
        return None
    payload = {"exclude": list(exclude or [])}
    # Same reviewer across a RECHECK sequence when it is still available (#337).
    prev = [g for g in _cycle_gates(con, run) if g.get("reviewer_dispatch_id") and g.get("review_status") == contract.COMPLETED]
    if prev and not payload["exclude"]:
        payload["resume_from"] = prev[-1]["reviewer_dispatch_id"]
    elif pr.get("exclude_route") and pr["exclude_route"] not in payload["exclude"]:
        payload["exclude"].append(pr["exclude_route"])  # an operator-chosen technical escalation
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


def review_failed(con, run: dict) -> dict | None:
    """The plan-review gate whose reviewer could not finish and that nothing is retrying
    (convergence contract: unavailable, or no usable reply), or None."""
    pr = _pr(state.get_run(con, run["id"]))
    gates = plan_gates(con, run["id"])
    if pr.get("ended") or pr.get("status") not in ("unavailable", "attention") or not gates:
        return None
    last = gates[-1]
    if last["status"] != "done" or last.get("review_status") not in (contract.UNAVAILABLE, contract.INVALID_RESULT):
        return None
    return last


def fallback_next(con, run: dict) -> str:
    """The next: line for a plan review whose reviewer could not finish: the next
    fallback reviewer, or the orchestrator's own recorded review, never a waiver."""
    from office import gates as gate_engine
    last = review_failed(con, run)
    version = last["plan_version"] if last else run["plan_version"]
    tried = gate_engine.reviewer_tried(con, run["id"], [g["id"] for g in plan_gates(con, run["id"])
                                                         if g["plan_version"] == version])
    nxt = gate_engine.next_reviewer_route(con, run, "plan_reviewer", None, tried)
    head = (f"plan review: no reviewer returned a verdict for p{version} (runtime status, not a verdict, no round "
            "spent).")
    produced = None if run.get("planner_mode") == "dedicated" else "you wrote this plan inline"
    return gate_engine.fallback_options(head, "plan", nxt, subject=f"plan p{version}",
                                        report_cmd="office review plan --report <file>", produced=produced)


def rerun_review(con, run: dict, *, pin: dict | None) -> str:
    """`office rerun plan --review [--review-as <route>]`: run the plan review again on the
    current plan version after its reviewer could not finish. No round is spent. Caller holds the tx."""
    run = state.get_run(con, run["id"])
    from office import gates as gate_engine
    gate_engine.require_orchestrator(con, run, "re-run a review", code="worker-cannot-pin-reviewer")
    if not contract.is_convergence(run):
        raise Usage("plan-review-contract", "the plan review of a v3.1 run is retried with office resume",
                    next_step="office resume")
    last = review_failed(con, run)
    if last is None:
        raise Refused("nothing-to-rerun", "the plan review has no failed reviewer to replace: it is running, has a "
                      "verdict, or ended", scope="plan", next_step="office status")
    if pin:
        pr = _pr(run)
        pr["review_pin"] = pin
        state.update_run(con, run["id"], plan_review=pr)
    gid = _queue_convergence_review(con, state.get_run(con, run["id"]), last["plan_version"])
    if gid is None:
        raise Refused("nothing-to-rerun", "no plan review round can start now", scope="plan", next_step="office status")
    state.emit(con, run, "review.rerun", f"plan review p{last['plan_version']} re-run by its reviewer only"
               + (f" on {pin['as']}" if pin else ""), audience="runtime")
    return gid


def fallback_review(con, run: dict, report: Path) -> Result:
    """The orchestrator's plan review once the plan reviewer could not finish, recorded as degraded and
    non-independent. Refused for a plan the orchestrator wrote (an inline planner), and for a plan review
    that has a verdict or is still running."""
    from office import gates as gate_engine
    gate_engine.require_orchestrator(con, run, "review in a reviewer's place")
    if not report.is_file():
        raise Usage("no-report", f"no review file at {report}")
    parsed = review_parse.parse(gate_engine._last_block(report.read_text(encoding="utf-8", errors="replace")),
                                plan_review=True, contract=contract.CONVERGENCE)
    if not parsed.valid or not parsed.verdict:
        raise Refused("report-invalid", "the report is not a valid review: " + "; ".join(parsed.errors[:3] or ["no verdict"]),
                      next_step="rewrite it in the review format, then retry")
    who = gate_engine.actor_identity()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        last = review_failed(con, run) if contract.is_convergence(run) else None
        if last is None:
            raise Refused("fallback-not-allowed", "the orchestrator's review stands in only for a plan reviewer that "
                          "could not finish (silent, or stopped on a usage limit); this plan review is not in that state",
                          scope="plan", next_step="office status")
        if run.get("planner_mode") != "dedicated":
            raise Refused("orchestrator-produced-work", "the orchestrator may not review a plan it wrote inline",
                          scope="plan", next_step="another reviewer: office rerun plan --review --review-as <route>")
        gid = "G" + uuid.uuid4().hex[:8]
        con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, round, escalated, "
                    "created_at, contract, cycle) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gid, run["id"], PLAN_SUBJECT, last["plan_version"], "plan_review", f"plan:{last['plan_version']}",
                     "running", last["round"], 0, now_iso(), contract.CONVERGENCE, _cycle(run)))
        state.record_evidence(con, run["id"], "review_output", report, gate_id=gid,
                              meta={"route": "orchestrator", "who": who, "plan_version": last["plan_version"],
                                    "independence": contract.DEGRADED, "replaces_gate": last["id"]})
        _ingest_convergence(con, run, gid, {
            "status": contract.COMPLETED, "verdict": parsed.verdict, "parsed": parsed,
            "route": f"orchestrator ({who}) (degraded fallback)",
            "summary": f"{parsed.verdict} by the orchestrator ({who}) on route orchestrator for plan "
                       f"p{last['plan_version']}: degraded, non-independent fallback after the reviewer could not "
                       f"finish ({last.get('review_status')})"}, independence=contract.DEGRADED)
        state.emit(con, run, "review.degraded_fallback", f"plan p{last['plan_version']} reviewed by the orchestrator "
                   f"({who}, route orchestrator) as the degraded, non-independent fallback: {parsed.verdict}",
                   payload={"who": who, "route": "orchestrator", "plan_version": last["plan_version"],
                            "replaces_gate": last["id"], "verdict": parsed.verdict})
    jobs.kick(con, run["id"])
    return Result(lines=[f"plan p{last['plan_version']} {parsed.verdict} recorded as a degraded, non-independent "
                         f"orchestrator review by {who}"], next="office status")


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


def _ingest_convergence(con, run: dict, gate_id: str, outcome: dict, *, independence: str = contract.INDEPENDENT) -> None:
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
    status = outcome.get("status") or contract.UNAVAILABLE
    verdict = outcome.get("verdict") if status == contract.COMPLETED else None
    parsed: review_parse.Parsed | None = outcome.get("parsed")
    reviewer = outcome.get("dispatch_id")
    con.execute("UPDATE gates SET status='done', verdict=?, review_status=?, route=?, finished_at=?, summary=?, "
                "reviewer_dispatch_id=?, independence=?, next_action=?, contract=? WHERE id=?",
                (verdict, status, outcome.get("route"), now_iso(), outcome.get("summary"), reviewer, independence,
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
    from office import convergence
    renamed = convergence.distinct_codes(con, run, PLAN_SCOPE, "plan_review", parsed)
    if renamed:
        state.emit(con, run, "finding.recoded", f"plan review reused finding code(s) of earlier rounds for new findings; "
                   "recorded as " + ", ".join(f"{old} -> {new}" for old, new in sorted(renamed.items())),
                   audience="runtime", payload={"scope": PLAN_SCOPE, "recoded": renamed})
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
    pr["reviewed_requirements"] = run["requirements_version"]
    pr["reviewer"] = reviewer
    nonblocking = [f for f in parsed.findings if not f.get("blocking")]
    if verdict == "APPROVED":
        pr.update({"ended": True, "ended_reason": f"APPROVED on {v}", "status": "approved", "escalation": None})
        state.update_run(con, run["id"], plan_review=pr)
        _unpause_plan_holds(con, run)
        state.emit(con, run, "plan.approved", f"PLAN APPROVED {v}: dispatch-safe"
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
    rounds = substantive_rounds(con, run)
    if rounds >= contract.MAX_ROUNDS:
        pr.update({"status": "escalated", "escalation": _escalation_summary(con, run, blocking, parsed)})
        state.update_run(con, run["id"], plan_review=pr)
        paused = _pause_affected(con, run, blocking, "plan review at its round cap")
        state.emit(con, run, "plan.escalation", f"PLAN RECHECK {v} after {rounds} substantive rounds: the operator "
                   f"decides now (office decide plan escalate|continue|waive|stop); recommendation: "
                   f"{pr['escalation']['recommendation']}" + (f"; paused {', '.join(paused)}" if paused else ""),
                   payload=pr["escalation"])
        return
    pr["status"] = "recheck"
    state.update_run(con, run["id"], plan_review=pr)
    paused = _pause_affected(con, run, blocking, "plan recheck")
    state.emit(con, run, "plan.recheck", f"PLAN RECHECK {v} (round {rounds}/{contract.MAX_ROUNDS}): "
               + "; ".join(f"{f['code']} {f.get('location') or ''} {f['summary'][:80]}" for f in blocking[:4])
               + (f"; paused {', '.join(paused)}" if paused else ""),
               payload={"findings": parsed.findings, "next_action": parsed.next_action})


def _escalation_summary(con, run: dict, blocking: list[dict], parsed) -> dict:
    """What the operator sees at the plan round cap (#337 escalation)."""
    history = [{"round": g["round"], "plan_version": g["plan_version"], "verdict": g["verdict"],
                "summary": (g.get("summary") or "")[:160]} for g in _cycle_gates(con, run)
               if g.get("review_status") == contract.COMPLETED]
    material = [f for f in blocking if (f.get("level") or f.get("severity")) in ("high", "medium")]
    return {
        "scope": PLAN_SCOPE,
        "remaining": [{k: f.get(k) for k in ("code", "level", "location", "summary", "seam")} for f in blocking],
        "materiality": f"{len(material)} of {len(blocking)} blocking finding(s) are high or medium",
        "attempts": history,
        "risk": "dispatch stays held for the tasks these findings name until the plan is approved or the gate is waived",
        "recommendation": parsed.next_action or ("continue: one more bounded revision cycle" if len(blocking) <= 2
                                                  else "escalate: a different reviewer or planner"),
        "choices": contract.round_cap_choices(PLAN_SCOPE),
    }
