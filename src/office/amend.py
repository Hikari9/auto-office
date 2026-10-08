"""Amendments: versioned deltas, durable delivery, and applied acknowledgement.

Delivered and applied are different states. The runtime delivers a combined
delta on the worker's next `office` command (and nudges a live Herdr worker).
Only `office ack <id>` records that the worker applied it at a safe boundary.
A newer amendment supersedes an older unapplied one.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from office import db, jobs, paths, planfile, planpath, plans, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, sha256_bytes

# Words that signal an authority-envelope change (external, irreversible, or
# destructive action). An "ordinary" amendment carrying one is refused. A term that
# is one part of a hyphen/underscore compound identifier (`send-keys`, `send_keys`,
# `release-notes`) names a thing, not the action, so it does not count; `--prod` and
# `force-push` still do.
AUTHORITY_TERMS = re.compile(
    r"\b(?<!\w-)(?:deploy|production|prod|publish|release|send|email|notify users|delete|drop table|truncate|"
    r"force.?push|merge (?:to|into) main|migrat(?:e|ion) (?:prod|production)|payment|charge|"
    r"rotate (?:key|secret)|credentials?)\b(?!-\w)", re.I)


def amend(con, run: dict, scope: str, delta: str, *, contract: bool = False, requirements: bool = False,
          quote: str | None = None, cwd: Path | None = None, redirect: dict | None = None,
          drop_criteria: list[str] | None = None, add_criteria: list[str] | None = None,
          no_review: bool = False, reason: str | None = None) -> Result:
    """`redirect` ({defect, root_cause, requirement, reviewer}) marks a contract
    amendment as the user's redirect of a plan defect (see office.redirect).
    `drop_criteria`/`add_criteria` edit the frozen done criteria (requirements only). A requirements
    amendment that only adds criteria is not delivered to live tasks; naming tasks as the scope
    (`office amend T1,T2 --requirements ...`) forces delivery to those.
    `no_review` (with a `reason`) is the orchestrator's veto of plan review for an ordinary amendment. It
    matters only while a plan-review cycle is open: once it closes, no amendment is reviewed (#418)."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-amend", "workers do not amend the plan; report the problem in your submission",
                      next_step="office submit")
    if not delta or not delta.strip():
        raise Usage("missing-delta", "an amendment needs a delta", next_step='office amend <scope> -- "<delta>"')
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}")
    if redirect is not None:
        from office import redirect as redirect_mod
        if not contract or requirements or scope == "requirements":
            raise Usage("redirect-needs-contract", "a defect redirect is a contract amendment",
                        next_step=redirect_mod.AMEND_FORM)
        redirect = redirect_mod.validate(con, run, dict(redirect, quote=quote))
    if (drop_criteria or add_criteria) and not (requirements or scope == "requirements"):
        raise Usage("criteria-are-requirements", "done criteria change only with a requirements amendment",
                    next_step=CRITERIA_FORM)
    if reason and not no_review:
        raise Usage("reason-needs-no-review", "--reason records why plan review is vetoed; it goes with --no-review",
                    next_step=NO_REVIEW_FORM)
    if no_review:
        if not reason or not reason.strip():
            raise Usage("no-review-needs-reason", "--no-review needs a reason the run's record keeps", next_step=NO_REVIEW_FORM)
        if contract or requirements or scope == "requirements":
            raise Refused("no-review-ordinary-only", "--no-review vetoes review of an ordinary amendment only; a contract "
                          "or requirements revision follows the plan-review cycle (reviewed while it is open, never "
                          "after it closes)", scope=scope, preserved="plan unchanged",
                          next_step="rerun without --no-review (office amend --help)")
    if requirements or scope == "requirements":
        to = [] if scope == "requirements" else _scope_ids(con, run, scope)
        return _requirements_change(con, run, delta, quote, drop_criteria or [], add_criteria or [], to)
    scope_ids = _scope_ids(con, run, scope)
    plan_path = _orchestrator_plan_path(con, run, cwd)
    plan_text = planfile.strip_generated(plan_path.read_text(encoding="utf-8")) if plan_path else None
    if contract:
        return _contract(con, run, scope, scope_ids, delta, plan_text, redirect, plan_path)
    return _ordinary(con, run, scope, scope_ids, delta, plan_text, plan_path, veto_reason=reason.strip() if no_review else None)


def _scope_ids(con, run: dict, scope: str) -> list[str]:
    if scope in ("plan", "all", "run"):
        return []
    ids = [s.strip() for s in re.split(r"[,\s]+", scope) if s.strip()]
    known = {t["id"] for t in state.tasks(con, run["id"])}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise Usage("unknown-task", f"unknown task(s) {', '.join(unknown)}", next_step="scope is plan, or task ids like T2,T3")
    return ids


def _orchestrator_plan_path(con, run: dict, cwd: Path | None) -> Path | None:
    ident = paths.repo_identity(cwd)
    if ident is None:
        return None
    planpath.relocate_legacy(con, ident[0])
    draft = planpath.draft(ident[0], run)
    return draft if draft.is_file() else None


def _requirements_change(con, run: dict, delta: str, quote: str | None, drop: list[str], add: list[str],
                         to: list[str]) -> Result:
    if not quote or not quote.strip():
        raise Refused("user-quote-required", "only the user can change requirements; record their words",
                      next_step='office amend requirements --quote "<user\'s exact words>" -- "<change>"')
    with db.transaction(con):
        version, targets = record_requirements(con, state.get_run(con, run["id"]), delta, quote, drop=drop, add=add,
                                                to=to, with_targets=True)
        delivery = f"delivered to {', '.join(targets)}" if targets else "not delivered to live tasks"
        state.emit(con, run, "requirements.changed",
                   f"REQUIREMENTS r{version}: {delta.strip()[:100]}; {delivery}; authorization required")
    jobs.kick(con, run["id"])
    return Result(lines=[f"requirements r{version} recorded | {delivery} | authorization for r{version} required"],
                  next='ask the user (native question tool) for authorization, then office approve plan --quote "<user\'s words>"')


def record_requirements(con, run: dict, delta: str, quote: str, *, drop: list[str] = (), add: list[str] = (),
                        to: list[str] = (), with_targets: bool = False):
    """Record requirements r(n+1) from the user's words and deliver it to live
    workers. Caller holds the tx. Returns the new version (with `with_targets`, also the tasks
    it was delivered to). `drop` names frozen done criteria the user removed (each must match
    one); `add` appends new ones. A change that only adds criteria concerns no task already
    running its contract, so it is recorded but delivered only to the live tasks in `to`."""
    cur = state.current_requirements(con, run["id"])
    frozen = dict(cur["frozen"])
    criteria = list(frozen.get("done_criteria") or [])
    for text in drop:
        criteria.remove(_match_criterion(criteria, text))
    added = [a.strip() for a in add if a.strip() and a.strip() not in criteria]
    criteria += added
    frozen["done_criteria"] = criteria
    frozen.setdefault("user_changes", []).append(delta.strip())
    version = cur["version"] + 1
    con.execute("INSERT INTO requirements(run_id, version, frozen_json, source, quote, created_at) VALUES(?,?,?,?,?,?)",
                (run["id"], version, dumps(frozen), "user", quote.strip(), now_iso()))
    state.update_run(con, run["id"], requirements_version=version)
    amendment_id = _record(con, run, "requirements", [], delta, run["plan_version"], run["plan_version"])
    live = [t["id"] for t in state.tasks(con, run["id"]) if t["status"] not in ("accepted", "cancelled", "planned")]
    if added and not drop:
        live = [t for t in live if t in to]
    targets = _deliver(con, state.get_run(con, run["id"]), amendment_id, live, f"requirements r{version}: {delta.strip()}",
                       run["plan_version"])
    return (version, targets) if with_targets else version


CRITERIA_FORM = ('office amend requirements --quote "<user\'s words>" [--drop-criterion "<criterion>"]... '
                 '[--add-criterion "<criterion>"]... -- "<change>"')


def _match_criterion(criteria: list[str], text: str) -> str:
    """The one frozen done criterion `text` names: an exact match, else the only one containing it."""
    want = " ".join(text.split()).lower()
    exact = [c for c in criteria if " ".join(str(c).split()).lower() == want]
    hits = exact or [c for c in criteria if want and want in " ".join(str(c).split()).lower()]
    if len(hits) == 1:
        return hits[0]
    listing = "; ".join(str(c)[:80] for c in criteria) or "none"
    raise Refused("ambiguous-criterion" if hits else "unknown-criterion",
                  f"{text!r} names {len(hits)} frozen done criteria (current: {listing})", scope="requirements",
                  preserved="requirements unchanged", next_step=CRITERIA_FORM)


NO_REVIEW_FORM = 'office amend <scope> --no-review --reason "<why review adds nothing>" -- "<delta>"'


def _ordinary(con, run: dict, scope: str, scope_ids: list[str], delta: str, plan_text: str | None,
             plan_path: Path | None = None, veto_reason: str | None = None) -> Result:
    current = state.current_plan(con, run["id"])
    if current is None:
        raise Refused("no-plan", "there is no plan to amend", next_step="office submit the plan first")
    new_text, parsed = _next_plan_text(current, plan_text, scope_ids, delta)
    # Only what the amendment adds can widen authority: an edited plan is judged
    # by its new lines (removing a line that names a deploy adds no action), a
    # delta-only amendment by its delta, which becomes plan text.
    old_lines = set(current["body"].splitlines())
    added = "\n".join(line for line in new_text.splitlines() if line not in old_lines)
    if AUTHORITY_TERMS.search(added):
        raise Refused("contract-level-change", "this delta touches the authority envelope (external, irreversible, or "
                      "destructive action); it is not an ordinary amendment", scope=scope,
                      next_step=f'office amend {scope} --contract -- "<request>" (and user authorization for the new action)')
    changes = _diff(con, run, parsed.tasks)
    contract_hits = changes["contract"] + [f"{t} (new, overlapping scope)" for t in changes["overlap_new"]]
    if parsed.requirements.get("named_actions") and parsed.requirements["named_actions"] != (current["requirements"] or {}).get("named_actions"):
        contract_hits.append("named actions")
    if contract_hits:
        raise Refused("contract-level-change", f"ordinary amendments cannot change scope ownership, interfaces, or "
                      f"authority ({', '.join(contract_hits)})", scope=scope, preserved="plan unchanged",
                      next_step=f'office amend {scope} --contract -- "<request>"')
    res = Result()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        version = run["plan_version"] + 1
        amendment_id = _record(con, run, "ordinary", scope_ids, delta, run["plan_version"], version)
        con.execute("INSERT INTO plans(run_id, version, kind, body, tasks_json, requirements_json, created_by, created_at, "
                    "content_hash, parent_version, amendment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run["id"], version, "ordinary", new_text, dumps(parsed.tasks), dumps(parsed.requirements),
                     "orchestrator", now_iso(), sha256_bytes(new_text.encode()), run["plan_version"], amendment_id))
        sync = plans.sync_tasks(con, run, parsed.tasks, version, rerun_checks=True)
        # A checks-only change to an accepted task reruns its checks instead of reopening it.
        rerun = sync["checks_only"]
        # Added tasks are the only affected ones when the plan merely grows: a running task's contract is untouched.
        affected = sorted((set(scope_ids) | set(sync["acceptance"]) | set(sync["contract"]) | set(sync["added"])) - set(rerun))
        if not scope_ids and not affected and not rerun:
            affected = [t["id"] for t in state.tasks(con, run["id"]) if t["status"] not in ("accepted", "cancelled")]
        for tid in scope_ids:
            if tid not in sync["acceptance"] and tid not in rerun:
                state.update_task(con, run["id"], tid, contract_version=version, acceptance_version=version)
        state.update_run(con, run["id"], plan_version=version)
        run = state.get_run(con, run["id"])
        plans.apply_run_checks(con, run, parsed.run_checks)
        run = state.get_run(con, run["id"])
        rechecked = [tid for tid in rerun if plans.rerun_accepted_checks(con, run, tid, amendment_id)]
        delivered = _deliver(con, run, amendment_id, affected, delta.strip(), version)
        rereview = None
        from office import contract
        if veto_reason:
            # The orchestrator's veto: p+1 queues no plan review. A gate already queued or running for an earlier
            # version is left to finish.
            state.emit(con, run, "plan.review_skipped", f"plan p{version}: review vetoed by the orchestrator: {veto_reason}")
        elif contract.is_convergence(run):
            rereview = plans.review_after_revision(con, run, version)
            rereview = rereview if "queued" in rereview else None
        elif plans.review_required(run) and not plans.plan_review_ended(con, run):
            rereview = plans.queue_plan_review(con, run, version)
        state.emit(con, run, "plan.amended", f"plan p{version} ({amendment_id}, ordinary)"
                   + (f" | affected {', '.join(affected)}" if affected else ""), audience="runtime")
    jobs.kick(con, run["id"])
    parts = [f"plan p{version}", amendment_id]
    if rereview:
        parts.append("rereview queued")
    if affected:
        parts.append(f"affected {','.join(affected)}")
    if rechecked:
        parts.append(f"checks rerun on {','.join(rechecked)}")
    if delivered:
        parts.append(f"delivering to {','.join(delivered)}")
    res.add(" | ".join(parts))
    plans.show_diagram(con, state.get_run(con, run["id"]), version, parsed.tasks, plan_path, res)
    from office import guide
    res.next = guide.next_action(con, state.get_run(con, run["id"]))
    return res


def _contract(con, run: dict, scope: str, scope_ids: list[str], delta: str, plan_text: str | None,
              redirect: dict | None = None, plan_path: Path | None = None) -> Result:
    """Contract amendments belong to the planner. In a run whose orchestrator is
    the planner (inline mode), its edited PLAN.md is the contract amendment."""
    if run.get("planner_mode") == "inline":
        if plan_text is None:
            raise Usage("no-plan-file", f"edit {planpath.rel(run)} with the contract change first",
                        next_step=f'edit {planpath.rel(run)}, then office amend {scope} --contract -- "<summary>"')
        return _apply_contract_text(con, run, scope_ids, delta, plan_text, author="orchestrator-as-planner",
                                    redirect=redirect, plan_path=plan_path)
    from office import dispatch
    from office import redirect as redirect_mod
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        amendment_id = _record(con, run, "contract", scope_ids, delta, run["plan_version"], None)
        request = f"{amendment_id}: {delta.strip()}"
        if redirect:
            redirect_lines = redirect_mod.record(con, run, redirect)
            run = state.get_run(con, run["id"])
            request += "\n" + redirect_mod.planner_note(redirect)
        graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
        targets = set(scope_ids)
        for tid in scope_ids:
            targets |= planfile.dependants(graph, tid)
        if not scope_ids:
            targets = {t["id"] for t in state.tasks(con, run["id"])}
        paused = []
        for t in state.tasks(con, run["id"]):
            if t["id"] in targets and t["status"] not in ("accepted", "cancelled", "planned", "paused"):
                state.update_task(con, run["id"], t["id"], status="paused", pause_reason=f"contract amendment {amendment_id}")
                paused.append(t["id"])
        dispatch.create_planner_task(con, run, contract_request=request)
        state.emit(con, run, "plan.contract_requested", f"contract amendment {amendment_id} requested; planner queued",
                   audience="runtime")
    jobs.kick(con, run["id"])
    return Result(lines=[f"contract amendment {amendment_id} | planner P1 queued"
                         + (f" | paused {','.join(paused)}" if paused else "")] + (redirect_lines if redirect else []),
                  next="no action; the revised plan returns here (unaffected work continues)")


def _apply_contract_text(con, run, scope_ids, delta, text, author, redirect: dict | None = None,
                         plan_path: Path | None = None) -> Result:
    parsed = planfile.parse(text)
    if parsed.errors:
        raise Refused("plan-invalid", "plan has problems: " + "; ".join(parsed.errors[:5]),
                      next_step=f"fix {planpath.rel(run)}, then retry the amendment")
    _require_contract_edit(con, run, scope_ids, delta, text, parsed)
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        redirect_lines = []
        if redirect:
            from office import redirect as redirect_mod
            redirect_lines = redirect_mod.record(con, run, redirect)
            run = state.get_run(con, run["id"])
        version = run["plan_version"] + 1
        amendment_id = _record(con, run, "contract", scope_ids, delta, run["plan_version"], version)
        con.execute("INSERT INTO plans(run_id, version, kind, body, tasks_json, requirements_json, created_by, created_at, "
                    "content_hash, parent_version, amendment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run["id"], version, "contract", text, dumps(parsed.tasks), dumps(parsed.requirements), author,
                     now_iso(), sha256_bytes(text.encode()), run["plan_version"], amendment_id))
        sync = plans.sync_tasks(con, run, parsed.tasks, version)
        state.update_run(con, run["id"], plan_version=version)
        run = state.get_run(con, run["id"])
        plans.apply_run_checks(con, run, parsed.run_checks)
        run = state.get_run(con, run["id"])
        affected = sorted(set(scope_ids) | set(sync["contract"]) | set(sync["acceptance"]))
        _deliver(con, run, amendment_id, affected, delta.strip(), version)
        flagged = _envelope_changes(con, run, parsed)
        from office import contract
        if contract.is_convergence(run):
            plans.review_after_revision(con, run, version)
        elif plans.review_required(run):
            plans.queue_plan_review(con, run, version, escalated=plans.plan_review_ended(con, run))
        state.emit(con, run, "plan.amended", f"plan p{version} ({amendment_id}, contract)", audience="runtime")
    jobs.kick(con, run["id"])
    lines = [f"plan p{version} | {amendment_id} contract | affected {','.join(affected) or 'none'}", *redirect_lines]
    if flagged:
        lines.append(f"new authority entries need user authorization: {', '.join(flagged)}")
    res = Result(lines=lines, next=('ask the user (native question tool) for authorization, then office approve ' + flagged[0] + ' --quote "<words>"')
                 if flagged else "exceptions only; office status")
    plans.show_diagram(con, state.get_run(con, run["id"]), version, parsed.tasks, plan_path, res)
    return res


def _task_changed(cur: dict, planned: dict | None) -> bool:
    """Whether the plan's entry for a task differs from the task's recorded
    contract and acceptance (the fields plans.sync_tasks versions)."""
    if planned is None:
        return cur["status"] != "cancelled"  # removed from the plan
    return (cur["scope"] != planned["scope"] or (cur["interfaces"] or []) != planned["interfaces"]
            or cur["accept"] != planned["accept"] or cur["checks"] != (planned["checks"] or [])
            or cur["visual"] != planned["visual"] or cur["depends"] != planned["depends"]
            or cur["title"] != planned["title"])


def _require_contract_edit(con, run: dict, scope_ids: list[str], delta: str, text: str, parsed) -> None:
    """An inline contract amendment is the edited PLAN.md. Without the edit it
    would bump the plan version while the named task keeps its old contract, so
    a relaunched executor works to the old scope; refuse instead."""
    scope = ",".join(scope_ids) or "plan"
    nxt = (f"edit {planpath.rel(run)} so {'the ' + scope + ' task entry' if scope_ids else 'the plan'} states the "
           f'contract change, then office amend {scope} --contract -- "{delta.strip()[:80]}"')
    current = state.current_plan(con, run["id"])
    if current and sha256_bytes(text.encode()) == current["content_hash"]:
        raise Refused("plan-not-edited", f"{planpath.rel(run)} is identical to the current plan "
                      f"p{current['version']}; a contract amendment records the edited plan, not the request text",
                      scope=scope, preserved="plan and task contracts unchanged", next_step=nxt)
    if not scope_ids:
        return
    planned = {p["id"]: p for p in parsed.tasks}
    unchanged = [tid for tid in scope_ids if not _task_changed(state.get_task(con, run["id"], tid), planned.get(tid))]
    if unchanged:
        changed = sorted(tid for tid, p in planned.items()
                         if tid not in scope_ids and (state.get_task(con, run["id"], tid) is None
                                                      or _task_changed(state.get_task(con, run["id"], tid), p)))
        raise Refused("contract-not-edited", f"{planpath.rel(run)} does not change the entry for {', '.join(unchanged)}; "
                      "its contract would stay at the old version"
                      + (f" (the edit changes {', '.join(changed)})" if changed else ""),
                      scope=scope, preserved="plan and task contracts unchanged",
                      next_step=nxt if not changed else
                      f"edit {', '.join(unchanged)} in {planpath.rel(run)}, or name the tasks the edit changes: "
                      f'office amend {",".join(changed)} --contract -- "<summary>"')


def contract_from_planner(con, run: dict, amendment_id: str | None, changes: dict, version: int) -> None:
    """Deliver a planner-submitted contract revision. Caller holds tx."""
    affected = sorted(set(changes.get("contract", [])) | set(changes.get("acceptance", [])))
    _deliver(con, run, amendment_id or "P" + str(version), affected, f"plan p{version} contract revision", version)
    for t in state.tasks(con, run["id"]):
        if t["status"] == "paused" and (t.get("pause_reason") or "").startswith("contract amendment"):
            from office import gates
            state.update_task(con, run["id"], t["id"], status=gates.derive_status(con, run, t), pause_reason=None)


def _envelope_changes(con, run: dict, parsed) -> list[str]:
    old = {(e.get("action"), tuple(e.get("preconditions") or [])) for e in (run.get("envelope") or [])}
    flagged = []
    envelope = list(run.get("envelope") or [])
    for a in parsed.requirements.get("named_actions") or []:
        key = (a["action"], tuple(a.get("preconditions") or []))
        if key not in old:
            entry = {"id": f"X{len(envelope) + 1}", **a, "needs_authorization": True}
            envelope.append(entry)
            flagged.append(entry["id"])
    if flagged:
        state.update_run(con, run["id"], envelope=envelope)
    return flagged


def _next_plan_text(current: dict, plan_text: str | None, scope_ids: list[str], delta: str):
    if plan_text and sha256_bytes(plan_text.encode()) != current["content_hash"]:
        parsed = planfile.parse(plan_text)
        if parsed.errors:
            raise Refused("plan-invalid", "the edited plan draft has problems: " + "; ".join(parsed.errors[:5]),
                          next_step="fix the edited plan draft, then retry the amendment")
        return plan_text, parsed
    note = f"\n\n<!-- amendment -->\nAmendment ({', '.join(scope_ids) or 'plan'}): {delta.strip()}\n"
    text = current["body"].rstrip() + note
    return text, planfile.parse(current["body"])


def _diff(con, run, planned: list[dict]) -> dict:
    existing = {t["id"]: t for t in state.tasks(con, run["id"])}
    out = {"contract": [], "overlap_new": []}
    for p in planned:
        cur = existing.get(p["id"])
        if cur is None:
            others = [t for t in existing.values() if t["status"] not in ("cancelled", "accepted")]
            if any(planfile.scopes_overlap(p["scope"], o["scope"]) for o in others):
                out["overlap_new"].append(p["id"])
            continue
        if cur["scope"] != p["scope"] or (cur["interfaces"] or []) != p["interfaces"]:
            out["contract"].append(p["id"])
    return out


def _record(con, run, klass, scope_ids, delta, from_v, to_v) -> str:
    seq = con.execute("SELECT COUNT(*) FROM amendments WHERE run_id=?", (run["id"],)).fetchone()[0] + 1
    amendment_id = f"A{seq}"
    con.execute("INSERT INTO amendments(id, run_id, seq, class, scope_json, delta, from_plan_version, to_plan_version, "
                "requested_by, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"{run['id'][:8]}:{amendment_id}", run["id"], seq, klass, dumps(scope_ids), delta.strip(), from_v, to_v,
                 "orchestrator", "committed", now_iso()))
    return amendment_id


def _deliver(con, run: dict, amendment_id: str, task_ids: list[str], text: str, target_version: int) -> list[str]:
    """Queue one combined delta per affected dispatched task and supersede older
    unapplied ones. Caller holds tx."""
    from office import dispatch, gates
    targets = []
    for tid in task_ids:
        task = state.get_task(con, run["id"], tid)
        if task is None or not task.get("current_dispatch_id") or task["status"] == "cancelled":
            continue
        d = state.get_dispatch(con, task["current_dispatch_id"])
        applied = d.get("applied_plan_version") or 0
        older = con.execute("SELECT id, content FROM deliveries WHERE run_id=? AND task_id=? AND status IN ('queued','delivered') "
                            "ORDER BY target_version", (run["id"], tid)).fetchall()
        combined = [r["content"] for r in older] + [f"{amendment_id} (p{target_version}): {text}"]
        did = f"{run['id'][:8]}:{amendment_id}:{tid}"
        for r in older:
            con.execute("UPDATE deliveries SET status='superseded', superseded_by=? WHERE id=?", (did, r["id"]))
        con.execute("INSERT OR REPLACE INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, "
                    "content, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (did, run["id"], amendment_id, tid, d["id"], target_version, "queued",
                     "\n".join(combined) + f"\n(from p{applied} to p{target_version})", now_iso()))
        if task["status"] == "accepted":
            state.update_task(con, run["id"], tid, status="changes_required", pause_reason=f"amended by {amendment_id}")
        if gates.worker_live(con, d["id"]):
            from office import submit as submit_mod
            # A task blocked by this worker's own refused submit or scope request is unblocked
            # only once the prompt below is confirmed delivered to a live agent (the
            # notify_worker job); `worker_live` alone can be a dead pane.
            state.enqueue(con, run, "notify_worker", {"dispatch_id": d["id"], "task_id": tid,
                          "unblock": submit_mod.self_blocked(task), "amendment_id": amendment_id,
                          "block_id": submit_mod.block_id(con, d["id"]),
                          "text": f"AMENDMENT {amendment_id}: run office status, apply it, office ack {amendment_id}, "
                                  "then office submit again."},
                          dedup_key=f"notify:{did}", max_attempts=1)
        elif task["status"] in ("planned",):
            # Nothing has run: the first session starts from the current contract.
            con.execute("UPDATE deliveries SET status='superseded', superseded_by='relaunch' WHERE id=?", (did,))
        else:
            # The worker is gone. The relaunched session starts from the current contract, but an ordinary
            # delta is not in the contract and a reopened task has no findings: hand the delivery to the new
            # session so its brief carries the delta, and it is delivered once that prompt is confirmed.
            new_did = dispatch.request_launch(con, run, tid, role="executor", fix_of=task.get("current_revision_id"))
            con.execute("UPDATE deliveries SET dispatch_id=?, status='queued', superseded_by=NULL WHERE id=?",
                        (new_did, did))
        targets.append(tid)
    return targets


def confirm_launch_deliveries(con, run: dict) -> int:
    """Mark delivered the amendments a relaunched session carries in its brief once that brief's
    prompt landed in its running agent (launch.json records it). Only deliveries recorded before the
    session started are in its brief; a later one reaches it by its own prompt. Returns how many changed."""
    import json
    n = 0
    for r in con.execute("SELECT dl.id, dl.dispatch_id FROM deliveries dl JOIN dispatches d ON d.id=dl.dispatch_id "
                         "WHERE dl.run_id=? AND dl.status='queued' AND d.status='running' AND d.ended_at IS NULL "
                         "AND dl.created_at<=d.started_at", (run["id"],)).fetchall():
        try:
            spec = json.loads((paths.run_dir(run["id"]) / "dispatches" / r["dispatch_id"] / "launch.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(spec, dict) and spec.get("prompt_landed"):
            with db.transaction(con):
                n += con.execute("UPDATE deliveries SET status='delivered', delivered_at=COALESCE(delivered_at, ?) "
                                 "WHERE id=? AND status='queued'", (now_iso(), r["id"])).rowcount
    return n


def pending_block(con, run: dict, dispatch_id: str) -> list[str]:
    """Delivery rides on the worker's command responses. Marks delivered."""
    d = state.get_dispatch(con, dispatch_id)
    if d is None or not d.get("task_id"):
        return []
    rows = con.execute("SELECT * FROM deliveries WHERE run_id=? AND task_id=? AND dispatch_id=? AND status IN ('queued','delivered') "
                       "ORDER BY target_version DESC LIMIT 1", (run["id"], d["task_id"], dispatch_id)).fetchall()
    if not rows:
        return []
    r = rows[0]
    with db.transaction(con):
        con.execute("UPDATE deliveries SET status='delivered', delivered_at=COALESCE(delivered_at, ?), "
                    "delivered_count=delivered_count+1 WHERE id=?", (now_iso(), r["id"]))
    return [f"AMENDMENT {r['amendment_id']} delivered | task {r['task_id']} | plan -> p{r['target_version']}",
            *[f"  {line}" for line in r["content"].splitlines()[:8]],
            f"apply it at a safe boundary, then: office ack {r['amendment_id']}"]


def ack(con, run: dict, amendment_id: str) -> Result:
    dispatch_id = os.environ.get("OFFICE_DISPATCH_ID")
    if not dispatch_id:
        raise Refused("not-a-worker", "office ack is run by the worker that applied the amendment",
                      next_step="the worker runs office ack <id> after applying it")
    d = state.get_dispatch(con, dispatch_id)
    amendment_id = amendment_id.upper()
    with db.transaction(con):
        row = con.execute("SELECT * FROM deliveries WHERE run_id=? AND task_id=? AND amendment_id=? ORDER BY created_at DESC LIMIT 1",
                          (run["id"], d["task_id"], amendment_id)).fetchone()
        if row is None:
            raise Refused("unknown-amendment", f"no amendment {amendment_id} was delivered to {d['task_id']}",
                          next_step="office status shows pending amendments")
        if row["status"] == "applied":
            return Result(lines=[f"{amendment_id} already applied"], next="continue; office submit when ready")
        if row["status"] == "superseded":
            current = con.execute("SELECT * FROM deliveries WHERE run_id=? AND task_id=? AND status IN ('queued','delivered') "
                                  "ORDER BY target_version DESC LIMIT 1", (run["id"], d["task_id"])).fetchone()
            nxt = f"apply {current['amendment_id']} instead, then office ack {current['amendment_id']}" if current else "office status"
            raise Refused("amendment-superseded", f"{amendment_id} was superseded"
                          + (f" by {current['amendment_id']}:\n{current['content']}" if current else ""),
                          scope=d["task_id"], next_step=nxt)
        if row["dispatch_id"] != dispatch_id:
            holder = state.get_dispatch(con, row["dispatch_id"]) if row["dispatch_id"] else None
            task = state.get_task(con, run["id"], d["task_id"])
            if (holder and holder.get("ended_at") is None and holder.get("status") in ("launching", "running")) \
                    or task["current_dispatch_id"] != dispatch_id:
                raise Refused("wrong-holder", f"{amendment_id} was delivered to another session of {d['task_id']}")
            # The session it was delivered to has ended and this one holds the
            # task now: the amendment is this session's to apply.
            con.execute("UPDATE deliveries SET dispatch_id=? WHERE id=?", (dispatch_id, row["id"]))
        con.execute("UPDATE deliveries SET status='applied', applied_at=?, delivered_at=COALESCE(delivered_at, ?) WHERE id=?",
                    (now_iso(), now_iso(), row["id"]))
        con.execute("UPDATE dispatches SET applied_plan_version=? WHERE id=?", (row["target_version"], dispatch_id))
        state.emit(con, run, "amendment.applied", f"{d['task_id']} applied {amendment_id}", audience="runtime",
                   task_id=d["task_id"])
    return Result(lines=[f"{amendment_id} applied | task contract now p{row['target_version']}"],
                  next="continue work; office submit when ready")
