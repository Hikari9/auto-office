"""User authority: authorization, merge, envelope entries, trust, waivers.

These record the user's own words. They surface only when the run needs
them, never in normal role briefs, and nothing else grants authority.

Convergence contract (#337): waiving a lane or shared-scope review gate needs
landing authority for that scope, never a role. The user holds it; the
orchestrator holds it only when the user delegated landing (office.convergence
.landing_authority). A waiver keeps the unmet verdict or evidence state and is
bound to the scope's composed commit.
"""
from __future__ import annotations

import os
import re
import shlex
import uuid
from pathlib import Path

from office import db, jobs, routing, scoring, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, short


def approve(con, run: dict, target: str, quote: str | None, extra: list[str] | None = None,
            root_cause: str | None = None, *, by: str | None = None, report: str | None = None,
            actor: str = "user", reason: str | None = None) -> Result:
    import os
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-approve", "a worker cannot approve anything")
    from office import contract
    if (target.lower() == "waive" and extra and contract.is_convergence(run)
            and re.fullmatch(r"[LS]-[\w+.-]+:\w+", extra[0])):
        from office import convergence
        if actor == "user" and (not quote or len(re.sub(r"\s+", "", quote)) < 2):
            raise Usage("user-quote-required", "a user waiver records the user's own words",
                        next_step=f'office approve waive {extra[0]} --quote "<user\'s words>" --reason "<why>"')
        return convergence.waive(con, run, extra[0], actor=actor, quote=quote, reason=reason)
    if actor != "user":
        raise Usage("orchestrator-cannot-approve", "only lane and shared-scope gate waivers accept --as orchestrator")
    if not quote or len(re.sub(r"\s+", "", quote)) < 2:
        raise Usage("user-quote-required", "approvals record the user's own words",
                    next_step=f'office approve {target} --quote "<user\'s exact words>"')
    extra = extra or []
    t = target.lower()
    if t == "plan":
        return _authorize(con, run, quote)
    if t == "merge":
        return _record(con, run, "merge", "merge", quote, f"merge authority recorded for {short(run['id'])}",
                       "perform the authorized landing, then office close --handoff <ref>")
    if re.fullmatch(r"x\d+", t):
        return _envelope_entry(con, run, target.upper(), quote)
    if t == "trust":
        if not extra:
            raise Usage("missing-route", "name the route to promote", next_step="office approve trust <harness@ver/model@effort> --quote ...")
        return _trust(con, run, extra[0], quote)
    if t == "waive":
        if not extra:
            raise Usage("missing-gate", "name what to waive", next_step="office approve waive <T2:visual|plan-review|P3> --quote ...")
        return _waive(con, run, extra[0], quote, root_cause)
    if t == "visual":
        if not extra or not by or not report:
            raise Usage("missing-visual-verdict", "name the task, the reviewer, and its report", next_step=VISUAL_USAGE)
        return _visual_verdict(con, run, extra[0].upper(), by, Path(report).expanduser(), quote)
    raise Usage("unknown-authority", f"nothing named {target!r} needs approval",
                next_step="office status shows any authority the run needs")


def _record(con, run, kind, target, quote, line, nxt) -> Result:
    with db.transaction(con):
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, authorized_by, "
                    "quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("Z" + uuid.uuid4().hex[:8], run["id"], kind, target, run["requirements_version"],
                     dumps(run.get("envelope") or []), "user", quote.strip(), now_iso()))
        state.emit(con, run, f"authority.{kind}", f"user authorized {kind} {target}", audience="runtime")
    return Result(lines=[line], next=nxt)


def _authorize(con, run, quote) -> Result:
    import json
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        if not run["plan_version"]:
            raise Refused("no-plan", "there is no plan to authorize yet", next_step="wait for or submit the plan")
        envelope = [dict(e, needs_authorization=False) for e in (run.get("envelope") or [])]
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, authorized_by, "
                    "quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("Z" + uuid.uuid4().hex[:8], run["id"], "plan", "requirements", run["requirements_version"],
                     json.dumps(envelope), "user", quote.strip(), now_iso()))
        state.update_run(con, run["id"], envelope=envelope)
        state.emit(con, run, "authority.plan", f"user authorized r{run['requirements_version']} and its authority envelope",
                   audience="runtime")
    from office import guide
    return Result(lines=[f"authorization recorded for r{run['requirements_version']} (+{len(envelope)} envelope entries)"],
                  next=guide.next_action(con, run))


def _envelope_entry(con, run, entry_id, quote) -> Result:
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        envelope = list(run.get("envelope") or [])
        hit = [e for e in envelope if e.get("id") == entry_id]
        if not hit:
            raise Usage("unknown-entry", f"no authority entry {entry_id}", next_step="office inspect run")
        for e in envelope:
            if e.get("id") == entry_id:
                e["needs_authorization"] = False
        state.update_run(con, run["id"], envelope=envelope)
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, authorized_by, quote, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", ("Z" + uuid.uuid4().hex[:8], run["id"], "envelope-entry", entry_id,
                                                run["requirements_version"], "user", quote.strip(), now_iso()))
    return Result(lines=[f"{entry_id} ({hit[0]['action']}) authorized"], next="exceptions only; office status")


def plan_names(con, run: dict, entry: dict) -> bool:
    """Whether the current plan still names this envelope entry: the same action with the same
    preconditions, the key `amend` uses to tell a new entry from an old one."""
    from office import planfile
    plan = state.current_plan(con, run["id"])
    key = (entry.get("action"), tuple(entry.get("preconditions") or []))
    return bool(plan) and any((a["action"], tuple(a.get("preconditions") or [])) == key
                              for a in planfile.parse(plan["body"]).requirements.get("named_actions") or [])


def decline_entry(con, run: dict, entry_id: str, reason: str | None) -> Result:
    """Drop an authority entry nobody authorized once the plan no longer names its action. It grants
    nothing, so the orchestrator may record it; the dropped entry stays on the authorization record."""
    from office import guide
    entry_id = entry_id.upper()
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-decline", "a worker cannot change the authority envelope")
    if not reason or not reason.strip():
        raise Usage("reason-required", "recording a declined entry needs the reason",
                    next_step=f'office decline {entry_id} --reason "<why the action left the plan>"')
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        envelope = list(run.get("envelope") or [])
        hit = next((e for e in envelope if e.get("id") == entry_id), None)
        if hit is None:
            raise Usage("unknown-entry", f"no authority entry {entry_id}", next_step="office inspect run")
        if not hit.get("needs_authorization"):
            raise Refused("entry-authorized", f"{entry_id} ({hit['action']}) is authorized; an authorized entry stays",
                          next_step="office status")
        if plan_names(con, run, hit):
            raise Refused("entry-in-plan", f"{entry_id} ({hit['action']}) is still named by the plan",
                          next_step=f"remove it from the plan (office amend plan --contract -- {shlex.quote('drop ' + hit['action'])}), "
                                    f'or ask the user to authorize it: office approve {entry_id} --quote "<words>"')
        state.update_run(con, run["id"], envelope=[e for e in envelope if e is not hit])
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, authorized_by, "
                    "quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("Z" + uuid.uuid4().hex[:8], run["id"], "envelope-decline", entry_id, run["requirements_version"],
                     dumps(hit), "orchestrator", reason.strip(), now_iso()))
        state.emit(con, run, "authority.envelope_decline", f"{entry_id} ({hit['action']}) declined: {reason.strip()[:120]}",
                   audience="runtime")
    return Result(lines=[f"{entry_id} ({hit['action']}) dropped from the authority envelope (recorded)"],
                  next=guide.next_action(con, state.get_run(con, run["id"])))


def _trust(con, run, triple: str, quote: str) -> Result:
    """#134: the missing CLI path for an explicit, attributed trust act."""
    from office import paths
    act = scoring.record_trust_act(paths.runs_db(), triple, "proven", "user",
                                   f"user authorized in run {short(run['id'])}: {quote.strip()}",
                                   evidence_reference=f"run:{run['id']}")
    return Result(lines=[f"{triple} trust recorded as proven (attributed to the user)"],
                  next="retry the dispatch that needed it",
                  data={"trust_act_id": act["trust_act_id"]})


def _waive(con, run, spec: str, quote: str, root_cause: str | None = None) -> Result:
    from office import gates, plans, redirect
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, authorized_by, quote, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", ("Z" + uuid.uuid4().hex[:8], run["id"], "waiver", spec,
                                                run["requirements_version"], "user", quote.strip(), now_iso()))
        if spec.lower() == "plan-review":
            pr = dict(run.get("plan_review") or {})
            pr.update({"ended": True, "ended_reason": "waived by the user"})
            state.update_run(con, run["id"], plan_review=pr)
            # No reviewer will run again, so nothing could ever clear an open
            # defect: record each as a waived gap and release what it paused.
            con.execute("UPDATE findings SET state='waived', updated_at=? WHERE run_id=? AND gate_kind='plan_review' "
                        "AND state='open'", (now_iso(), run["id"]))
            con.execute("UPDATE gates SET status='cancelled', stale_reason='plan review waived' WHERE run_id=? "
                        "AND kind='plan_review' AND status IN ('queued','running')", (run["id"],))
            plans.unpause_cleared(con, run)
            state.emit(con, run, "authority.waiver", "user waived further plan review")
            line = "plan review waived by the user (recorded)"
        elif re.fullmatch(r"P\d+", spec, re.I):
            # The user judged a plan defect wrong: the requirement stands.
            line = redirect.waive(con, run, spec, quote, root_cause)
        else:
            m = re.fullmatch(r"(T\d+):(checks|code_review|code|visual|ui)", spec, re.I)
            if not m:
                raise Usage("bad-waiver", f"cannot waive {spec!r}", next_step="office approve waive T2:visual --quote ...")
            from office import contract
            if contract.is_convergence(run) and m.group(2).lower() != "checks":
                raise Usage("bad-waiver", f"{spec}: under the {contract.CONVERGENCE} contract code and visual review "
                            "are lane gates", next_step='office approve waive L-<task>:convergence|visual --quote '
                                                         '"<words>" --reason "<why>" (office inspect convergence)')
            tid, kind = m.group(1).upper(), {"code": "code_review", "ui": "visual"}.get(m.group(2).lower(), m.group(2).lower())
            task = state.get_task(con, run["id"], tid)
            if task is None:
                raise Usage("unknown-task", f"no task {tid}")
            # A review of this kind still in flight (an escalation) must not
            # land after the waiver and reopen the task: its result goes stale.
            con.execute("UPDATE gates SET status='cancelled', stale_reason='gate waived by the user' WHERE run_id=? "
                        "AND task_id=? AND kind=? AND status IN ('queued','running','waiting')", (run["id"], tid, kind))
            state.update_task(con, run["id"], tid, status=gates.derive_status(con, run, task) if task["status"] in ("paused", "blocked") else task["status"],
                              pause_reason=None)
            state.emit(con, run, "authority.waiver", f"user waived the {kind} gate for {tid} (named gap recorded)", task_id=tid)
            gates.evaluate_acceptance(con, run, tid)
            line = f"{tid} {kind} gate waived by the user; the gap is recorded on the task"
    jobs.kick(con, run["id"])
    return Result(lines=[line], next="exceptions only; office status")


VISUAL_USAGE = ('office approve visual T2 --by <harness>/<model>[@effort] --report <review file> '
                '--quote "<user\'s words>"')


def _visual_verdict(con, run, tid: str, by: str, report: Path, quote: str) -> Result:
    """#211: record a visual review the user had run outside Office as the
    task's visual gate result. The report must parse in the visual format;
    acceptance is still evaluated by the runtime against every other gate.
    Independence is per agent: the user attests (the quote) that the report
    came from a session other than the producer's. The producer's model or
    family is not a bar, so a fresh session of the same model may review."""
    from office import candidates, gates, review_parse
    if not report.is_file():
        raise Usage("no-report", f"no review file at {report}", next_step=VISUAL_USAGE)
    reviewer = candidates.parse_route_override(by)
    if not reviewer.get("harness") or not reviewer.get("model_id"):
        raise Usage("invalid-reviewer", f"--by {by!r}: expected <harness>/<model>[@effort]", next_step=VISUAL_USAGE)
    parsed = review_parse.parse(gates._last_block(report.read_text(encoding="utf-8", errors="replace")), visual=True)
    if not parsed.valid:
        raise Refused("report-invalid", "the report is not a valid visual review: " + "; ".join(parsed.errors[:3]),
                      next_step="have the reviewer rewrite it in the visual review format, then retry")
    from office import contract
    if contract.is_convergence(run):
        raise Refused("lane-visual", "under the convergence contract visual review is a lane gate",
                      next_step="office inspect convergence; a specialist re-run: office resume; or a waiver")
    if parsed.verdict not in ("PASS", "CHANGES_REQUIRED"):
        raise Refused("report-not-a-verdict", f"the report's verdict is {parsed.verdict}; only PASS or "
                      "CHANGES_REQUIRED can stand in for the gate", next_step=f"office approve waive {tid}:visual")
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        task = state.get_task(con, run["id"], tid)
        if task is None or not task.get("current_revision_id"):
            raise Usage("unknown-task", f"{tid} has no submitted revision to review")
        rev_id = task["current_revision_id"]
        latest = [g for g in gates.required_gates(con, run, task, rev_id) if g["kind"] == "visual"]
        if not latest:
            raise Refused("no-visual-gate", f"{tid} revision {rev_id} has no visual gate", next_step="office status")
        prior = latest[0]
        if prior["status"] in ("queued", "running", "waiting"):
            raise Refused("visual-gate-busy", f"{tid} visual gate {prior['id']} is {prior['status']}; Office is still "
                          "running it", next_step="office wait, then retry if it ends UNAVAILABLE")
        if prior["verdict"] == "PASS":
            return Result(lines=[f"{tid} visual gate already PASS on {rev_id}"], next="office status")
        gid = gates._new_gate(con, run, task, rev_id, "visual", prior["input_key"] + ":external", "running",
                              round_no=prior["round"])
        con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, authorized_by, quote, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", ("Z" + uuid.uuid4().hex[:8], run["id"], "external-visual", f"{tid}:{gid}",
                                                run["requirements_version"], "user", quote.strip(), now_iso()))
        state.record_evidence(con, run["id"], "review_output", report, task_id=tid, revision_id=rev_id, gate_id=gid,
                              meta={"route": by, "external": True, "replaces_gate": prior["id"]})
        if task["status"] in ("paused", "blocked"):
            state.update_task(con, run["id"], tid, status="submitted", pause_reason=None)
        state.emit(con, run, "authority.external_visual", f"user recorded an external visual review of {tid} by {by}",
                   task_id=tid)
        gates.ingest_task_gate(con, run, gid, {"verdict": parsed.verdict, "parsed": parsed, "route": by,
                                               "evidence_status": parsed.evidence_status,
                                               "summary": f"{parsed.verdict} by {by} (external, recorded by the user)"})
        status = state.get_task(con, run["id"], tid)["status"]
    jobs.kick(con, run["id"])
    return Result(lines=[f"{tid} visual {parsed.verdict} by {by} recorded on {rev_id} | {tid} {status}"],
                  next="exceptions only; office status")


def waived(con, run_id: str, task_id: str) -> set[str]:
    out = set()
    for r in con.execute("SELECT target FROM authorizations WHERE run_id=? AND kind='waiver' AND revoked_at IS NULL",
                         (run_id,)).fetchall():
        m = re.fullmatch(r"(T\d+):(\w+)", r["target"] or "", re.I)
        if m and m.group(1).upper() == task_id:
            kind = m.group(2).lower()
            out.add({"code": "code_review", "ui": "visual"}.get(kind, kind))
    return out
