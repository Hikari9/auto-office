"""#809: runtime-funded, independent self-review subagents that precede convergence review.

Office itself launches these as separate reviewer sessions on a task's accepted revision. They
are advisory audits of the producer's work and its four-lens ledger, never a convergence
verdict, a substantive round or an approval. The one authoritative acceptance verdict stays the
convergence review. A missing, invalid or wrong-commit subagent report blocks that review
(fail closed) and spends no round.

New (convergence-v1) runs fund one subagent per task for every funded gear: edge cases and test strength in
one read-only pass, plus platform/build only when the task's scope touches it. Security is not a task
self-review: the single convergence reviewer covers it when a lane changes a sensitive path (#938).

Funding by run gear (pinned `run-gear-1` runs; legacy-pinned runs keep the funding below):
  XS      none: the executor's inline four-lens self-check is the whole of it
  S, M    one four-lens subagent per task
  L, XL   up to four specialists per task (security, edge-cases, platform, test-strength),
          chosen by what the task touches; edge-cases and test-strength always run
Each task revision and rubric is reviewed once: a report for the same revision, commit and
rubric is reused on a later convergence round or a review-only re-run.
"""
from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from office import briefs, contract, db, paths, state
from office.util import now_iso

REVIEW_VERSION = "runtime-self-review-1"
# audit_scope status: a subagent that never launched, to be retried through the bounded review fallback.
RETRY = "RETRY"
FUNDED_GEARS = ("S", "M", "L", "XL")
RUBRICS = {
    "combined": "Review edge cases and test strength in ONE pass: empty or null input, parser ambiguity, retries, restart "
                "states, weak assertions, tests that still pass with the fix reverted, missing negative paths. Review "
                "platform/build only when told the scope touches it. Security is covered by the convergence review. "
                "Try to falsify the producer's claims using the actual checkout, without changing files.",
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


def platform_relevant(files) -> bool:
    """Whether any file is a script, CI, packaging or build file (an extension matches the whole suffix)."""
    return any(p.endswith(m) if m.startswith(".") else m in p
               for p in (str(f).lower() for f in files) for m in _PLATFORM_MARKS)


def _platform_relevant(task: dict) -> bool:
    return platform_relevant(task.get("scope") or []) or (task.get("descriptor") or {}).get("domain") == "infrastructure"


# A path is sensitive when one of its directory or file-name words is one of these. Whole words only:
# `product.py` and `writer.py` are not `prod` or `write`.
_SENSITIVE_WORDS = frozenset((
    "auth", "authn", "authz", "authentication", "authorization", "oauth", "sso", "login",
    "permission", "permissions", "acl", "rbac", "secret", "secrets", "token", "tokens", "credential", "credentials",
    "password", "passwords", "crypto", "encryption", "security", "firewall"))


def security_relevant(files) -> bool:
    """Whether any changed path names a security-sensitive area, matched by path segment words."""
    for path in files:
        for segment in str(path).lower().replace("\\", "/").split("/"):
            if _SENSITIVE_WORDS & set(re.split(r"[^a-z0-9]+", segment)):
                return True
    return False


def funded(run: dict) -> bool:
    return run.get("gear") in FUNDED_GEARS and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"


def rubrics(run: dict, task: dict) -> tuple[str, ...]:
    """The independent subagent reviews this run's gear funds for one task."""
    if not funded(run):
        return ()
    if contract.is_convergence(run):
        return ("combined",)
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
                                          "WHERE d.run_id=? AND g.kind='self_review_subagent' AND g.status IN ('queued','running') "
                                          "AND d.ended_at IS NULL",
                                          (run_id,))}


def _report(task_id: str, rubric: str, rid: str, **extra) -> dict:
    return {"task": task_id, "rubric": rubric, "revision": rid, **extra}


def audit_scope(con, run: dict, tasks: list[dict], checkout: Path, commit: str, *, reviewer=None,
                retry: bool = False) -> dict:
    """Run every funded subagent review of `tasks` at `commit` (the checkout's HEAD, the commit convergence
    reviews). Returns {status, reports, reason}; any status but COMPLETED means convergence must not run.
    With `retry`, a subagent that was never launched (no reviewer route could start it) returns status RETRY
    instead of EVIDENCE_BLOCKED, for the caller to retry through the bounded review fallback. A subagent that ran
    and left a bad report, and a revoked one, stay blocked.

    The subagents of one scope share no mutable state (each owns its gate, dispatch, session and reply file, and reads
    the checkout without writing it), so those that still need a review launch together and are composed in task and
    rubric order: the result is what running them one after another gives, up to the first one that did not pass."""
    if not funded(run):
        return {"status": contract.COMPLETED, "reports": [], "reason": "no runtime self-review funded for this gear"}
    from office import gates
    reviewer = reviewer or gates.run_reviewer
    plan = _plan(con, run, tasks, commit)
    pending = [item for item in plan if item["kind"] == "review"]
    results: dict[int, dict] = {}  # by id(item): the outcomes of reviews that ran together
    failures: dict[int, dict | None] = {}  # by id(item): what recording each of them found
    if _parallel_reviews(pending):
        for item in pending:
            _prepare(con, run, item, checkout, commit)
        results = _run_together(run, pending, checkout, reviewer)
        # Every review that ran is recorded, so a sibling of one that failed leaves no gate open.
        failures = {id(i): _record(con, run, i, results[id(i)], commit, retry=retry) for i in pending}
    reports = []
    for item in plan:
        if item["kind"] == "blocked":
            return {"status": contract.EVIDENCE_BLOCKED, "reports": reports, "reason": item["reason"]}
        if item["kind"] == "reused":
            reports.append(item["report"])
            continue
        if id(item) in results:
            outcome = results[id(item)]
        else:  # one at a time: a subagent that did not pass stops the scope before the next one launches
            _prepare(con, run, item, checkout, commit)
            outcome = _review_one(con, run, item, checkout, reviewer)
        failure = failures[id(item)] if id(item) in failures else _record(con, run, item, outcome, commit, retry=retry)
        if failure:
            return {"status": RETRY if failure["retried"] else contract.EVIDENCE_BLOCKED, "reports": reports,
                    "reason": f"{item['task']['id']} {item['rubric']} self-review subagent: {failure['reason']}; "
                              "convergence review cannot proceed without it"}
        parsed = outcome["parsed"]
        reports.append(_report(item["task"]["id"], item["rubric"], item["rid"], dispatch_id=outcome["dispatch_id"],
                               advisory=parsed.verdict, reused=False,
                               findings=[{k: f.get(k) for k in ("code", "severity", "location", "summary",
                                                                "action", "blocking")} for f in parsed.findings]))
    return {"status": contract.COMPLETED, "reports": reports}


def _plan(con, run: dict, tasks: list[dict], commit: str) -> list[dict]:
    """Every subagent review of the scope, in order, with no side effect: a saved report to reuse, a review to run, or
    the task whose missing accepted revision stops the scope at that point."""
    plan = []
    for task in tasks:
        for rubric in rubrics(run, task):
            rid = task.get("accepted_revision_id")
            rev = con.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone() if rid else None
            if rev is None:
                plan.append({"kind": "blocked", "reason": f"{task['id']}: no accepted revision for the {rubric} subagent review"})
                return plan
            key = f"{rid}:{commit}:{rubric}:{REVIEW_VERSION}"
            saved = _reusable(con, run, key)
            if saved:
                plan.append({"kind": "reused", "report": _report(task["id"], rubric, rid, dispatch_id=saved["reviewer_dispatch_id"],
                                                                 advisory=saved["verdict"], summary=saved["summary"], reused=True)})
                continue
            plan.append({"kind": "review", "task": task, "rubric": rubric, "rid": rid, "rev": dict(rev), "key": key})
    return plan


def _prepare(con, run: dict, item: dict, checkout: Path, commit: str) -> None:
    """Create (or reopen) the review's gate and write its brief onto `item`."""
    from office import gates
    task, rubric, rid, rev, key = item["task"], item["rubric"], item["rid"], item["rev"], item["key"]
    # A subagent that never launched is retried on its own gate: the gate reopens, and its next attempt
    # chain starts away from the routes the last one failed on.
    unlaunched = con.execute("SELECT id FROM gates WHERE run_id=? AND kind='self_review_subagent' AND input_key=? "
                             "AND review_status=? ORDER BY created_at DESC LIMIT 1",
                             (run["id"], key, contract.UNAVAILABLE)).fetchone()
    audit_gate = {"id": unlaunched["id"] if unlaunched else "G" + uuid.uuid4().hex[:8], "task_id": task["id"],
                  "revision_id": rid, "plan_version": run.get("plan_version")}
    with db.transaction(con):
        if unlaunched:
            con.execute("UPDATE gates SET status='queued', review_status=NULL, verdict=NULL, summary=NULL, "
                        "finished_at=NULL WHERE id=?", (audit_gate["id"],))
        else:
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
    item["gate"] = audit_gate
    item["brief"] = briefs.self_review_subagent_brief(run, task, rev, commit, diff,
                                                      checks["summary"] if checks else "none declared",
                                                      str(checkout), rubric, RUBRICS[rubric] + (
                                                          " Platform/build IS in scope for this task." if rubric == "combined"
                                                          and _platform_relevant(task) else ""))


def _parallel_reviews(pending: list[dict]) -> bool:
    """Whether these reviews can launch together. Each owns its own gate, dispatch and reply file, and the in-process
    `sync` launcher supervises in the calling thread, which only the main thread can do, so it runs one at a time."""
    return len(pending) > 1 and os.environ.get("OFFICE_LAUNCHER", "auto") != "sync"


def _review_one(con, run: dict, item: dict, checkout: Path, reviewer) -> dict:
    try:
        return reviewer(con, run, item["gate"], "code_reviewer", item["brief"], cwd=checkout,
                        include_dirs=[checkout], kind="reviewer")
    except Exception as exc:
        return {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}


def _run_together(run: dict, pending: list[dict], checkout: Path, reviewer) -> dict[int, dict]:
    """The outcome of each prepared review, by `id(item)`. Each runs on a connection of its own
    (a sqlite connection stays in the thread that made it)."""
    def worker(item: dict) -> dict:
        own = db.connect()
        try:
            return _review_one(own, run, item, checkout, reviewer)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=len(pending), thread_name_prefix="self-review") as pool:
        outcomes = list(pool.map(worker, pending))
    return {id(i): o for i, o in zip(pending, outcomes)}


def _record(con, run: dict, item: dict, outcome: dict, commit: str, *, retry: bool) -> dict | None:
    """Close the subagent's gate on its outcome and say so. None when it passed, else {reason, retried}."""
    task, rubric, rid, audit_gate = item["task"], item["rubric"], item["rid"], item["gate"]
    good, reason = _valid(outcome, commit, run)
    parsed = outcome.get("parsed")
    retried = bool(retry and not good and not outcome.get("dispatch_id") and outcome.get("status") != contract.COMPLETED)
    with db.transaction(con):
        con.execute("UPDATE gates SET status='done', review_status=?, verdict=?, summary=?, route=?, "
                    "reviewer_dispatch_id=?, finished_at=? WHERE id=?",
                    (contract.COMPLETED if good else contract.UNAVAILABLE if retried else contract.EVIDENCE_BLOCKED,
                     parsed.verdict if good else None, (outcome.get("summary") or reason)[:500],
                     outcome.get("route"), outcome.get("dispatch_id"), now_iso(), audit_gate["id"]))
        state.emit(con, run, "self_review.subagent.completed" if good else "self_review.subagent.blocked",
                   f"{task['id']} {rubric}: {reason}", task_id=task["id"],
                   payload={"rubric": rubric, "revision": rid, "commit": commit,
                            "dispatch_id": outcome.get("dispatch_id"), "success": good})
    return None if good else {"reason": reason, "retried": retried}
