"""#809: runtime-owned, genuine, independent pre-convergence self-review agents.

These are *advisory audits* of the producer revision and four lens ledger,
not extra convergence verdicts or review rounds. The existing convergence gate
is the only authoritative acceptance verdict, after deterministic checks. New
S/M runs require one audit per task even when the executor was inline; L runs
fund specialists for justified complexity; XL runs fund two independent audits.
Old pinned runs preserve their existing self-review and gate contracts.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from office import briefs, contract, db, state
from office.util import now_iso

REVIEW_VERSION = "runtime-self-review-1"
RUBRICS = {
    "four-lens": "Review all four lenses independently: security, edge cases, platform/build, and test strength. "
                 "Try to falsify the producer's claims using the actual current checkout, without changing files.",
    "security-recovery": "Review trust, permissions, data integrity, crashes, retries, rollback, and abnormal state transitions.",
    "integration-tests": "Review integration contracts, owned interfaces, deterministic tests, mutation sensitivity and recovery paths.",
}


def rubrics(run: dict, task: dict) -> tuple[str, ...]:
    gear = run.get("gear")
    if gear in ("S", "M"):
        return ("four-lens",)
    if gear == "XL":
        return ("security-recovery", "integration-tests")
    if gear == "L" and ((run.get("risk") or {}).get("classification") == "elevated"
                        or (task.get("descriptor") or {}).get("task_size") in ("L", "XL")
                        or (task.get("descriptor") or {}).get("work") == "architecture"):
        return ("four-lens",)
    return ()


def _valid(outcome: dict, commit: str, run: dict) -> tuple[bool, str]:
    parsed = outcome.get("parsed")
    if outcome.get("status") != contract.COMPLETED or not outcome.get("dispatch_id"):
        return False, "subagent was not launched and completed"
    if parsed is None or not getattr(parsed, "valid", False) or getattr(parsed, "verdict", None) not in contract.VERDICTS:
        return False, "subagent report missing or invalid"
    # A reviewer may return a valid result for a different commit. Reject it.
    from office import convergence
    problem = convergence._stamp_reviewed_commit(run, commit, outcome)
    if problem:
        return False, problem
    return True, "reviewed current revision"


def audit_scope(con, run: dict, tasks: list[dict], checkout: Path, *, reviewer=None) -> dict:
    if reviewer is None:
        from office import gates
        reviewer = gates.run_reviewer
    if run.get("gear") not in ("S", "M", "L", "XL") or \
            (run.get("gates") or {}).get("gear_model_version") != "run-gear-1":
        return {"status": contract.COMPLETED, "reports": [], "reason": "pinned legacy/XS policy"}
    from office import gates
    reports = []
    for task in tasks:
        for rubric in rubrics(run, task):
            rid = task.get("accepted_revision_id")
            if not rid:
                return {"status": contract.EVIDENCE_BLOCKED, "reports": reports,
                        "reason": f"{task['id']}: no accepted revision for subagent audit"}
            rev = con.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone()
            if rev is None:
                return {"status": contract.EVIDENCE_BLOCKED, "reports": reports,
                        "reason": f"{task['id']}: missing revision {rid}"}
            rev = dict(rev)
            key = f"{rid}:{rubric}:{REVIEW_VERSION}"
            saved = con.execute("SELECT * FROM gates WHERE run_id=? AND kind='self_review_subagent' "
                                "AND input_key=? AND review_status=? ORDER BY finished_at DESC LIMIT 1",
                                (run["id"], key, contract.COMPLETED)).fetchone()
            if saved:
                # Do not count old output as a new launch; exact revision+rubric is reusable.
                reports.append({"task": task["id"], "rubric": rubric,
                                "dispatch_id": saved["reviewer_dispatch_id"], "result": saved["summary"],
                                "revision": rid, "reused": True})
                continue
            audit_gate = {"id": "G" + uuid.uuid4().hex[:8], "task_id": task["id"],
                          "revision_id": rid, "plan_version": run.get("plan_version")}
            with db.transaction(con):
                con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, "
                            "input_key, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (audit_gate["id"], run["id"], task["id"], task["id"], rid,
                             run.get("plan_version"), "self_review_subagent", key, "queued", now_iso()))
                state.emit(con, run, "self_review.subagent.queued",
                           f"{task['id']} {rubric}: independent subagent review required before convergence",
                           task_id=task["id"], payload={"revision": rid, "rubric": rubric})
            diff = gates.cap_diff(gates.paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"]))
            checks = con.execute("SELECT summary FROM gates WHERE revision_id=? AND kind='checks' AND status='done' "
                                 "ORDER BY created_at DESC LIMIT 1", (rid,)).fetchone()
            brief = briefs.code_review_brief(run, task, rev, diff, checks["summary"] if checks else "none declared",
                                            [], str(checkout))
            brief = ("MANDATORY INDEPENDENT SELF-REVIEW SUBAGENT (read-only) — " + rubric + ".\n"
                     "You are not the executor, not the convergence reviewer, and not the run-gear planner. "
                     "Give a fresh review of this exact revision; never claim a review without actually inspecting it. "
                     "Start your reply file with REVIEWED_COMMIT " + rev["commit_sha"] + ".\n"
                     "Independent rubric: " + RUBRICS[rubric] + "\n\n" + brief)
            try:
                outcome = reviewer(con, run, audit_gate, "code_reviewer", brief, cwd=checkout,
                                   include_dirs=[checkout], kind="reviewer")
            except Exception as exc:
                outcome = {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
            good, reason = _valid(outcome, rev["commit_sha"], run)
            with db.transaction(con):
                con.execute("UPDATE gates SET status='done', review_status=?, verdict=?, summary=?, route=?, "
                            "reviewer_dispatch_id=?, finished_at=? WHERE id=?",
                            (contract.COMPLETED if good else contract.EVIDENCE_BLOCKED,
                             outcome.get("verdict") if good else None,
                             (outcome.get("summary") or reason)[:500], outcome.get("route"),
                             outcome.get("dispatch_id"), now_iso(), audit_gate["id"]))
                state.emit(con, run, "self_review.subagent.completed" if good else "self_review.subagent.blocked",
                           f"{task['id']} {rubric}: {reason}", task_id=task["id"],
                           payload={"rubric": rubric, "revision": rid,
                                    "dispatch_id": outcome.get("dispatch_id"), "success": good})
            if not good:
                return {"status": contract.EVIDENCE_BLOCKED, "reports": reports,
                        "reason": f"{task['id']} {rubric}: {reason}; independent review cannot proceed"}
            parsed = outcome["parsed"]
            reports.append({"task": task["id"], "rubric": rubric, "revision": rid,
                            "dispatch_id": outcome["dispatch_id"], "advisory": parsed.verdict,
                            "findings": [{k: f.get(k) for k in ("code", "severity", "location", "summary", "action", "blocking")}
                                         for f in parsed.findings], "reused": False})
    return {"status": contract.COMPLETED, "reports": reports}
