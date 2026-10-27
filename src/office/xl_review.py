"""XL-only Elephant/Goldfish plan review, under one authoritative lead gate.

The runtime launches genuinely separate reviewer sessions, even for a harness
that cannot spawn nested agents. Auxiliary audit gates are never plan-review
verdicts or substantive rounds. Missing/unparseable reports block advancement.
No plan reviewer determines or escalates the pinned run gear.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from office import contract, db, paths, state
from office.util import now_iso

MANDATORY = (
    ("requirements", "Check every frozen requirement for comprehension, contradictions, missing assumptions and user-owned decisions."),
    ("architecture", "Inspect the actual repository for existing reusable helpers; review architecture, simplicity, scalability, performance and failure modes."),
    ("readiness", "Validate real code and API/schema assumptions, ownership, dependencies, tests, integration and recovery readiness."),
)


def hazard_audits(run: dict, plan: dict) -> list[tuple[str, str, str]]:
    """At most two optional hazards, each with a recorded concrete trigger."""
    risk = run.get("risk") or {}
    tasks = plan.get("tasks") or []
    hazards = []
    if risk.get("irreversible") or risk.get("blast_radius") in ("production", "production-data"):
        hazards.append(("safety", "Audit auth, authorization, irreversible actions, migration/rollback, data integrity and risk floors.",
                        "run declares production, production-data or irreversible effects"))
    if any(t.get("visual") and not (t["visual"] or {}).get("none") for t in tasks):
        hazards.append(("interaction", "Audit user-facing accessibility, responsive interaction, states, capture evidence and visual regression.",
                        "plan includes visual-impact tasks"))
    if len(hazards) < 2 and any(any(k in str(p).lower() for k in ("auth", "security", "permission", "secret"))
                                for t in tasks for p in t.get("scope") or []):
        hazards.append(("security", "Audit trust boundaries, secret exposure, permission enforcement and negative security tests.",
                        "task ownership includes security-sensitive paths"))
    return hazards[:2]


def eligible(run: dict) -> bool:
    """The XL swarm is pinned by the RUN, never by a subtask or reviewer suggestion."""
    return run.get("gear") == "XL" and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"


def _make_gate(con, run: dict, gate: dict, name: str, reason: str) -> dict:
    ident = "G" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, "
                "round, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, run["id"], f"goldfish:{name}", gate["plan_version"], "goldfish_audit",
                 f"goldfish:{gate['id']}:{name}", "queued", gate["round"], now_iso()))
    state.emit(con, run, "plan.goldfish.queued", f"XL Goldfish {name} queued: {reason}",
               payload={"gate_id": ident, "lead_gate": gate["id"], "rubric": name, "selection_reason": reason})
    return {"id": ident, "task_id": None, "plan_version": gate["plan_version"], "round": gate["round"]}


def _audit_brief(lead_brief: str, rubric: str, rubric_text: str) -> str:
    return ("INDEPENDENT XL GOLDFISH AUDIT — " + rubric.upper() + "\n"
            "You are one independent reviewer, NOT the plan author or Office's gear-policy reviewer. "
            "Do not request a different run gear or more Goldfish; those are runtime decisions. "
            "Read ONLY the frozen requirements, submitted plan and repository evidence below. "
            "Do not consume the Elephant's conversation, private reasoning or another audit. "
            "You must genuinely examine files in the provided checkout and report verifiable findings; "
            "a no-output or invalid response is a failed audit. Your VERDICT and findings are ADVISORY "
            "and do not count as a plan-review gate or budget.\n"
            "Specific independent rubric: " + rubric_text + "\n\n" + lead_brief)


def _summary(name: str, parsed) -> dict:
    # Store only the externally reportable structured report, not private chain of thought.
    return {"rubric": name, "advisory_verdict": parsed.verdict,
            "findings": [{k: f.get(k) for k in ("code", "location", "summary", "action", "blocking", "severity", "evidence")}
                         for f in (parsed.findings or []) + (parsed.defects or [])],
            "decision": parsed.decision, "next_action": parsed.next_action}


def audit_plan(con, run: dict, gate: dict, plan: dict, lead_brief: str, cwd: Path,
               *, reviewer=None) -> dict:
    """Execute Goldfish before one lead reviewer; return a normal plan-gate outcome.

    Runs only on genuine XL. Auxiliary reviewers dispatch independently with a
    unique gate, brief, working directory, evidence file and session. Errors
    never become an APPROVED verdict; the lead remains unlaunched on a failure.
    """
    if not eligible(run):
        raise ValueError("XL audits require a pinned XS-XL XL run")
    live = reviewer is None
    if reviewer is None:
        from office import gates
        reviewer = gates.run_reviewer
    specs = [(name, rubric, "mandatory XL review") for name, rubric in MANDATORY]
    specs += hazard_audits(run, plan)
    summaries = []
    for name, rubric, reason in specs:
        with db.transaction(con):
            audit_gate = _make_gate(con, run, gate, name, reason)
        try:
            outcome = reviewer(con, run, audit_gate, "plan_reviewer", _audit_brief(lead_brief, name, rubric),
                               cwd=cwd, plan_review=True, kind="reviewer")
        except Exception as exc:
            outcome = {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
        parsed = outcome.get("parsed")
        valid = (parsed is not None and getattr(parsed, "valid", False) and
                 getattr(parsed, "verdict", None) in (contract.VERDICTS if contract.is_convergence(run)
                                                    else contract.LEGACY_VERDICTS))
        launched = bool(outcome.get("dispatch_id"))
        if live and launched:
            launched = bool(con.execute("SELECT 1 FROM dispatches WHERE id=? AND run_id=? AND role='plan_reviewer'",
                                        (outcome["dispatch_id"], run["id"])).fetchone())
            reply = paths.run_dir(run["id"]) / "dispatches" / outcome["dispatch_id"] / "reply.txt"
            launched = launched and reply.is_file() and reply.stat().st_size > 0
        good = (valid and launched and outcome.get("status") == contract.COMPLETED)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', review_status=?, summary=?, route=?, finished_at=?, "
                        "reviewer_dispatch_id=? WHERE id=?",
                        (contract.COMPLETED if good else contract.EVIDENCE_BLOCKED,
                         (outcome.get("summary") or "missing or invalid Goldfish report")[:500],
                         outcome.get("route"), now_iso(), outcome.get("dispatch_id"), audit_gate["id"]))
            state.emit(con, run, "plan.goldfish.report" if good else "plan.goldfish.blocked",
                       f"XL Goldfish {name}: {'report captured' if good else 'report missing or invalid'}",
                       payload={"gate_id": audit_gate["id"], "rubric": name, "success": bool(good),
                                "dispatch_id": outcome.get("dispatch_id"), "report": _summary(name, parsed) if good else None})
        if not good:
            return {"status": contract.EVIDENCE_BLOCKED, "verdict": None, "parsed": None,
                    "summary": f"XL Goldfish {name}: genuine independent report unavailable/invalid; "
                               "one lead verdict is not permitted without all required evidence"}
        summaries.append(_summary(name, parsed))
    lead_append = ["\nXL LEAD PLAN REVIEW: synthesize the independently sourced advisory audits below into ONE "
                   "authoritative verdict under the original plan-review contract. Deduplicate findings, "
                   "verify every claimed repo fact, and resolve contradictions by evidence. "
                   "You are NOT evaluating Office's gear policy. No additional review gate or round is created."]
    for report in summaries:
        lead_append.append(f"AUDIT {report['rubric']}: advisory={report['advisory_verdict']}; "
                           f"findings={report['findings']}; decision={report['decision']}; "
                           f"next={report['next_action']}")
    outcome = reviewer(con, run, gate, "plan_reviewer", lead_brief + "\n" + "\n".join(lead_append),
                       cwd=cwd, plan_review=True)
    outcome["goldfish"] = {"completed": len(summaries), "required": len(specs),
                            "mandatory": len(MANDATORY), "hazards": [s[0] for s in specs[3:]]}
    return outcome
