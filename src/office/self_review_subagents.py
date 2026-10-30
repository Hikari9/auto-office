"""#809: runtime-funded, independent self-review subagents that precede convergence review.

Office itself launches these as separate reviewer sessions on a task's accepted revision. They
are advisory audits of the producer's work and its four-lens ledger, never a convergence
verdict, a substantive round or an approval. The one authoritative acceptance verdict stays the
convergence review. A missing, invalid or wrong-commit subagent report blocks that review
(fail closed) and spends no round.

Funding by run gear (pinned `run-gear-1` runs only; legacy-pinned runs keep their contract):
  XS      none: the executor's inline four-lens self-check is the whole of it
  S, M    one four-lens subagent per task
  L, XL   up to four specialists per task (security, edge-cases, platform, test-strength),
          chosen by what the task touches; edge-cases and test-strength always run
Each task revision and rubric is reviewed once: a report for the same revision, commit and
rubric is reused on a later convergence round or a review-only re-run.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from office import briefs, contract, db, paths, state
from office.util import now_iso

REVIEW_VERSION = "runtime-self-review-1"
FUNDED_GEARS = ("S", "M", "L", "XL")
RUBRICS = {
    "four-lens": "Review all four lenses independently: security, edge cases, platform/build, and test strength. "
                 "Try to falsify the producer's claims using the actual checkout, without changing files.",
    "security": "Security: secrets, token exposure, auth and permission bypass, missing server-side revalidation, "
                "path or symlink escapes, trust boundaries and irreversible actions.",
    "edge-cases": "Edge cases: parser ambiguity, empty or null input, crashes, retries, restart and offline states, "
                  "interaction paths that bypass an explicit confirmation, data integrity on abnormal transitions.",
    "platform": "Platform and build: macOS vs Linux differences (BSD sed, process groups), packaging, bundled "
                "resources, scripts, CI manifests and stale build artifacts.",
    "test-strength": "Test strength: weak assertions, tests that still pass with the fix reverted, missing negative "
                     "and recovery paths, integration contracts and mutation sensitivity.",
}
_SECURITY_WORDS = ("auth", "security", "permission", "secret", "token", "crypt", "credential")
_PLATFORM_MARKS = ("scripts/", ".github/", ".githooks/", "dockerfile", "pyproject.toml", "setup.py", "makefile",
                   ".sh", ".yml", ".yaml", ".toml", "package.json")


def _security_relevant(run: dict, task: dict) -> bool:
    risk = run.get("risk") or {}
    return bool(risk.get("classification") == "elevated" or risk.get("irreversible")
                or risk.get("blast_radius") in ("production", "production-data")
                or any(w in str(p).lower() for p in task.get("scope") or [] for w in _SECURITY_WORDS))


def _platform_relevant(task: dict) -> bool:
    scope = [str(p).lower() for p in task.get("scope") or []]
    return (any(m in p for p in scope for m in _PLATFORM_MARKS)
            or (task.get("descriptor") or {}).get("domain") == "infrastructure")


def funded(run: dict) -> bool:
    return run.get("gear") in FUNDED_GEARS and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"


def rubrics(run: dict, task: dict) -> tuple[str, ...]:
    """The independent subagent reviews this run's gear funds for one task."""
    if not funded(run):
        return ()
    if run["gear"] in ("S", "M"):
        return ("four-lens",)
    picked = ["edge-cases", "test-strength"]
    if _security_relevant(run, task):
        picked.insert(0, "security")
    if _platform_relevant(task):
        picked.insert(-1, "platform")
    return tuple(picked)


def _valid(outcome: dict, commit: str, run: dict) -> tuple[bool, str]:
    parsed = outcome.get("parsed")
    if outcome.get("status") != contract.COMPLETED or not outcome.get("dispatch_id"):
        return False, "subagent was not launched and completed"
    if parsed is None or not getattr(parsed, "valid", False) or getattr(parsed, "verdict", None) not in contract.VERDICTS:
        return False, "subagent report missing or invalid"
    from office import convergence
    # A reviewer may return a valid report for another commit: that is no review of this one.
    problem = convergence._stamp_reviewed_commit(run, commit, outcome)
    if problem:
        return False, problem
    return True, "reviewed the current commit"


def _reusable(con, run: dict, key: str):
    return con.execute("SELECT * FROM gates WHERE run_id=? AND kind='self_review_subagent' AND input_key=? "
                       "AND review_status=? ORDER BY finished_at DESC LIMIT 1",
                       (run["id"], key, contract.COMPLETED)).fetchone()


def running_dispatch_ids(con, run_id: str) -> set[str]:
    """Open reviewer dispatches that are self-review subagents. Each one owns its gate (#895)."""
    return {row[0] for row in con.execute("SELECT d.id FROM dispatches d JOIN gates g ON g.id=d.gate_id "
                                          "WHERE d.run_id=? AND g.kind='self_review_subagent' AND d.ended_at IS NULL",
                                          (run_id,))}


def _report(task_id: str, rubric: str, rid: str, **extra) -> dict:
    return {"task": task_id, "rubric": rubric, "revision": rid, **extra}


def audit_scope(con, run: dict, tasks: list[dict], checkout: Path, commit: str, *, reviewer=None) -> dict:
    """Run every funded subagent review of `tasks` at `commit` (the checkout's HEAD, the commit convergence
    reviews). Returns {status, reports, reason}; any status but COMPLETED means convergence must not run."""
    if not funded(run):
        return {"status": contract.COMPLETED, "reports": [], "reason": "no runtime self-review funded for this gear"}
    from office import gates
    reviewer = reviewer or gates.run_reviewer
    reports = []
    for task in tasks:
        for rubric in rubrics(run, task):
            rid = task.get("accepted_revision_id")
            rev = con.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone() if rid else None
            if rev is None:
                return {"status": contract.EVIDENCE_BLOCKED, "reports": reports,
                        "reason": f"{task['id']}: no accepted revision for the {rubric} subagent review"}
            rev = dict(rev)
            key = f"{rid}:{commit}:{rubric}:{REVIEW_VERSION}"
            saved = _reusable(con, run, key)
            if saved:
                reports.append(_report(task["id"], rubric, rid, dispatch_id=saved["reviewer_dispatch_id"],
                                       advisory=saved["verdict"], summary=saved["summary"], reused=True))
                continue
            audit_gate = {"id": "G" + uuid.uuid4().hex[:8], "task_id": task["id"], "revision_id": rid,
                          "plan_version": run.get("plan_version")}
            with db.transaction(con):
                con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, "
                            "input_key, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (audit_gate["id"], run["id"], task["id"], task["id"], rid, run.get("plan_version"),
                             "self_review_subagent", key, "queued", now_iso()))
                state.emit(con, run, "self_review.subagent.queued",
                           f"{task['id']} {rubric}: independent subagent review before convergence",
                           task_id=task["id"], payload={"revision": rid, "rubric": rubric, "commit": commit})
            diff = gates.cap_diff(paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"]))
            checks = con.execute("SELECT summary FROM gates WHERE revision_id=? AND kind='checks' AND status='done' "
                                 "ORDER BY created_at DESC LIMIT 1", (rid,)).fetchone()
            brief = briefs.self_review_subagent_brief(run, task, rev, commit, diff,
                                                      checks["summary"] if checks else "none declared",
                                                      str(checkout), rubric, RUBRICS[rubric])
            try:
                outcome = reviewer(con, run, audit_gate, "code_reviewer", brief, cwd=checkout,
                                   include_dirs=[checkout], kind="reviewer")
            except Exception as exc:
                outcome = {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
            good, reason = _valid(outcome, commit, run)
            parsed = outcome.get("parsed")
            with db.transaction(con):
                con.execute("UPDATE gates SET status='done', review_status=?, verdict=?, summary=?, route=?, "
                            "reviewer_dispatch_id=?, finished_at=? WHERE id=?",
                            (contract.COMPLETED if good else contract.EVIDENCE_BLOCKED,
                             parsed.verdict if good else None, (outcome.get("summary") or reason)[:500],
                             outcome.get("route"), outcome.get("dispatch_id"), now_iso(), audit_gate["id"]))
                state.emit(con, run, "self_review.subagent.completed" if good else "self_review.subagent.blocked",
                           f"{task['id']} {rubric}: {reason}", task_id=task["id"],
                           payload={"rubric": rubric, "revision": rid, "commit": commit,
                                    "dispatch_id": outcome.get("dispatch_id"), "success": good})
            if not good:
                return {"status": contract.EVIDENCE_BLOCKED, "reports": reports,
                        "reason": f"{task['id']} {rubric} self-review subagent: {reason}; "
                                  "convergence review cannot proceed without it"}
            reports.append(_report(task["id"], rubric, rid, dispatch_id=outcome["dispatch_id"],
                                   advisory=parsed.verdict, reused=False,
                                   findings=[{k: f.get(k) for k in ("code", "severity", "location", "summary",
                                                                    "action", "blocking")} for f in parsed.findings]))
    return {"status": contract.COMPLETED, "reports": reports}
