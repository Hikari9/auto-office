"""XL-only Elephant/Goldfish plan review, under one authoritative lead gate.

The Elephant is the one lead plan reviewer. Goldfish are fresh, separate
reviewer sessions that each audit the plan from one rubric and never see the
lead's or another Goldfish's conversation. The runtime launches them itself,
even for a harness that cannot spawn nested agents.

Only the INITIAL plan review of a pinned XL run uses them: never a later
RECHECK round, an amendment's review, an XS/S/M/L run, a legacy-pinned run, or a
run whose gear was merely inferred. A Goldfish verdict is advisory evidence, not
a plan-review gate, a substantive round or a budget. A missing or failed
Goldfish blocks the lead from running (fail closed), so one lead verdict
(APPROVED | RECHECK | INTAKE_GAP) always rests on all required evidence.
No plan reviewer determines or escalates the pinned run gear.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from office import contract, db, paths, state
from office.util import now_iso

MANDATORY = (
    ("requirements", "Requirements and comprehension: check every frozen requirement for comprehension, "
                     "contradictions, missing assumptions and user-owned decisions."),
    ("architecture", "Architecture, reuse, simplicity and scalability: inspect the actual repository for existing "
                     "reusable helpers; review architecture, simplicity, scalability, performance and failure modes."),
    ("readiness", "Implementation readiness and codebase reality: validate real code and API/schema assumptions, "
                  "ownership, dependencies, tests, integration and recovery readiness."),
)
MAX_HAZARDS = 2
GOLDFISH_KIND = "goldfish_audit"


def hazard_audits(run: dict, plan: dict) -> list[tuple[str, str, str]]:
    """At most MAX_HAZARDS optional specialists, each with a recorded concrete trigger."""
    risk = run.get("risk") or {}
    tasks = plan.get("tasks") or []
    hazards = []
    if risk.get("irreversible") or risk.get("blast_radius") in ("production", "production-data"):
        hazards.append(("safety", "Audit auth, authorization, irreversible actions, migration/rollback, data integrity and risk floors.",
                        "run declares production, production-data or irreversible effects"))
    if any(t.get("visual") and not (t["visual"] or {}).get("none") for t in tasks):
        hazards.append(("interaction", "Audit user-facing accessibility, responsive interaction, states, capture evidence and visual regression.",
                        "plan includes visual-impact tasks"))
    if len(hazards) < MAX_HAZARDS and any(any(k in str(p).lower() for k in ("auth", "security", "permission", "secret"))
                                          for t in tasks for p in t.get("scope") or []):
        hazards.append(("security", "Audit trust boundaries, secret exposure, permission enforcement and negative security tests.",
                        "task ownership includes security-sensitive paths"))
    return hazards[:MAX_HAZARDS]


def pinned_xl(run: dict) -> bool:
    """The XL swarm is pinned by the RUN's gear, never by a subtask size or a reviewer suggestion."""
    return (run.get("gear") == "XL" and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"
            and contract.is_convergence(run))


def initial_review(con, run: dict, gate: dict) -> bool:
    """No plan review of this run has returned a verdict yet. A retry after an UNAVAILABLE or invalid attempt
    is still the initial review (no substantive round was spent); the first verdict ends it."""
    return con.execute("SELECT 1 FROM gates WHERE run_id=? AND kind='plan_review' AND id<>? AND review_status=?",
                       (run["id"], gate["id"], contract.COMPLETED)).fetchone() is None


def eligible(con, run: dict, gate: dict) -> bool:
    return pinned_xl(run) and initial_review(con, run, gate)


def _make_gate(con, run: dict, gate: dict, name: str, reason: str) -> dict:
    ident = "G" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, "
                "round, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, run["id"], f"goldfish:{name}", gate["plan_version"], GOLDFISH_KIND,
                 f"goldfish:{gate['id']}:{name}", "queued", gate["round"], now_iso()))
    state.emit(con, run, "plan.goldfish.queued", f"XL Goldfish {name} queued: {reason}",
               payload={"gate_id": ident, "lead_gate": gate["id"], "rubric": name, "selection_reason": reason})
    return {"id": ident, "task_id": None, "plan_version": gate["plan_version"], "round": gate["round"]}


def _audit_brief(lead_brief: str, rubric: str, rubric_text: str) -> str:
    role_line, _, rest = lead_brief.partition("\n")  # the role line stays first: it routes the reviewer
    return (role_line + "\n"
            "INDEPENDENT XL GOLDFISH AUDIT - " + rubric.upper() + "\n"
            "You are one independent reviewer, NOT the plan author or Office's gear-policy reviewer. "
            "Do not request a different run gear or more Goldfish; those are runtime decisions. "
            "Read ONLY the frozen requirements, submitted plan and repository evidence below. "
            "Do not consume the Elephant's conversation, private reasoning or another audit. "
            "You must genuinely examine files in the provided checkout and report verifiable findings; "
            "a no-output or invalid response is a failed audit. Your VERDICT and findings are ADVISORY "
            "and do not count as a plan-review gate or budget.\n"
            "Specific independent rubric: " + rubric_text + "\n" + rest)


def _summary(name: str, parsed) -> dict:
    # Store only the externally reportable structured report, not private chain of thought.
    return {"rubric": name, "advisory_verdict": parsed.verdict,
            "findings": [{k: f.get(k) for k in ("code", "location", "summary", "action", "blocking", "severity", "evidence")}
                         for f in (parsed.findings or []) + (parsed.defects or [])],
            "decision": parsed.decision, "next_action": parsed.next_action}


def _genuine(con, run: dict, outcome: dict, *, live: bool) -> bool:
    """A Goldfish counts only as a completed, valid, launched review that left a reply."""
    parsed = outcome.get("parsed")
    if not (parsed is not None and getattr(parsed, "valid", False) and parsed.verdict in contract.VERDICTS
            and outcome.get("status") == contract.COMPLETED and outcome.get("dispatch_id")):
        return False
    if not live:
        return True
    if not con.execute("SELECT 1 FROM dispatches WHERE id=? AND run_id=? AND role='plan_reviewer'",
                       (outcome["dispatch_id"], run["id"])).fetchone():
        return False
    reply = paths.run_dir(run["id"]) / "dispatches" / outcome["dispatch_id"] / "reply.txt"
    return reply.is_file() and reply.stat().st_size > 0


def _session_of(con, dispatch_id: str | None) -> str | None:
    row = con.execute("SELECT session_id FROM dispatches WHERE id=?", (dispatch_id,)).fetchone() if dispatch_id else None
    return row["session_id"] if row else None


def _blocked(reason: str) -> dict:
    return {"status": contract.EVIDENCE_BLOCKED, "verdict": None, "parsed": None, "summary": reason}


def audit_plan(con, run: dict, gate: dict, plan: dict, lead_brief: str, cwd: Path,
               *, reviewer=None, exclude: list[str] | None = None) -> dict:
    """Execute the Goldfish, then one lead reviewer; return a normal plan-gate outcome.

    Each Goldfish dispatches independently with a unique gate, brief, evidence file and session,
    and is never resumed. Errors never become an APPROVED verdict; the lead stays unlaunched when
    any Goldfish fails, and the outcome is a runtime status (no round is spent).
    """
    if not eligible(con, run, gate):
        raise ValueError("XL Goldfish audits require the initial plan review of a pinned XS-XL XL run")
    live = reviewer is None
    if reviewer is None:
        from office import gates
        reviewer = gates.run_reviewer
    specs = [(name, rubric, "mandatory XL review") for name, rubric in MANDATORY]
    specs += hazard_audits(run, plan)
    summaries, dispatches, sessions = [], [], []
    for name, rubric, reason in specs:
        with db.transaction(con):
            audit_gate = _make_gate(con, run, gate, name, reason)
        try:
            outcome = reviewer(con, run, audit_gate, "plan_reviewer", _audit_brief(lead_brief, name, rubric),
                               cwd=cwd, plan_review=True, kind="reviewer")
        except Exception as exc:
            outcome = {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
        did = outcome.get("dispatch_id")
        session = _session_of(con, did)
        # One reviewer session per Goldfish: a reused dispatch or session is not independent evidence.
        good = _genuine(con, run, outcome, live=live) and did not in dispatches and (not session or session not in sessions)
        dispatches.append(did)
        if session:
            sessions.append(session)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', review_status=?, summary=?, route=?, finished_at=?, "
                        "reviewer_dispatch_id=? WHERE id=?",
                        (contract.COMPLETED if good else contract.EVIDENCE_BLOCKED,
                         (outcome.get("summary") or "missing or invalid Goldfish report")[:500],
                         outcome.get("route"), now_iso(), did, audit_gate["id"]))
            state.emit(con, run, "plan.goldfish.report" if good else "plan.goldfish.blocked",
                       f"XL Goldfish {name}: {'report captured' if good else 'report missing or invalid'}",
                       payload={"gate_id": audit_gate["id"], "rubric": name, "success": bool(good),
                                "dispatch_id": did, "report": _summary(name, outcome["parsed"]) if good else None})
        if not good:
            return _blocked(f"XL Goldfish {name}: genuine independent report unavailable/invalid; "
                            "one lead verdict is not permitted without all required evidence")
        summaries.append(_summary(name, outcome["parsed"]))
    lead_append = ["\nXL LEAD PLAN REVIEW: synthesize the independently sourced advisory audits below into ONE "
                   "authoritative verdict under the original plan-review contract. Deduplicate findings, "
                   "verify every claimed repo fact, and resolve contradictions by evidence. "
                   "You are NOT evaluating Office's gear policy. No additional review gate or round is created."]
    for report in summaries:
        lead_append.append(f"AUDIT {report['rubric']}: advisory={report['advisory_verdict']}; "
                           f"findings={report['findings']}; decision={report['decision']}; "
                           f"next={report['next_action']}")
    outcome = reviewer(con, run, gate, "plan_reviewer", lead_brief + "\n" + "\n".join(lead_append),
                       cwd=cwd, plan_review=True, exclude=exclude)
    lead = outcome.get("dispatch_id")
    if lead and (lead in dispatches or (_session_of(con, lead) or object()) in sessions):
        return _blocked("XL lead plan review reused a Goldfish session; the Elephant must be a distinct session")
    outcome["goldfish"] = {"completed": len(summaries), "required": len(specs), "mandatory": len(MANDATORY),
                           "hazards": [s[0] for s in specs[len(MANDATORY):]], "dispatches": dispatches}
    return outcome
