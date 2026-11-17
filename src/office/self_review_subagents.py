"""#809: runtime-funded, independent self-review subagents that precede convergence review.

Office itself launches these as separate reviewer sessions on a task's accepted revision. They
are advisory audits of the producer's work and its four-lens ledger, never a convergence
verdict, a substantive round or an approval. The one authoritative acceptance verdict stays the
convergence review. A missing, invalid or wrong-commit subagent report blocks that review
(fail closed) and spends no round.

New (convergence-v1) runs fund one subagent per task for every funded gear: edge cases and test strength in
one read-only pass, plus platform/build only when the task's scope touches it. Security is not a task
self-review: the single convergence reviewer covers it when a lane changes a sensitive path (#938).

#1233 (Rico, 2026-11-17: "Separate panes on XL only"): a run started after #1233 records the gears it funds
(`gates.self_review_audit_gears`, XL only). S, M and L tasks get no Office audit: the executor's in-session
subagent fan-out is their per-task check. A run without the key (started earlier) keeps the gears below.

Funding by run gear before #1233 (pinned `run-gear-1` runs; legacy-pinned runs fund nothing):
  XS      none: the executor's inline four-lens self-check is the whole of it
  S, M    one four-lens subagent per task
  L, XL   up to four specialists per task (security, edge-cases, platform, test-strength),
          chosen by what the task touches; edge-cases and test-strength always run
Each task revision and rubric is reviewed once: a report for the same revision, commit and
rubric is reused on a later convergence round or a review-only re-run.
"""
from __future__ import annotations

import json
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
# audit_scope status in the job flow: the subagents are launched and supervised, and their last settle queues the
# convergence job again to compose them.
WAITING = "WAITING"
COLLECT_KEY = "subagents_collected"  # the convergence job payload key naming the subagent gates it composes
# The gears a run started before #1233 funds (no `self_review_audit_gears` in its gates).
FUNDED_GEARS = ("S", "M", "L", "XL")
# The gears a new run funds, recorded on the run at start so a later default never changes a run in flight.
AUDIT_GEARS = ("XL",)
AUDIT_GEARS_KEY = "self_review_audit_gears"
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


def audit_gears(value) -> list[str]:
    """The configured audit gears (`review.self_review_audit_gears`), XL when unset. Each must be S, M, L or XL."""
    if value is None:
        return list(AUDIT_GEARS)
    if not isinstance(value, list) or any(g not in FUNDED_GEARS for g in value):
        raise ValueError(f"review.{AUDIT_GEARS_KEY} must be a list of gears from {list(FUNDED_GEARS)}, got {value!r}")
    return [g for g in FUNDED_GEARS if g in value]


def funded(run: dict) -> bool:
    """Whether Office launches its own per-task audits for this run's gear (the gears pinned at start, #1233)."""
    gates = run.get("gates") or {}
    gears = gates.get(AUDIT_GEARS_KEY)
    return (run.get("gear") in (FUNDED_GEARS if gears is None else gears)
            and gates.get("gear_model_version") == "run-gear-1")


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
                retry: bool = False, job: dict | None = None) -> dict:
    """Run every funded subagent review of `tasks` at `commit` (the checkout's HEAD, the commit convergence
    reviews). Returns {status, reports, reason}; any status but COMPLETED means convergence must not run.
    With `retry`, a subagent that was never launched (no reviewer route could start it) returns status RETRY
    instead of EVIDENCE_BLOCKED, for the caller to retry through the bounded review fallback. A subagent that ran
    and left a bad report, and a revoked one, stay blocked.

    The subagents of one scope share no mutable state (each owns its gate, dispatch, session and reply file, and reads
    the checkout without writing it), so those that still need a review launch together and are composed in task and
    rubric order: the result is what running them one after another gives, up to the first one that did not pass.
    With the convergence `job`, each launches through `run_reviewer` with it and is settled by its own supervise job
    (status WAITING, the checkout stays); the last one to settle queues the job again, which composes them."""
    if not funded(run):
        return {"status": contract.COMPLETED, "reports": [], "reason": "no runtime self-review funded for this gear"}
    from office import gates
    reviewer = reviewer or gates.run_reviewer
    plan = _plan(con, run, tasks, commit)
    if job is not None:
        return _audit_scope_job(con, run, plan, checkout, commit, job, retry=retry, reviewer=reviewer)
    pending = [item for item in plan if item["kind"] == "review"]
    results: dict[int, dict] = {}  # by id(item): the outcomes of reviews that ran together
    failures: dict[int, dict | None] = {}  # by id(item): what recording each of them found
    if _parallel_reviews(pending):
        prepared = []
        try:
            for item in pending:
                _prepare(con, run, item, checkout, commit)
                prepared.append(item)
        except Exception as exc:
            for item in prepared:  # a gate left queued by an item that was never launched would stay open for good
                _record(con, run, item, {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"},
                        commit, retry=retry)
            raise
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
                plan.append({"kind": "reused", "gate_id": saved["id"],
                             "report": _report(task["id"], rubric, rid, dispatch_id=saved["reviewer_dispatch_id"],
                                               advisory=saved["verdict"], summary=saved["summary"], reused=True)})
                continue
            plan.append({"kind": "review", "task": task, "rubric": rubric, "rid": rid, "rev": dict(rev), "key": key})
    return plan


def _prepare(con, run: dict, item: dict, checkout: Path, commit: str) -> None:
    """Create (or reopen) the review's gate and write its brief onto `item`."""
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
    item["gate"] = audit_gate
    _brief(con, run, item, checkout, commit)
def _brief(con, run: dict, item: dict, checkout: Path, commit: str) -> None:
    """Write the subagent's brief onto `item`."""
    from office import gates
    task, rubric, rid, rev = item["task"], item["rubric"], item["rid"], item["rev"]
    diff = gates.cap_diff(paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"]))
    checks = con.execute("SELECT summary FROM gates WHERE revision_id=? AND kind='checks' AND status='done' "
                         "ORDER BY created_at DESC LIMIT 1", (rid,)).fetchone()
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

# ------------------------------------------------------------------ the job flow
def _latest_gate(con, run_id: str, key: str):
    return con.execute("SELECT * FROM gates WHERE run_id=? AND kind='self_review_subagent' AND input_key=? "
                       "ORDER BY rowid DESC LIMIT 1", (run_id, key)).fetchone()
def _open_keys(con, run_id: str, keys: list[str]) -> list[str]:
    """The subagents among `keys` whose latest gate is still open."""
    return [k for k in keys if (g := _latest_gate(con, run_id, k)) is not None and g["status"] in ("queued", "running")]
def _audit_scope_job(con, run: dict, plan: list[dict], checkout: Path, commit: str, job: dict, *, retry: bool,
                     reviewer) -> dict:
    """`audit_scope` in the convergence job: launch every subagent that needs a review, each supervised and settled
    on its own (they run together), then return WAITING. Composed from the recorded gates once the last one settles
    (the job queued again with COLLECT_KEY), or at once when none launched."""
    from office import gates
    payload = job["payload"]
    collected = collected_gates(job)
    pending = [i for i in plan if i["kind"] == "review"]
    ran: dict[str, dict] = {}
    if collected is None and pending:
        prepared = []
        try:
            for item in pending:
                _prepare(con, run, item, checkout, commit)
                prepared.append(item)
        except Exception as exc:
            for item in prepared:  # a gate left queued by an item that was never launched would stay open for good
                _record(con, run, item, {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"},
                        commit, retry=retry)
            raise
        parent = {"payload": {k: v for k, v in payload.items() if k != gates.SETTLE_KEY}}
        base = {"scope": payload.get("scope"), "subagent": {
            "parent": parent, "commit": commit, "retry": retry, "keys": [i["key"] for i in pending],
            "gates": [i["gate"]["id"] for i in pending]}}
        launched = False
        for item in pending:
            sub = {**job, "payload": {**base, "gate_id": item["gate"]["id"], "subagent": {**base["subagent"], "key": item["key"]},
                                      **_fresh_chain(con, item["gate"]["id"])}}
            outcome = _launch(con, run, item, checkout, sub, reviewer)
            if gates.review_launched(outcome) or outcome.get("fallback_queued"):
                launched = True
            else:
                ran[item["gate"]["id"]] = outcome
                _record(con, run, item, outcome, commit, retry=retry)
        if launched:
            _queue_compose(con, run, base["subagent"])  # every sibling may have settled while the rest were launching
            return {"status": WAITING, "reports": [], "reason": "the subagent reviews are running"}
    return _compose(con, run, plan, retry=retry, ran=ran,
                    fresh=set(collected or ()) | {i["gate"]["id"] for i in pending if "gate" in i})
def collected_gates(job: dict) -> list[str] | None:
    """The subagent gates a compose job composes, or None for a job that launches them. A compose job belongs to the chain
    it was queued in: a redispatch (a later epoch) launches the subagents anew rather than composing the failed ones."""
    mark = job["payload"].get(COLLECT_KEY)
    if not mark or mark["epoch"] != (job["payload"].get("review_fallback") or {}).get("epoch", 0):
        return None
    return mark["gates"]
def supervised_gates(jobs: list[dict]) -> list[str]:
    """The subagent gates whose launched reviewers these `review_supervise` job rows wait on."""
    out = []
    for j in jobs:
        p = json.loads(j["payload_json"] or "{}") if j["kind"] == "review_supervise" else {}
        if ((p.get("origin") or {}).get("payload") or {}).get("subagent"):
            out.append(p["gate_id"])
    return out
def open_gates_at(con, run_id: str, commit: str) -> list[str]:
    """Open subagent gates reviewing the composition at `commit`, supervised or not (the key is rid:commit:rubric:version)."""
    if not commit:
        return []
    return [r[0] for r in con.execute("SELECT id FROM gates WHERE run_id=? AND kind='self_review_subagent' "
                                      "AND status IN ('queued','running') AND instr(input_key, ?) > 0",
                                      (run_id, f":{commit}:"))]
def _fresh_chain(con, gate_id: str) -> dict:
    """The job payload that starts a subagent's review on a fresh chain of routes when an earlier attempt of its gate
    failed (a retry of a subagent that never launched): the routes that attempt failed on are eligible again."""
    from office import gates
    epoch = max((a["epoch"] + 1 for a in gates.review_attempts(con, gate_id)), default=0)
    return {"review_fallback": {"epoch": epoch, "chain_epoch": epoch}} if epoch else {}
def _launch(con, run: dict, item: dict, checkout: Path, job: dict, reviewer=None) -> dict:
    from office import gates
    try:
        return (reviewer or gates.run_reviewer)(con, run, item["gate"], "code_reviewer", item["brief"], cwd=checkout,
                                                include_dirs=[checkout], kind="reviewer", job=job)
    except Exception as exc:
        return {"status": contract.UNAVAILABLE, "summary": f"{type(exc).__name__}: {exc}"}
def _compose(con, run: dict, plan: list[dict], *, retry: bool, fresh: set[str], ran: dict[str, dict] | None = None) -> dict:
    """The scope's result from the recorded gates, in task and rubric order up to the first subagent that did not
    pass. `fresh` names the gates this chain ran: their reports carry their findings, a reused one its summary. `ran`
    holds the outcomes of the ones that settled in the launching call itself."""
    from office import gates, review_parse
    reports = []
    for item in plan:
        if item["kind"] == "blocked":
            return {"status": contract.EVIDENCE_BLOCKED, "reports": reports, "reason": item["reason"]}
        reused = item["kind"] == "reused"
        if reused and item["gate_id"] not in fresh:
            reports.append(item["report"])
            continue
        gate = con.execute("SELECT * FROM gates WHERE id=?", (item["gate_id"],)).fetchone() if reused \
            else _latest_gate(con, run["id"], item["key"])
        task_id, rubric, rid = ((item["report"]["task"], item["report"]["rubric"], item["report"]["revision"]) if reused
                                else (item["task"]["id"], item["rubric"], item["rid"]))
        if gate is None or gate["review_status"] != contract.COMPLETED:
            unlaunched = gate is not None and gate["review_status"] == contract.UNAVAILABLE
            reason = gate["summary"] if gate is not None and gate["summary"] else "subagent was not launched and completed"
            return {"status": RETRY if unlaunched and retry else contract.EVIDENCE_BLOCKED, "reports": reports,
                    "reason": f"{task_id} {rubric} self-review subagent: {reason}; convergence review cannot proceed without it"}
        parsed = (ran or {}).get(gate["id"], {}).get("parsed")
        if parsed is None:
            reply = paths.run_dir(run["id"]) / "dispatches" / gate["reviewer_dispatch_id"] / "reply.txt"
            parsed = review_parse.parse(gates._last_block(reply.read_text(encoding="utf-8", errors="replace")),
                                        contract=contract.of(run))
        reports.append(_report(task_id, rubric, rid, dispatch_id=gate["reviewer_dispatch_id"], advisory=parsed.verdict,
                               reused=False,
                               findings=[{k: f.get(k) for k in ("code", "severity", "location", "summary", "action", "blocking")}
                                         for f in parsed.findings]))
    return {"status": contract.COMPLETED, "reports": reports}
def _queue_compose(con, run: dict, sa: dict) -> None:
    """Queue the convergence job again to compose the subagents once none of them is open. Many settles may find the
    scope done at once: the dedup key keeps it to one job."""
    if _open_keys(con, run["id"], sa["keys"]):
        return
    payload = {**sa["parent"]["payload"]}
    epoch = (payload.get("review_fallback") or {}).get("epoch", 0)
    payload[COLLECT_KEY] = {"gates": sa["gates"], "epoch": epoch}
    with db.transaction(con):
        state.enqueue(con, run, "convergence_review", payload, max_attempts=2,
                      dedup_key=f"self_review_compose:{payload['gate_id']}:{sa['commit']}:{epoch}")
def job_subagent_review(con, run: dict, job: dict) -> dict:
    """The convergence job entered for one subagent: its supervise job settled it, or its fallback runs the next route.
    Records its gate and, as the last of its scope to settle, queues the convergence job to compose them."""
    from office import gates
    sa = job["payload"]["subagent"]
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (job["payload"]["gate_id"],)).fetchone())
    lane_id = sa["parent"]["payload"]["gate_id"]
    lane = con.execute("SELECT status FROM gates WHERE id=?", (lane_id,)).fetchone()
    checkout = paths.run_dir(run["id"]) / "checkouts" / f"converge-{lane_id}"
    if gate["status"] not in ("queued", "running"):
        return {"skipped": gate["status"]}
    if lane is None or lane["status"] not in ("queued", "running"):  # nothing will compose it: free what it held
        gates.remove_checkout(run, checkout)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', review_status=?, summary=?, finished_at=? WHERE id=?",
                        (contract.EVIDENCE_BLOCKED, "its lane review closed before it settled", now_iso(), gate["id"]))
        return {"skipped": lane["status"] if lane else "missing"}
    task = state.get_task(con, run["id"], gate["task_id"])
    rev = con.execute("SELECT * FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone()
    rubric = sa["key"].split(":")[2]
    item = {"task": task, "rubric": rubric, "rid": gate["revision_id"], "rev": dict(rev), "key": sa["key"], "gate": gate}
    if job["payload"].get(gates.SETTLE_KEY):
        outcome = gates.settle_reviewer(con, run, gate, job)
    elif checkout.is_dir():
        _brief(con, run, item, checkout, sa["commit"])
        outcome = _launch(con, run, item, checkout, job)
    else:
        outcome = {"status": contract.UNAVAILABLE, "summary": "the checkout the subagent reads is gone"}
    if gates.review_launched(outcome):
        return {"supervise": outcome["supervise_queued"], "dispatch_id": outcome["dispatch_id"]}
    if outcome.get("fallback_queued"):
        return {"fallback": outcome["fallback_queued"]}
    _record(con, run, item, outcome, sa["commit"], retry=sa["retry"])
    _queue_compose(con, run, sa)
    return {"recorded": gate["id"]}
