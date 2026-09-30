"""User authority: authorization, merge, envelope entries, trust, waivers.

These record the user's own words. They surface only when the run needs
them, never in normal role briefs, and nothing else grants authority.
"""
from __future__ import annotations

import re
import uuid

from office import db, jobs, routing, scoring, state
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, short


def approve(con, run: dict, target: str, quote: str | None, extra: list[str] | None = None,
            root_cause: str | None = None) -> Result:
    import os
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-approve", "a worker cannot approve anything")
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
            tid, kind = m.group(1).upper(), {"code": "code_review", "ui": "visual"}.get(m.group(2).lower(), m.group(2).lower())
            task = state.get_task(con, run["id"], tid)
            if task is None:
                raise Usage("unknown-task", f"no task {tid}")
            state.update_task(con, run["id"], tid, status=gates.derive_status(con, run, task) if task["status"] in ("paused", "blocked") else task["status"],
                              pause_reason=None)
            state.emit(con, run, "authority.waiver", f"user waived the {kind} gate for {tid} (named gap recorded)", task_id=tid)
            gates.evaluate_acceptance(con, run, tid)
            line = f"{tid} {kind} gate waived by the user; the gap is recorded on the task"
    jobs.kick(con, run["id"])
    return Result(lines=[line], next="exceptions only; office status")


def waived(con, run_id: str, task_id: str) -> set[str]:
    out = set()
    for r in con.execute("SELECT target FROM authorizations WHERE run_id=? AND kind='waiver' AND revoked_at IS NULL",
                         (run_id,)).fetchall():
        m = re.fullmatch(r"(T\d+):(\w+)", r["target"] or "", re.I)
        if m and m.group(1).upper() == task_id:
            kind = m.group(2).lower()
            out.add({"code": "code_review", "ui": "visual"}.get(kind, kind))
    return out
