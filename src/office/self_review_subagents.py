"""#938: one read-only, combined task audit before convergence, never a deep swarm.

The executor first completes its own single pass. This Office-funded audit is advisory,
not a convergence verdict. Sensitive lanes receive a distinct mandatory security review
of the composed result at convergence.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from office import briefs, contract, db, paths, state
from office.util import now_iso

REVIEW_VERSION = "runtime-self-review-2"
FUNDED_GEARS = ("S", "M", "L", "XL")
RUBRICS = {
    "combined": "Inspect edge cases and test strength (including weak assertions) in ONE read-only pass. "
                "Inspect platform/build only when the changed files touch scripts, CI, packaging, OS/process. "
                "Do not repeat security here: sensitive lanes receive a mandatory convergence security pass. "
                "Try to falsify the actual acceptance evidence without changing any source files.",
}
_PLATFORM_MARKS = ("scripts/", ".github/", ".githooks/", "dockerfile", "pyproject.toml",
                   "setup.py", "makefile", ".sh", ".yml", ".yaml", ".toml", "package.json")
_PLATFORM_WORDS = ("process", "subprocess", "signal", "thread", "multiprocessing", "os/" )
_SECURITY_WORDS = ("auth", "permission", "secret", "token", "credential", "password", "access_control",
                   "input", "parse", "parser", "write", "payment", "billing", "prod", "security")


def platform_relevant(files) -> bool:
    """Diff-surface selection, not run gear or a broad task descriptor."""
    return any(any(mark in str(path).lower() for mark in _PLATFORM_MARKS)
               or any(word in str(path).lower().split("/")[-1] for word in _PLATFORM_WORDS)
               for path in files)


_SENSITIVE_ADDITION = re.compile(
    r"\b(?:authenticat\w*|authoriz\w*|permissio\w*|secret\w*|credential\w*|password\w*|"
    r"tokens?|jwt|oauth|access_control|input_pars\w*|parse_input|deserializ\w*|"
    r"sql_exec\w*|insert\s+into|delete\s+from|update\s+\w+\s+set|"
    r"production\s+(?:write|update|delete)|write_production)\b", re.I)


def security_relevant(files, diff: str = "") -> bool:
    """Pick sensitive lanes by actual changed paths AND security-sensitive added code.

    Ignore documentation/tests in diff-scanning; a generic filename like handler.py
    can still add an authorization check or production write that requires audit.
    """
    if any(any(word in str(path).lower() for word in _SECURITY_WORDS) for path in files):
        return True
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = line.partition(" b/")[2].strip()
        elif (line.startswith("+") and not line.startswith("+++")
              and current and not current.startswith(("tests/", "test/", "docs/"))
              and "/test_" not in current and Path(current).suffix.lower() in
              (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".kt", ".sql", ".php")
              and _SENSITIVE_ADDITION.search(line[1:])):
            return True
    return False


def funded(run: dict) -> bool:
    return run.get("gear") in FUNDED_GEARS and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"


def rubrics(run: dict, task: dict) -> tuple[str, ...]:
    """The only funded task self-review is one combined edge/test (+ conditional platform) pass."""
    return ("combined",) if funded(run) else ()


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
            diff = gates.cap_diff(paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"]))[:12000]
            checks = con.execute("SELECT summary FROM gates WHERE revision_id=? AND kind='checks' AND status='done' "
                                 "ORDER BY created_at DESC LIMIT 1", (rid,)).fetchone()
            import json
            changed = json.loads(rev.get("changed_json") or "[]")
            surface = ("Platform/build is REQUIRED for changed files: " + ", ".join(changed[:15])
                       if platform_relevant(changed) else "Platform/build is OUT OF SCOPE for this diff.")
            brief = briefs.self_review_subagent_brief(run, task, rev, commit, diff,
                                                      checks["summary"] if checks else "none declared",
                                                      str(checkout), rubric, RUBRICS[rubric] + " " + surface)
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


def audit_security(con, run: dict, scope: dict, tasks: list[dict], checkout: Path, commit: str,
                   diff: str, *, reviewer=None) -> dict:
    """Mandatory, independent, composed-lane security review for sensitive changed paths.

    This is not task self-review and is not a convergence verdict. A RECHECK or
    INTAKE_GAP result is routed into the authoritative convergence gate unchanged;
    absence, invalidity and wrong-commit responses block convergence.
    """
    from office import convergence, gates
    files = sorted({path for task in tasks for path in convergence._changed(con, run, task)})
    if not security_relevant(files, diff):
        return {"status": contract.COMPLETED, "required": False, "reason": "no sensitive changed surface"}
    key = f"{scope['id']}:{commit}:security-convergence-1"
    saved = con.execute("SELECT * FROM gates WHERE run_id=? AND kind='convergence_security' AND input_key=? "
                        "AND status='done' AND review_status=? ORDER BY finished_at DESC LIMIT 1",
                        (run["id"], key, contract.COMPLETED)).fetchone()
    if saved and saved["verdict"] == "APPROVED":
        return {"status": contract.COMPLETED, "required": True, "verdict": "APPROVED",
                "reason": saved["summary"], "reused": True}
    gate = {"id": "G" + uuid.uuid4().hex[:8], "task_id": tasks[0]["id"] if tasks else None,
            "scope": scope["id"], "revision_id": None, "plan_version": run.get("plan_version")}
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, scope, task_id, plan_version, kind, input_key, status,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gate["id"], run["id"], scope["id"], scope["id"], gate["task_id"], run.get("plan_version"),
                     "convergence_security", key, "queued", now_iso()))
        state.emit(con, run, "convergence.security.queued",
                   f"{scope['id']}: mandatory independent security pass on composed commit {commit[:12]}",
                   payload={"files": files[:25], "commit": commit})
    brief = ("ROLE independent convergence SECURITY reviewer (mandatory, read-only). Change no source.\n"
             "Do not trust the producer's self-review. Examine the composed result and cross-task seams for "
             "auth, permissions, secrets, unsafe input parsing, trust boundaries and unauthorized or repeated "
             "production writes. Report every security defect, including security issues in code reached by "
             "the changed paths; stay within the lane acceptance contract.\n"
             f"SCOPE {scope['id']} tasks: {', '.join(t['id'] for t in tasks)}\n"
             f"SENSITIVE FILES: {', '.join(files[:40])}\n"
             f"REVIEWED_COMMIT {commit} is the exact commit under review; state it in your reply.\n"
             "No tools may edit or delete source and no destructive cleanup commands.\n"
             + briefs.CONVERGENCE_REVIEW_FORMAT
             + "\nDIFF (composed lane):\n" + diff[:25000])
    try:
        outcome = (reviewer or gates.run_reviewer)(con, run, gate, "code_reviewer", brief,
                                                    cwd=checkout, include_dirs=[checkout], kind="reviewer")
    except Exception as exc:
        outcome = {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
    good, reason = _valid(outcome, commit, run)
    parsed = outcome.get("parsed")
    verdict = parsed.verdict if good else None
    with db.transaction(con):
        con.execute("UPDATE gates SET status='done', review_status=?, verdict=?, summary=?, route=?, "
                    "reviewer_dispatch_id=?, finished_at=? WHERE id=?",
                    (contract.COMPLETED if good else contract.EVIDENCE_BLOCKED,
                     verdict, (outcome.get("summary") or reason)[:500], outcome.get("route"),
                     outcome.get("dispatch_id"), now_iso(), gate["id"]))
        state.emit(con, run, "convergence.security.completed" if good else "convergence.security.blocked",
                   f"{scope['id']} security: {verdict or reason}", payload={"commit": commit, "valid": good})
    if not good:
        return {"status": contract.EVIDENCE_BLOCKED, "required": True,
                "reason": f"mandatory security convergence pass: {reason}"}
    return {"status": contract.COMPLETED, "required": True,
            "verdict": verdict, "outcome": outcome, "reason": reason}
