"""Gate engine: revision-bound verdicts, selective invalidation, bounded
convergence, and the single acceptance evaluator.

Two review contracts (office.contract), chosen by the run's pin:

v3.1 — verdicts PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT |
UNAVAILABLE. Every task revision gets checks, an independent code review and,
when applicable, a visual review. Acceptance requires every required gate to
PASS on the task's current revision, the worker's applied version to be
current, and no open plan defect on the task's scope.

convergence-v1 (#337) — a task revision gets only its deterministic checks;
the executor's four-lens self-review is enforced by preflight. Independent
code and visual review happen once per ownership/composition lane on the
composed result (office.convergence). A checks gate records APPROVED (all
pass) or RECHECK (a failure) with review status COMPLETED; a check that cannot
run is UNAVAILABLE status with no verdict. Nothing unavailable, malformed,
stale, or missing is ever treated as approved.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from office import briefs, candidates, contract, db, jobs, paths, planfile, review_parse, routing, state, version, worktree_setup
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, pid_alive, sha256_bytes, sha256_obj

TASK_GATES = ("checks", "code_review", "visual")
MAX_DIFF_CHARS = 120_000


def cap_diff(diff: str) -> str:
    """Cap a review diff and say so, so the reviewer knows to read the checkout."""
    if len(diff) > MAX_DIFF_CHARS:
        return diff[:MAX_DIFF_CHARS] + "\n[diff truncated; inspect the checkout for the rest]"
    return diff


# ------------------------------------------------------------------ planning

def plan_for_revision(con, run: dict, task: dict, rev_id: str, changed: list[str], d: dict) -> dict:
    """Create gate rows for a new revision and queue the first step. Caller holds tx."""
    archive_ledger(run, task, rev_id, d)
    if contract.is_convergence(run):
        return _plan_for_revision_convergence(con, run, task, rev_id)
    from office import visual
    g = run.get("gates") or {}
    rows, summary = {}, []
    if task["checks"]:
        rows["checks"] = _new_gate(con, run, task, rev_id, "checks", f"checks:{rev_id}", "queued")
        summary.append("checks running")
    if g.get("code_review"):
        rows["code_review"] = _new_gate(con, run, task, rev_id, "code_review", f"code:{rev_id}",
                                        "waiting" if "checks" in rows else "queued")
        summary.append("code-review queued")
    applicability = visual.applicability(con, run, task, changed)
    if applicability["status"] in ("required", "probe"):
        key = visual.input_key(con, run, task, rev_id, applicability)
        reuse = _reusable(con, run, task, "visual", key)
        if reuse:
            gid = _new_gate(con, run, task, rev_id, "visual", key, "done", verdict="PASS", reused_from=reuse["id"],
                            evidence_status=reuse["evidence_status"])
            rows["visual"] = gid
            summary.append("visual evidence reused (no presentation change)")
        else:
            rows["visual"] = _new_gate(con, run, task, rev_id, "visual", key, "waiting" if "checks" in rows else "queued")
            summary.append("visual-review queued")
    if "checks" in rows:
        state.enqueue(con, run, "run_checks", {"gate_id": rows["checks"], "task_id": task["id"], "revision_id": rev_id},
                      dedup_key=f"checks:{rows['checks']}", max_attempts=2)
    else:
        start_waiting(con, run, task["id"], rev_id)
    if not rows:
        summary.append("no gates configured")
        # No gate will ever finish to trigger acceptance, so evaluate it now;
        # the evaluator accepts only when policy explicitly requires no gate.
        if evaluate_acceptance(con, run, task["id"]):
            summary.append("accepted (no gate required by policy)")
    return {"gates": rows, "summary": summary}


LEDGER_ARCHIVE = "self-review-ledger.md"


def ledger_archive_path(run: dict, task_id: str, rev_id: str) -> Path:
    return paths.run_dir(run["id"]) / "evidence" / task_id / rev_id / LEDGER_ARCHIVE


def archive_ledger(run: dict, task: dict, rev_id: str, d: dict) -> None:
    """Keep the executor's self-review ledger with the revision it vouched for. Submit consumes
    the worktree's copy right after this runs; a later submission of an identical tree restores it
    (restore_ledger) instead of asking the executor to review the same content again. Best effort:
    a ledger that cannot be read is simply not reused."""
    from office import submit
    try:
        text = submit._read_untracked_text(Path(d["worktree"]), briefs.LEDGER_FILE, briefs.LEDGER_MAX_CHARS)
        if not text or len(text) > briefs.LEDGER_MAX_CHARS:
            return
        dest = ledger_archive_path(run, task["id"], rev_id)
        if dest.exists():
            return  # a re-plan of the same revision (office resume) must not overwrite what vouched for it
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        os.chmod(dest, 0o600)
    except (OSError, paths.GitError):
        return


def restore_ledger(con, run: dict, task: dict, wt: Path) -> str | None:
    """Put back the self-review ledger of an already reviewed revision when the worktree holds exactly
    its tree and nothing is left to fix: the content was reviewed on the four lenses, so resubmitting it
    demands no new producer work. The ledger names HEAD, so its COMMIT line is rewritten to the current
    HEAD. Never replaces a ledger the executor wrote. Returns the revision whose ledger came back, or None."""
    target = wt / briefs.LEDGER_FILE
    if os.path.lexists(target):
        return None
    if con.execute("SELECT 1 FROM findings WHERE run_id=? AND task_id=? AND " + contract.TASK_WORK_FINDINGS,
                   (run["id"], task["id"])).fetchone():
        return None  # a fix round changes the tree: the earlier ledger would only be stale at submit
    try:
        if paths.git(wt, "status", "--porcelain", "--untracked-files=no").strip():
            return None
        head, tree = paths.git(wt, "rev-parse", "HEAD"), paths.git(wt, "rev-parse", "HEAD^{tree}")
    except paths.GitError:
        return None
    for r in con.execute("SELECT id FROM revisions WHERE run_id=? AND task_id=? AND tree_sha=? ORDER BY seq DESC",
                         (run["id"], task["id"], tree)).fetchall():
        try:
            saved = ledger_archive_path(run, task["id"], r["id"])
            if not saved.is_file():
                continue
            text = re.sub(r"(?m)^COMMIT\s+\S+\s*$", f"COMMIT {head}", saved.read_text(encoding="utf-8"), count=1)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except (OSError, ValueError):
            return None
        return r["id"]
    return None


def _plan_for_revision_convergence(con, run: dict, task: dict, rev_id: str) -> dict:
    """#337: a task revision gets its deterministic checks only. Independent code
    and visual review run once per lane on the composed result."""
    rows, summary = {}, []
    if task["checks"]:
        rows["checks"] = _new_gate(con, run, task, rev_id, "checks", f"checks:{rev_id}", "queued")
        state.enqueue(con, run, "run_checks", {"gate_id": rows["checks"], "task_id": task["id"], "revision_id": rev_id},
                      dedup_key=f"checks:{rows['checks']}", max_attempts=2)
        summary.append("checks running; lane convergence review follows")
    else:
        summary.append("no checks declared; lane convergence review follows")
        if evaluate_acceptance(con, run, task["id"]):
            summary.append("task verified (lane convergence pending)")
    return {"gates": rows, "summary": summary}


def _new_gate(con, run, task, rev_id, kind, key, status, *, verdict=None, reused_from=None, evidence_status=None,
              escalated=0, round_no=None) -> str:
    gid = "G" + uuid.uuid4().hex[:8]
    if round_no is None:
        round_no = current_round(con, run["id"], task["id"], kind)
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, input_key, status, verdict, "
                "evidence_status, round, escalated, reused_from, created_at, finished_at, contract) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, run["id"], "task", task["id"], rev_id, run["plan_version"], kind, key, status, verdict, evidence_status,
                 round_no, escalated, reused_from, now_iso(), now_iso() if status == "done" else None, contract.of(run)))
    return gid


def _reusable(con, run, task, kind, key):
    row = con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND kind=? AND input_key=? AND verdict='PASS' "
                      "AND status IN ('done','stale') ORDER BY created_at DESC LIMIT 1", (run["id"], task["id"], kind, key)).fetchone()
    return dict(row) if row else None


def current_round(con, run_id: str, task_id: str, kind: str) -> int:
    rows = con.execute("SELECT DISTINCT revision_id FROM gates WHERE run_id=? AND task_id=? AND kind=? AND "
                       "verdict IN ('CHANGES_REQUIRED','RECHECK') AND escalated=0", (run_id, task_id, kind)).fetchall()
    return len(rows) + 1


def start_waiting(con, run: dict, task_id: str, rev_id: str) -> list[str]:
    """After deterministic checks pass, dispatch code and visual review together
    on the same fixed revision (Q6)."""
    started = []
    for g in con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND revision_id=? AND status IN ('waiting','queued') "
                         "AND kind IN ('code_review','visual')", (run["id"], task_id, rev_id)).fetchall():
        g = dict(g)
        con.execute("UPDATE gates SET status='queued' WHERE id=?", (g["id"],))
        if g["kind"] == "code_review":
            state.enqueue(con, run, "review", {"gate_id": g["id"], "task_id": task_id}, dedup_key=f"review:{g['id']}",
                          max_attempts=2)
        else:
            state.enqueue(con, run, "visual_capture", {"gate_id": g["id"], "task_id": task_id},
                          dedup_key=f"capture:{g['id']}", max_attempts=2)
        started.append(g["kind"])
    return started


REVIEW_STOPPED = ("UNAVAILABLE", "ATTENTION")


def unavailable_review_block(con, run: dict, task: dict) -> dict | None:
    """The code-review gate that alone blocks the task's current (submitted)
    revision because its reviewer could not finish (UNAVAILABLE: no route could
    answer; ATTENTION: the last wrote no readable reply), or None when the task
    is blocked for another reason."""
    rev_id = task.get("current_revision_id")
    if task["status"] != "blocked" or not rev_id:
        return None
    latest = {g["kind"]: g for g in required_gates(con, run, task, rev_id)}
    code = latest.get("code_review")
    if not code or code["status"] != "done" or code["verdict"] not in REVIEW_STOPPED:
        return None
    if any(g["status"] == "done" and g["verdict"] != "PASS" for k, g in latest.items() if k != "code_review"):
        return None  # another gate failed too; a re-review alone would not unblock it
    return code


def rerun_unavailable_review(con, run: dict, task: dict) -> str | None:
    """Queue a fresh code review of the task's current revision when that
    revision is blocked only because its reviewer could not finish. The
    submission is kept and no executor is launched. Returns the new gate id, or
    None when that is not the block. Caller holds the tx."""
    code = unavailable_review_block(con, run, task)
    if code is None:
        return None
    rev_id = code["revision_id"]
    gid = _new_gate(con, run, task, rev_id, "code_review", f"{code['input_key']}:rerun:{uuid.uuid4().hex[:6]}",
                    "queued", round_no=code["round"], escalated=code["escalated"])
    state.enqueue(con, run, "review", {"gate_id": gid, "task_id": task["id"]}, dedup_key=f"review:{gid}", max_attempts=2)
    state.update_task(con, run["id"], task["id"], status="submitted", pause_reason=None)
    state.emit(con, run, "gate.rerun", f"{task['id']} code review re-run on {rev_id} (was {code['verdict']})",
               task_id=task["id"])
    return gid


def fallback_options(head: str, target: str, nxt: str | None, *, subject: str, report_cmd: str,
                     produced: str | None) -> str:
    """The two options a `next:` line gives for a review whose reviewer could not finish, never a
    waiver: (1) the next fallback reviewer, `office rerun <target> --review --review-as <nxt>`;
    (2) the orchestrator's own review, recorded as a non-independent fallback for `subject`, unless
    `produced` says the orchestrator may not review this work."""
    first = (f"(1) the next fallback reviewer for this revision: office rerun {target} --review --review-as {nxt}" if nxt
             else "(1) the next fallback reviewer: none qualifies now, and office resume retries the chain when one does")
    second = (f"(2) your own review is not allowed: {produced}" if produced else
              f"(2) review it yourself, recorded as a non-independent fallback by {actor_identity()} on route "
              f"orchestrator for {subject}: {report_cmd}")
    return f"{head} Two options: {first}; {second}"


def task_review_next(con, run: dict, task: dict, block: dict) -> str:
    """The next: line for a v3.1 task whose code reviewer could not finish: the
    two recorded options, never a waiver (see convergence.fallback_next)."""
    tid, rev = task["id"], task["current_revision_id"]
    on_rev = [g["id"] for g in con.execute("SELECT id FROM gates WHERE run_id=? AND task_id=? AND revision_id=? AND "
                                           "kind='code_review'", (run["id"], tid, rev)).fetchall()]
    nxt = next_reviewer_route(con, run, "code_reviewer", tid, reviewer_tried(con, run["id"], on_rev))
    head = (f"{tid}: no code reviewer returned a verdict for {rev}"
            f"{' (the reviewer hit a usage limit)' if 'quota stall' in (block.get('summary') or '') else ''} "
            "(runtime status, not a verdict).")
    return fallback_options(head, tid, nxt, subject=f"revision {rev}", report_cmd=f"office review {tid} --report <file>",
                            produced=orchestrator_produced(con, run, [tid]))


def fallback_review_task(con, run: dict, tid: str, report: Path) -> Result:
    """The orchestrator's code review of a v3.1 task whose reviewer could not
    finish, recorded as degraded and non-independent: it satisfies the gate only
    as that recorded fallback, and never for work the orchestrator produced."""
    require_orchestrator(con, run, "review in a reviewer's place")
    if not report.is_file():
        raise Usage("no-report", f"no review file at {report}")
    parsed = review_parse.parse(_last_block(report.read_text(encoding="utf-8", errors="replace")))
    if not parsed.valid or parsed.verdict not in ("PASS", "CHANGES_REQUIRED"):
        raise Refused("report-invalid", "the report is not a valid review: "
                      + "; ".join(parsed.errors[:3] or [f"verdict {parsed.verdict} is not PASS or CHANGES_REQUIRED"]),
                      next_step="rewrite it in the review format, then retry")
    who = actor_identity()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        task = state.get_task(con, run["id"], tid)
        block = unavailable_review_block(con, run, task) if task else None
        if block is None:
            raise Refused("fallback-not-allowed", f"{tid}: the orchestrator's review stands in only for a code reviewer "
                          "that could not finish (silent, or stopped on a usage limit); this task is not blocked on one",
                          scope=tid, next_step="office status")
        produced = orchestrator_produced(con, run, [tid])
        if produced:
            raise Refused("orchestrator-produced-work", f"{tid}: the orchestrator may not review work it produced: "
                          f"{produced}", scope=tid, next_step=f"another reviewer: office rerun {tid} --review "
                                                              "--review-as <route>")
        rev_id = block["revision_id"]
        gid = _new_gate(con, run, task, rev_id, "code_review", f"{block['input_key']}:orchestrator:{uuid.uuid4().hex[:6]}",
                        "running", round_no=block["round"], escalated=block["escalated"])
        con.execute("UPDATE gates SET independence=?, contract=? WHERE id=?", (contract.DEGRADED, contract.of(run), gid))
        state.record_evidence(con, run["id"], "review_output", report, task_id=tid, revision_id=rev_id, gate_id=gid,
                              meta={"route": "orchestrator", "who": who, "revision": rev_id,
                                    "independence": contract.DEGRADED, "replaces_gate": block["id"]})
        state.update_task(con, run["id"], tid, status="submitted", pause_reason=None)
        ingest_task_gate(con, run, gid, {
            "verdict": parsed.verdict, "parsed": parsed, "route": f"orchestrator ({who}) (degraded fallback)",
            "summary": f"{parsed.verdict} by the orchestrator ({who}) on route orchestrator for revision {rev_id}: "
                       f"degraded, non-independent fallback after the reviewer could not finish ({block['verdict']})"})
        state.emit(con, run, "review.degraded_fallback", f"{tid} code review by the orchestrator ({who}, route "
                   f"orchestrator, revision {rev_id}) as the degraded, non-independent fallback: {parsed.verdict}",
                   task_id=tid, payload={"who": who, "route": "orchestrator", "revision": rev_id,
                                         "replaces_gate": block["id"], "verdict": parsed.verdict})
        status = state.get_task(con, run["id"], tid)["status"]
    jobs.kick(con, run["id"])
    return Result(lines=[f"{tid} code review {parsed.verdict} recorded as a degraded, non-independent orchestrator "
                         f"review by {who} for {rev_id} | {tid} {status}"], next="exceptions only; office status")


def rerun_unavailable_checks(con, run: dict, task: dict) -> bool:
    """`office resume`: re-plan the gates of the task's current revision when
    its checks were UNAVAILABLE (a timeout under host load, say). The
    submission is kept and no executor is launched. Caller holds the tx."""
    if task["status"] != "blocked" or not (task.get("pause_reason") or "").startswith("checks gate unavailable"):
        return False
    rev = con.execute("SELECT * FROM revisions WHERE id=?", (task["current_revision_id"],)).fetchone()
    if rev is None:
        return False
    rev = dict(rev)
    changed = json.loads(rev["changed_json"] or "[]")
    state.update_task(con, run["id"], task["id"], status="submitted", pause_reason=None)
    plan_for_revision(con, run, state.get_task(con, run["id"], task["id"]), rev["id"], changed,
                      state.get_dispatch(con, rev["dispatch_id"]))
    state.emit(con, run, "gate.rerun", f"{task['id']} checks re-run on {rev['id']} (were UNAVAILABLE)", task_id=task["id"])
    return True


def stale_open_gates(con, run: dict, task_id: str, new_rev: str) -> None:
    """A new revision supersedes the old one: unfinished gates on it never run
    or, if already running, are ingested as stale audit evidence."""
    con.execute("UPDATE gates SET status='cancelled', stale_reason=? WHERE run_id=? AND task_id=? AND revision_id!=? "
                "AND status IN ('waiting','queued')", (f"superseded by {new_rev}", run["id"], task_id, new_rev))
    con.execute("UPDATE outbox SET status='cancelled', finished_at=? WHERE run_id=? AND status='queued' AND "
                "json_extract(payload_json,'$.task_id')=? AND kind IN ('run_checks','review','visual_capture','visual_review')",
                (now_iso(), run["id"], task_id))


def mark_unavailable(con, run: dict, gate_id: str, reason: str) -> None:
    g = con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
    if g is None or g["status"] == "done":
        return
    con.execute("UPDATE gates SET status='done', verdict='UNAVAILABLE', summary=?, finished_at=? WHERE id=?",
                (reason[:500], now_iso(), gate_id))
    if g["task_id"] and g["subject"] == "task":
        state.emit(con, run, "gate.unavailable", f"{g['task_id']} {g['kind']} UNAVAILABLE: {reason[:140]}; "
                   "valid unrelated results are preserved", task_id=g["task_id"])
    elif g["subject"] == "integration":
        state.emit(con, run, "gate.unavailable", f"integration {g['kind']} UNAVAILABLE: {reason[:140]}")


# ------------------------------------------------------------------ deterministic checks

def job_run_checks(con, run: dict, job: dict) -> dict:
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (job["payload"]["gate_id"],)).fetchone())
    if gate["status"] not in ("queued", "running"):
        return {"skipped": gate["status"]}
    task = state.get_task(con, run["id"], gate["task_id"])
    rev = dict(con.execute("SELECT * FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone())
    d = state.get_dispatch(con, rev["dispatch_id"])
    with db.transaction(con):
        con.execute("UPDATE gates SET status='running', started_at=? WHERE id=?", (now_iso(), gate["id"]))
    outcome = run_commands(con, run, task["checks"], Path(d["worktree"]), rev, gate)
    with db.transaction(con):
        if outcome.get("preexisting"):
            state.emit(con, run, "gate.preexisting", f"{task['id']} checks on {rev['id']}: "
                       f"{', '.join(p['command'] for p in outcome['preexisting'])} also fail on the base "
                       f"{rev['base_commit'][:10]} (pre-existing, not a producer failure); independent review still runs",
                       task_id=task["id"], payload={"preexisting": outcome["preexisting"]})
        ingest_task_gate(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome["verdict"]}


def run_commands(con, run: dict, commands: list[str], cwd: Path, rev: dict, gate: dict, *, check_tree: bool = True) -> dict:
    cfgv = state.pinned_config(run).get("verification") or {}
    with check_slot(check_concurrency(cfgv)):
        return _run_commands(con, run, commands, cwd, rev, gate, cfgv, check_tree=check_tree)


def check_concurrency(cfgv: dict) -> int:
    """How many check suites may run at once across every run on this host.
    N parallel full test suites push load past the CPU count and turn per-test
    timeouts into flakes (issue #268); 0 or unset means max(1, CPUs // 4)."""
    n = int(cfgv.get("check_concurrency") or 0)
    return n if n > 0 else max(1, (os.cpu_count() or 1) // 4)


@contextlib.contextmanager
def check_slot(limit: int, *, poll: float = 2.0):
    """Hold one of `limit` host-wide check slots (flock'd files under the state
    home, released by the kernel if this process dies). Waits for a free one."""
    slots = paths.state_home() / "check-slots"
    slots.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(limit):
            fh = open(slots / f"slot-{i}.lock", "a")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                continue
            try:
                yield i
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            return
        time.sleep(poll)


def _run_commands(con, run: dict, commands: list[str], cwd: Path, rev: dict, gate: dict, cfgv: dict, *,
                  check_tree: bool = True) -> dict:
    from office.submit import matches_revision
    timeout = int(cfgv.get("check_timeout_seconds", 1800))
    evdir = paths.run_dir(run["id"]) / "evidence" / (gate.get("task_id") or "integration") / rev["id"]
    evdir.mkdir(parents=True, exist_ok=True)
    findings, results = [], []
    for i, command in enumerate(commands, start=1):
        if check_tree and not matches_revision(cwd, rev["commit_sha"], paths.run_dir(run["id"]) / "tmp"):
            return {"verdict": "STALE", "summary": "worktree changed after submit; checks cannot bind to the revision"}
        log = evdir / f"check-{i}.log"
        started = time.time()
        try:
            proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                                  env=_check_env(run))
            code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            code, out = 124, f"timed out after {timeout}s\n{exc.stdout or ''}"
        # Office's own timeout (124) or a test runner's per-test timeout (exit 1)
        # under host load is the environment, not the code (issue #267 A2).
        loaded = host_overloaded() if code == 124 or (code != 0 and runner_timeout(out)) else None
        if loaded:
            out = f"{loaded}\n{out}"
        log.write_text(out, encoding="utf-8")
        os.chmod(log, 0o600)
        passed = code == 0
        results.append({"command": command, "exit": code, "seconds": round(time.time() - started, 2), "log": str(log)})
        with db.transaction(con):
            state.record_evidence(con, run["id"], "check_output", log, task_id=gate.get("task_id"),
                                  revision_id=rev["id"], gate_id=gate["id"], meta={"command": command, "exit": code})
            con.execute("INSERT INTO validations(id, dispatch_id, kind, command, passed, known_bad_proven, evidence_hash, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, rev["dispatch_id"], "check", command, int(passed), 0,
                                                    sha256_bytes(out.encode()), now_iso()))
        if loaded:
            what = f"timed out after {timeout}s" if code == 124 else "hit a test-runner timeout"
            return {"verdict": "UNAVAILABLE", "summary": f"`{command}` {what}; {loaded}; "
                    "rerun when the host is quieter: office resume", "results": results}
        if code == 127 or ("command not found" in out[-400:] and code != 0):
            return {"verdict": "UNAVAILABLE", "summary": f"check command not found: {command}", "results": results}
        if not passed:
            tail = "\n".join(out.strip().splitlines()[-12:])
            findings.append({"code": f"C{i}", "severity": "material", "location": command, "_output": out, "_exit": code,
                             "summary": f"check failed (exit {code}): {tail[-600:]}", "action": "make this check pass"})
        if check_tree and not matches_revision(cwd, rev["commit_sha"], paths.run_dir(run["id"]) / "tmp"):
            return {"verdict": "STALE", "summary": "worktree changed while checks ran", "results": results}
    preexisting = []
    if findings and gate.get("task_id") and rev.get("base_commit"):
        findings, preexisting = _split_preexisting(con, run, gate, rev, findings, timeout, evdir)
    for f in findings:
        f.pop("_output", None)
        f.pop("_exit", None)
    verdict = "PASS" if not findings else "CHANGES_REQUIRED"
    parsed = review_parse.Parsed(verdict=verdict, findings=findings)
    summary = f"{len(commands) - len(findings) - len(preexisting)}/{len(commands)} checks passed"
    if preexisting:
        summary += (f"; {len(preexisting)} failed on the base revision {rev['base_commit'][:10]} too (pre-existing, not a "
                    "producer failure): " + "; ".join(p["command"] for p in preexisting))[:500]
    return {"verdict": verdict, "parsed": parsed, "results": results, "route": "deterministic", "summary": summary,
            "preexisting": preexisting}


# What a test runner prints for each failing test, one id per line. `[ \t]*`, never `\s*`: with re.M
# the latter rescans a long run of blank lines from every line start.
_FAILED_ID = re.compile(r"^[ \t]*(?:FAILED|FAIL|ERROR|not ok[ \t]+\d+(?:[ \t]+-)?|[✗×✖])[:]?[ \t]+"
                        r"([^\s(][^\n]*?)(?:[ \t]+-[ \t][^\n]*)?$", re.M)
_FAILED_COUNT = re.compile(r"(?<!\d)(\d{1,9})[ \t]+(?:failed|failures?|errors?)\b", re.I)  # anchored: one scan per digit run
_SCAN_CHARS = 200_000


def failure_ids(out: str) -> set[str]:
    """The failing test ids a runner's output names, or an empty set when it names none."""
    return {m.group(1).strip() for m in _FAILED_ID.finditer((out or "")[-_SCAN_CHARS:])}


def failure_count(out: str) -> int | None:
    """How many failures a runner's summary reports, or None when it reports no count."""
    counts = [int(n) for n in _FAILED_COUNT.findall((out or "")[-_SCAN_CHARS:])]
    return max(counts) if counts else None


_PATH = re.compile(r"(?:/[\w.@+~-]+)+/([\w.@+~-]+)")
_VOLATILE = re.compile(r"0x[0-9a-f]+|\b\d+(?:\.\d+)?[ \t]*(?:ms|s|sec|secs|seconds|us|µs)\b|\bpid[ =]\d+", re.I)


def _signature(out: str) -> list[str]:
    """The end of a failing check's output with what differs between two checkouts removed:
    directories (a worktree and a base checkout live in different ones), timings, addresses and
    pids. Counts stay: a different number of failures is a different failure."""
    lines = [ln.strip() for ln in (out or "")[-_SCAN_CHARS:].splitlines() if ln.strip()][-6:]
    return [_VOLATILE.sub("#", _PATH.sub(r"\1", ln)) for ln in lines]


def same_failure(head_out: str, base_out: str) -> bool:
    """Does a check that fails on the head fail on the base for the same reason? When the head's
    output names failing tests, every one must also fail on the base and the head may not report
    more failures than the base (a new failing test in an already failing file is the producer's).
    When it names none, the ends of both outputs must match: a different error (a feature missing
    on the base, wrongly built on the head) or a different count is not the base's failure."""
    head_ids = failure_ids(head_out)
    if head_ids:
        head_n, base_n = failure_count(head_out), failure_count(base_out)
        return head_ids <= failure_ids(base_out) and (head_n is None or base_n is None or head_n <= base_n)
    return _signature(head_out) == _signature(base_out)


def _names_a_file(out: str, changed: list[str]) -> bool:
    """Does a failing check's output name a file this revision changed? Then the failure is where the
    task worked, not an inherited one, whatever the base printed."""
    tail = (out or "")[-_SCAN_CHARS:]
    return any(name and name in tail for f in changed for name in {f, f.rsplit("/", 1)[-1]} if len(name) >= 3)


def _split_preexisting(con, run: dict, gate: dict, rev: dict, findings: list[dict], timeout: int,
                       evdir: Path) -> tuple[list[dict], list[dict]]:
    """Run each failing check on the task's base revision (#306). A check that fails there with the
    same exit status and failures is pre-existing: the task did not break it, so it is recorded as
    such and neither cancels independent review nor counts as a producer failure. Returns (producer
    failures, pre-existing). A base that cannot be checked out or a command that cannot run there
    leaves the failure the producer's."""
    # What the task changed since its base (a revision's own delta is only since the previous revision).
    changed = [f for f in paths.git(Path(run["repo_root"]), "-c", "core.quotepath=off", "diff", "--name-only", "-z",
                                    "--no-renames", rev["base_commit"], rev["commit_sha"], check=False).split("\0") if f]
    if not changed:
        return findings, []  # nothing was changed, so nothing was inherited: an empty submission fails as the base does
    name = f"base-{gate['id']}"
    try:
        checkout = detached_checkout(run, rev["base_commit"], name, purpose="check")
    except (paths.GitError, OSError):
        remove_checkout(run, paths.run_dir(run["id"]) / "checkouts" / name)  # a half-made checkout leaks nothing
        return findings, []
    mine, pre = [], []
    try:
        for f in findings:
            command = f["location"]
            try:
                proc = subprocess.run(command, shell=True, cwd=str(checkout), capture_output=True, text=True,
                                      timeout=timeout, env=_check_env(run))
                code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                mine.append(f)
                continue
            if (code in (0, 124, 127) or code != f["_exit"] or "command not found" in out[-400:]
                    or runner_timeout(f["_output"]) or _names_a_file(f["_output"], changed)
                    or not same_failure(f["_output"], out)):
                mine.append(f)
                continue
            # Kept as evidence: what the base printed is what made this not the producer's.
            log = evdir / f"check-{f['code'][1:]}-base.log"
            log.write_text(out, encoding="utf-8")
            os.chmod(log, 0o600)
            with db.transaction(con):
                state.record_evidence(con, run["id"], "check_output_base", log, task_id=gate.get("task_id"),
                                      revision_id=rev["id"], gate_id=gate["id"],
                                      meta={"command": command, "exit": code, "base_commit": rev["base_commit"]})
            pre.append({"code": f["code"], "command": command, "exit": code, "base_commit": rev["base_commit"],
                        "base_log": str(log)})
    finally:
        remove_checkout(run, checkout)
    return mine, pre


def host_overloaded() -> str | None:
    """A timeout under heavy host load (parallel suites) is the environment,
    not the code: the same tree that timed out at 1800s with load 100+ passed
    in 223s alone. Says why when the 1-minute load is over twice the CPU count."""
    try:
        load = os.getloadavg()[0]
    except OSError:
        return None
    cpus = os.cpu_count() or 1
    limit = float(os.environ.get("OFFICE_CHECK_LOAD_FACTOR", "2")) * cpus
    return f"host load {load:.0f} exceeds {limit:.0f} ({cpus} CPUs)" if load > limit else None


_RUNNER_TIMEOUT = re.compile(
    r"Test timed out in \d+\s*ms"                 # vitest
    r"|Exceeded timeout of \d+\s*ms"              # jest
    r"|Timeout of \d+\s*ms exceeded"              # mocha
    r"|Test timeout of \d+\s*ms exceeded"         # playwright
    r"|\+{3,} Timeout \+{3,}|Failed: Timeout >",  # pytest-timeout
    re.I)


def runner_timeout(out: str) -> bool:
    """A test runner's own per-test timeout fired. Exits nonzero like any
    failure, so on its own it proves nothing; under host load it is the
    environment. The rerun on `office resume` still catches a real failure."""
    return bool(_RUNNER_TIMEOUT.search(out))


def _check_env(run: dict) -> dict:
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("OFFICE_") and k not in ("OFFICE_DATA_HOME", "OFFICE_STATE_HOME"):
            env.pop(k)
    # Checks judge the submitted source, never a cached compile of an older one.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ------------------------------------------------------------------ reviewers

def run_reviewer(con, run: dict, gate: dict, role: str, brief: str, *, cwd: Path, plan_review: bool = False,
                 visual: bool = False, images: list[Path] | None = None, include_dirs: list[Path] | None = None,
                 exclude: list[str] | None = None, kind: str | None = None, resume_from: str | None = None,
                 review_override: dict | None = None) -> dict:
    """Route, launch, and parse one independent review, substituting routes on
    environment/adapter/schema failure up to the configured bound.

    `resume_from` names an earlier reviewer dispatch to continue (a plan-defect
    redirect with --reviewer same): its session is resumed when the harness can,
    else a fresh session runs on the same route."""
    convergence = contract.is_convergence(run)
    # v3.1 bounds route substitution; the convergence contract walks the whole
    # eligible fallback chain before reporting the reviewer unavailable (#337),
    # and none of those attempts spends a substantive round.
    limit = int((run.get("gates") or {}).get("environment_retry_max", 2)) if not convergence else 12
    excluded = set(exclude or [])
    rc = contract.of(run)
    producer = _producer_route(con, gate)
    failures = []
    pinned_stall = None
    task = state.get_task(con, run["id"], gate["task_id"]) if gate.get("task_id") else None
    # A user-pinned code reviewer (dispatch --review-as) replaces routing.
    # Independence is per agent: every reviewer is a fresh dispatch and session,
    # never the producer's, so the producer's model or family is not excluded.
    pinned = review_override or ((task or {}).get("review_override") if role == "code_reviewer" else None)
    profile_kind = kind or ("vision" if visual else "reviewer")
    resume, same_route = (_reviewer_resume(con, run, resume_from, profile_kind, cwd) if resume_from
                          else (None, None))
    for attempt in range(limit + 1):
        if attempt == 0 and same_route and not pinned:
            decision = candidates.route_role(con, state.pinned_config(run), run, role, task_id=gate.get("task_id"),
                                             exact=same_route)
            if decision.get("status") != "selected":
                resume = None
                decision = candidates.route_role(con, state.pinned_config(run), run, role,
                                                 task_id=gate.get("task_id"), exclude=excluded)
        elif pinned:
            decision = candidates.declared_decision(pinned["as"], flag="--review-as")
            decision["launch"] = {k: pinned[k] for k in ("cli", "external") if pinned.get(k)}
            if resume and routing.candidate_id(decision["candidate"]) != same_route:
                resume = None  # the earlier session was another route's: it cannot be resumed as the pinned one
        else:
            decision = candidates.route_role(con, state.pinned_config(run), run, role, task_id=gate.get("task_id"),
                                             exclude=excluded)
        if decision.get("status") != "selected":
            failures.append(f"no qualifying {role} route ({decision.get('status')})"
                            + (f"; {candidates.protected_quota_remedy(run, role, gate.get('task_id'))}"
                               if decision.get("status") == "protected_quota_would_be_consumed" else ""))
            break
        cand = decision["candidate"]
        triple = routing.candidate_id(cand)
        with db.transaction(con):
            dispatch_id = _reviewer_dispatch(con, run, gate, role, decision)
            con.execute("UPDATE gates SET status='running', route=?, route_json=?, started_at=COALESCE(started_at, ?) "
                        "WHERE id=?", (triple, dumps(decision.get("selection_disclosure")), now_iso(), gate["id"]))
        ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch_id
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / "brief.md").write_text(brief, encoding="utf-8")
        output = ddir / "reply.txt"
        from office import dispatch as dispatch_mod
        d = state.get_dispatch(con, dispatch_id)
        launch_form = decision.get("launch") or {}
        if resume and attempt == 0:
            with db.transaction(con):
                con.execute("UPDATE dispatches SET resumed_from=? WHERE id=?", (resume["parent"], dispatch_id))
        dispatch_mod.launch(run, d, profile_kind, ddir, cwd=cwd, wait=True, output=output, images=images,
                            include_dirs=include_dirs, cli=launch_form.get("cli"),
                            external=bool(launch_form.get("external")),
                            resume=resume if attempt == 0 else None)
        d = state.get_dispatch(con, dispatch_id)
        text = _reply_text(d, ddir, output)
        parsed = review_parse.parse(_last_block(text), plan_review=plan_review, visual=visual, contract=rc)
        wrote_file = _wrote_reply(output)
        # Wall signatures come from the harness log, never from the review
        # itself (a review of quota code must not read as a quota wall).
        log_text = "" if wrote_file else _log_text(d, ddir)
        launch_failed = (d.get("terminal_classification") != "success" and not wrote_file) \
            or _quota_signature(log_text) or _auth_signature(log_text)
        if not parsed.valid and not launch_failed:
            # The reviewer answered (or ended cleanly) but its reply file is
            # missing or unparseable. Its work is not discarded and no other
            # reviewer is substituted: the same session is asked to rewrite the
            # file with the exact errors (R11). Results come only from the file
            # (R13), never from pane or transcript text.
            text, parsed, attention = _reprompt_until_valid(con, run, d, ddir, output, parsed,
                                                            plan_review=plan_review, visual=visual)
            never_replied = not _wrote_reply(output)
            if attention and never_replied and HELD_PROMPT not in attention:
                # Every re-prompt came back empty: the reviewer never worked (a
                # quota wall the harness did not report, a brief that never
                # landed). A wall, or any silence under the convergence contract,
                # is a launch failure: the next route runs instead of the gate
                # ending INVALID_RESULT on this one (#384).
                (ddir / "pane-tail.txt").unlink(missing_ok=True)  # re-read after the re-prompts
                d = state.get_dispatch(con, dispatch_id)
                wall = reviewer_wall(_log_text(d, ddir))
                if wall or (convergence and not pinned):
                    failures.append(_failure_reason(d, triple, wall, unusable=parsed.errors))
                    _record_env_failure(con, gate, dispatch_id, wall)
                    if pinned:
                        pinned_stall = (triple, wall)
                        break
                    # A harness that silently answers nothing is walled for every
                    # model on it, so the chain moves to another harness.
                    excluded.update({triple, f"harness:{cand['harness']}"})
                    continue
            if attention:
                with db.transaction(con):
                    state.record_evidence(con, run["id"], "review_output", _reply_file(output),
                                          task_id=gate.get("task_id"), revision_id=gate.get("revision_id"),
                                          gate_id=gate["id"], meta={"route": triple, "exit": d.get("exit_code")},
                                          digest=sha256_bytes(text.encode()))
                if convergence:
                    return {"status": contract.INVALID_RESULT, "verdict": None, "parsed": None, "route": triple,
                            "dispatch_id": dispatch_id, "summary": attention, "producer_route": producer}
                return {"verdict": "ATTENTION", "parsed": None, "route": triple, "dispatch_id": dispatch_id,
                        "summary": attention, "producer_route": producer}
            d = state.get_dispatch(con, dispatch_id)
        with db.transaction(con):
            state.record_evidence(con, run["id"], "review_output", _reply_file(output),
                                  task_id=gate.get("task_id"), revision_id=gate.get("revision_id"), gate_id=gate["id"],
                                  meta={"route": triple, "exit": d.get("exit_code")},
                                  digest=sha256_bytes(text.encode()))
        if parsed.valid:
            # A valid reply file is the result, whatever the exit classification.
            # The reviewer is done: snapshot and close its pane (R1).
            dispatch_mod.reclaim_pane(run, dispatch_id)
            out = {"verdict": parsed.verdict, "parsed": parsed, "route": triple, "dispatch_id": dispatch_id,
                   "summary": f"{parsed.verdict or parsed.evidence_status} by {triple}", "producer_route": producer}
            if convergence:
                out["status"] = contract.COMPLETED if parsed.verdict else contract.EVIDENCE_BLOCKED
                out["env_failures"] = len(failures)
            return out
        # Only a launch failure reaches here: the agent never produced a reply
        # (never started, died first, or hit an auth or quota wall). That is the
        # one case where another route is substituted.
        wall = reviewer_wall(_log_text(d, ddir))
        failures.append(_failure_reason(d, triple, wall))
        _record_env_failure(con, gate, dispatch_id, wall)
        if pinned:
            pinned_stall = (triple, wall)
            break  # the user named this reviewer; never substitute another
        excluded.add(triple)
        if wall:
            excluded.add(f"harness:{cand['harness']}")
    if pinned_stall:
        failures.insert(0, _pinned_stall_hint(con, run, gate, role, pinned_stall, excluded))  # first: summaries are cut
    if convergence:
        return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "route": None,
                "summary": "every eligible reviewer route failed: " + "; ".join(failures)[:560],
                "env_failures": len(failures), "exhausted": True}
    return {"verdict": "UNAVAILABLE", "parsed": None, "route": None, "summary": "; ".join(failures)[:600]}


HELD_PROMPT = "typed but unsubmitted"  # a re-prompt the reviewer never received: its agent is waiting, not silent
REPLY_FILE_RULE = ("Office reads your review only from that file; text you print in the terminal is not read.")


def _reply_file(output: Path) -> Path | None:
    """The file that holds the reviewer's reply: its reply file, else the final-message file a headless
    harness writes. What the review's evidence points at, so its findings can be read back from it."""
    for p in (output, output.with_name("last-message.txt")):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _wrote_reply(output: Path) -> bool:
    """Did the reviewer leave a reply artifact: its reply file, or the final-message file a headless
    harness writes? Neither is silence, whatever the reply says."""
    return _reply_file(output) is not None


def _reply_text(d: dict, ddir: Path, output: Path) -> str:
    """The reviewer's reply: its reply file, or (headless only) the captured
    stdout log, which is also a file the harness wrote. Never pane text."""
    if output.is_file() and output.stat().st_size:
        return output.read_text(encoding="utf-8", errors="replace")
    if d.get("launcher") in ("herdr", "external"):
        return ""
    last_message = output.with_name("last-message.txt")
    if last_message.is_file() and last_message.stat().st_size:
        return last_message.read_text(encoding="utf-8", errors="replace")
    return _log_text(d, ddir)


def _log_text(d: dict, ddir: Path) -> str:
    """The harness's own output. A herdr pane agent has no log file, so its
    pane text stands in (saved once to pane-tail.txt): a quota or auth wall
    the harness prints but does not exit on shows only there (#384)."""
    tail = ddir / "pane-tail.txt"
    if d.get("launcher") == "herdr" and not tail.is_file():
        from office import dispatch as dispatch_mod
        snap = dispatch_mod.pane_text(d)
        if snap:
            tail.write_text(snap, encoding="utf-8")
    return "\n".join(log.read_text(encoding="utf-8", errors="replace")
                     for log in (Path(d.get("log_path") or ddir / "output.log"), tail) if log.is_file())


def _reprompt_until_valid(con, run: dict, d: dict, ddir: Path, output: Path, parsed, *, plan_review: bool,
                          visual: bool) -> tuple[str, "review_parse.Parsed", str | None]:
    """Ask the same reviewer to rewrite its reply file until it parses, up to
    gates.review_reprompt_max. Returns (text, parsed, attention reason or None)."""
    from office import dispatch as dispatch_mod
    gates_cfg = run.get("gates") or {}
    limit = int(gates_cfg.get("review_reprompt_max", 3))
    wait_s = float(os.environ.get("OFFICE_REVIEW_REPROMPT_WAIT") or gates_cfg.get("review_reprompt_wait_seconds", 900))
    poll = float(os.environ.get("OFFICE_REVIEW_REPROMPT_POLL", "5"))
    name = dispatch_mod.herdr_agent_name(d["id"])
    errors = parsed.errors or ["no reply file"]
    text = ""
    if d.get("launcher") in ("sync", "process", "process-fallback"):
        log = Path(d.get("log_path") or ddir / "output.log")
        return text, parsed, (f"reviewer {d['id']} ({d.get('triple')}) left no valid reply file: "
                              f"{'; '.join(errors[:3])}; no re-prompt was possible because the session was headless "
                              f"({d['launcher']}). Inspect {log}, {output}, {output.with_name('last-message.txt')} "
                              f"and {ddir / 'reply.invalid-*.txt'}; rerun the review or waive the gate")
    for n in range(1, limit + 1):
        if d.get("launcher") != "herdr" or not _agent_alive(name):
            break  # no live session to ask: the orchestrator decides (never a substitute)
        if output.is_file():
            output.replace(output.with_name(f"reply.invalid-{n}.txt"))
        prompt = (f"Office could not read your review ({'; '.join(errors[:4])}). Write your complete review again, "
                  f"in the format the brief requires (a VERDICT line first), to {output}. {REPLY_FILE_RULE}")
        # Left unsubmitted in the composer, it gets Enter, never a second copy.
        got = dispatch_mod.submit_prompt(name, prompt, pane=d.get("pane_id"))
        unsent = f" ({HELD_PROMPT})" if got == "held" else ""
        with db.transaction(con):
            state.emit(con, run, "review.reprompt", f"{d.get('task_id') or 'plan'} {d['role']} {d['id']}: re-prompted "
                       f"{n}/{limit}{unsent} ({'; '.join(errors[:2])})", audience="runtime", task_id=d.get("task_id"),
                       dispatch_id=d["id"])
        if got == "held":
            # No reply can come from a prompt the reviewer never received.
            return text, parsed, (f"reviewer {d['id']} ({d.get('triple')}): Office's re-prompt is {HELD_PROMPT} "
                                  f"in herdr agent {name}; submit it (herdr agent send-keys {name} "
                                  "Enter) or waive the gate")
        text = _await_file(output, wait_s, poll)
        parsed = review_parse.parse(_last_block(text), plan_review=plan_review, visual=visual, contract=contract.of(run))
        if parsed.valid:
            return text, parsed, None
        errors = parsed.errors or ["no reply file"]
    reason = (f"reviewer {d['id']} ({d.get('triple')}) left no valid reply file after re-prompting: "
              f"{'; '.join(errors[:3])}; its pane is kept. Re-prompt it (office prompt {d['id']} -- \"<message>\") "
              "or waive the gate")
    return text, parsed, reason


def _agent_alive(name: str) -> bool:
    try:
        return subprocess.run(["herdr", "agent", "get", name], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _await_file(output: Path, wait_s: float, poll: float) -> str:
    """Wait for the reviewer to write the reply file and stop growing."""
    deadline = time.time() + wait_s
    last = None
    while time.time() < deadline:
        size = output.stat().st_size if output.is_file() else 0
        if size and size == last:
            return output.read_text(encoding="utf-8", errors="replace")
        last = size or None
        time.sleep(poll)
    return output.read_text(encoding="utf-8", errors="replace") if output.is_file() else ""


def _producer_route(con, gate: dict) -> str | None:
    if not gate.get("revision_id"):
        return None
    row = con.execute("SELECT d.triple FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id WHERE r.id=?",
                      (gate["revision_id"],)).fetchone()
    return row["triple"] if row else None


def _reviewer_resume(con, run: dict, parent_id: str, profile_kind: str, cwd: Path) -> tuple[dict | None, str | None]:
    """(resume spec or None, the parent's route) for continuing reviewer `parent_id`.
    Without a resumable session the route alone is reused: a fresh session there."""
    from office import adapters, dispatch as dispatch_mod, rerun
    parent = state.get_dispatch(con, parent_id)
    if parent is None:
        return None, None
    same_route = parent["triple"]
    adapter = adapters.load_all().get(parent.get("adapter_id") or parent.get("harness") or "")
    argv = None
    why = None
    if not parent.get("session_id"):
        why = "no stored harness session id"
    elif adapter is None:
        why = f"no adapter {parent.get('adapter_id') or parent.get('harness')}"
    elif not dispatch_mod.herdr_usable():
        why = "a native resume needs a herdr session"
    else:
        argv = adapters.resume_argv(adapter, profile_kind, session_id=parent["session_id"],
                                    model=parent.get("model") or "", effort=parent.get("effort") or "", cwd=cwd)
        if argv is None:
            why = f"adapter {adapter.get('id')} declares no resume form"
        elif rerun.agent_alive(parent) is not False:
            why = "its agent may still be running"
    if why:
        with db.transaction(con):
            state.emit(con, run, "review.resume_fallback", f"cannot resume reviewer {parent_id} ({why}); "
                       f"a fresh session runs on its route {same_route}", audience="runtime")
        return None, same_route
    return {"parent": parent_id, "session_id": parent["session_id"], "argv": argv[0], "herdr_kind": argv[1]}, same_route


def _reviewer_dispatch(con, run: dict, gate: dict, role: str, decision: dict) -> str:
    from office import dispatch as dispatch_mod
    cand = decision["candidate"]
    dispatch_id = "D" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, selection_reason, started_at, "
                "task_id, kind, office_version, status, harness, model, effort, adapter_id, route_json, gate_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, run["id"], role, dispatch_id, routing.candidate_id(cand), cand.get("invocation_model_id"),
                 (decision.get("selection_disclosure") or {}).get("reason"), now_iso(), gate.get("task_id"), "reviewer",
                 version.current(), "launching", cand["harness"], cand.get("invocation_model_id"), cand.get("effort"),
                 cand.get("adapter_id"), dumps(dispatch_mod._route_payload(decision)), gate["id"]))
    if decision.get("override"):
        con.execute("UPDATE dispatches SET override_json=? WHERE id=?",
                    (dumps({"by": "user", "declared": True, "triple": routing.candidate_id(cand),
                            **(decision.get("launch") or {})}), dispatch_id))
    con.execute("INSERT INTO routing_decisions(id, run_id, role, request_hash, selected_triple, decision_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], role, sha256_obj(role + gate["id"]),
                                          decision.get("selected"), decision.get("decision_hash"), now_iso()))
    return dispatch_id


def _last_block(text: str) -> str:
    """Reviewers sometimes narrate before answering; keep the reply from the
    last VERDICT line onward plus the finding lines around it."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if re.search(r"VERDICT\s*[:=]", l, re.I)]
    if not idx:
        return text
    start = idx[-1]
    while start > 0 and re.match(r"^(EVIDENCE_STATUS|FINDING|DEFECT|RESOLVED|CLEARED|RETRACT|NEXT|DECISION|WHY|AFFECTS)",
                                 review_parse._clean(lines[start - 1]), re.I):
        start -= 1
    return "\n".join(lines[start:])


def _squash(text: str) -> str:
    """Whitespace collapsed: a pane's width can break a wall's sentence anywhere."""
    return re.sub(r"\s+", " ", text[-2000:])


def _quota_signature(text: str) -> bool:
    """A usage or rate wall in the harness's own output. The phrases a harness prints when it stops, not the bare
    words `quota` or `exhausted`, and a bare 429 only where it is a status (`HTTP 429`, `Error: 429.`, `429 Too
    Many Requests`): a review of quota code, a sha, a `file.py:429` reference are no wall."""
    return bool(re.search(r"rate.?limit|usage limit|session limit|weekly limit|(?:monthly|daily|credit|token) limit"
                          r"|quota (?:exceeded|exhausted|reached|limit)|exceeded (?:your |the )?(?:current )?quota"
                          r"|(?:insufficient|out of) (?:quota|credits?)|resource.?exhausted|too many requests"
                          r"|exhausted your capacity|quota (?:is|has been|was) (?:exhausted|exceeded)"
                          r"|\b(?:http|status|code|error|err|response)\b[^\d\n]{0,12}(?:\d\.\d )?429(?![\w/:-]|\.\d)",
                          _squash(text), re.I))


def _auth_signature(text: str) -> bool:
    """The harness is not signed in: every route on it will fail the same way."""
    return bool(re.search(r"not logged in|please run /login|authentication (failed|required)|unauthorized|\b401\b",
                          _squash(text), re.I))


def reviewer_wall(text: str) -> dict | None:
    """The wall a reviewer's harness stopped on, from its log or pane text:
    {"kind": "quota"|"auth", "label": "resets 9:30pm (Asia/Manila)" or None,
    "resets_at": iso or None}. A usage-limit screen shows only in the pane, and
    the harness does not exit on it (#384), so the pane text is read too."""
    from office import dispatch as dispatch_mod
    limit = dispatch_mod._usage_limit(text)
    if limit is not None or _quota_signature(text):
        limit = limit or {}
        resets = limit.get("resets_at")
        return {"kind": "quota", "label": limit.get("label"), "resets_at": resets.isoformat() if resets else None}
    if _auth_signature(text):
        return {"kind": "auth", "label": None, "resets_at": None}
    return None


def _failure_reason(d: dict, triple: str, wall: dict | None, *, unusable: list[str] | None = None) -> str:
    """One line saying why a reviewer produced no review. A wall is named as the
    cause, never reported as an empty reply; a dispatch that ended with no
    classification says what state it was left in (#305 B4)."""
    if wall and wall["kind"] == "quota":
        return (f"{triple}: quota stall{' (' + wall['label'] + ')' if wall.get('label') else ''}: the harness stopped on "
                "its usage limit before it wrote a review")
    if wall:
        return f"{triple}: auth wall: the harness is not signed in, so it wrote no review"
    if unusable is not None:
        return (f"{triple}: no usable reply after re-prompting ({'; '.join(unusable[:2]) or 'nothing written'}) "
                f"[{_end_state(d)}]")
    if d.get("terminal_classification") is None:
        return f"{triple}: ended with no recorded classification [{_end_state(d)}] and no reply"
    return f"{triple}: exit {d.get('exit_code')} ({d.get('terminal_classification')}) with no reply"


def _end_state(d: dict) -> str:
    """What is known of a dispatch's end: its status, launcher, supervisor and
    the last thing recorded about it."""
    bits = [f"status {d.get('status')}", f"launcher {d.get('launcher') or 'none'}"]
    if d.get("pid"):
        bits.append(f"supervisor pid {d['pid']} {'alive' if pid_alive(d['pid']) else 'gone'}")
    if d.get("terminal_classification"):
        bits.append(f"classified {d['terminal_classification']}")
    if d.get("exit_code") is not None:
        bits.append(f"exit {d['exit_code']}")
    return ", ".join(bits)


def _record_env_failure(con, gate: dict, dispatch_id: str, wall: dict | None) -> None:
    with db.transaction(con):
        con.execute("UPDATE gates SET env_failures=env_failures+1 WHERE id=?", (gate["id"],))
        con.execute("UPDATE dispatches SET attribution='adapter', outcome='environment_failure' WHERE id=?", (dispatch_id,))
        if wall and wall["kind"] == "quota":
            con.execute("UPDATE dispatches SET stall_kind='usage_limit', resets_at=?, limit_label=? WHERE id=?",
                        (wall.get("resets_at"), wall.get("label"), dispatch_id))


def require_orchestrator(con, run: dict, what: str, *, code: str = "worker-cannot-review") -> None:
    """Refuse a dispatched agent (planner, executor, reviewer): `what` is the orchestrator's act. A
    worker is known by its dispatch identity, and by standing in a task worktree of this run, which
    an agent that dropped its environment still does."""
    from office import discovery
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused(code, f"a dispatched agent (a producer or worker) may not {what}: that is the orchestrator's")
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None  # a deleted working directory is not a task worktree
    found = discovery.task_worktree(con, cwd) if cwd else None
    if found and found[0]["id"] == run["id"]:
        raise Refused(code, f"this is {found[1]['task_id']}'s task worktree, so this session is its worker, which may not "
                            f"{what}: that is the orchestrator's", scope=found[1]["task_id"])


def actor_identity() -> str:
    """Who is acting: the calling agent session when the harness named it, else
    just `orchestrator`. Recorded beside a fallback review."""
    harness, session = os.environ.get("OFFICE_HARNESS"), os.environ.get("OFFICE_SESSION")
    return f"orchestrator {harness}:{session}" if harness and session else "orchestrator"


def orchestrator_produced(con, run: dict, task_ids: list[str]) -> str | None:
    """Why the orchestrator may not review these tasks' work, or None. A task whose
    revision came from an external session (hosted by whoever the orchestrator
    handed it to, Office cannot tell that from its own) was produced outside
    Office's control: its review is never the orchestrator's to give."""
    for tid in task_ids:
        row = con.execute("SELECT d.id FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id WHERE r.run_id=? "
                          "AND r.task_id=? AND d.launcher='external' LIMIT 1", (run["id"], tid)).fetchone()
        if row:
            return (f"{tid} was produced in an external session ({row['id']}) that Office cannot tell from the "
                    "orchestrator's own, so the orchestrator is not independent of it")
    return None


def review_target(gate: dict) -> str | None:
    """What `office rerun <target> --review` names for this gate: the task for a
    task code review, `<scope>:<convergence|visual>` for a lane gate."""
    if gate.get("subject") == "lane" and gate.get("scope"):
        from office import convergence
        return convergence.gate_name(gate["scope"], gate["kind"])
    return gate.get("task_id")


def reviewer_tried(con, run_id: str, gate_ids: list[str]) -> set[str]:
    """Every reviewer route already used on these gates, plus the harnesses whose
    dispatches ended on an environment failure: none of them is a next fallback."""
    if not gate_ids:
        return set()
    out = set()
    marks = ",".join("?" * len(gate_ids))
    for r in con.execute(f"SELECT triple, harness, outcome FROM dispatches WHERE run_id=? AND gate_id IN ({marks})",
                         (run_id, *gate_ids)).fetchall():
        if r["triple"]:
            out.add(r["triple"])
        if r["outcome"] == "environment_failure" and r["harness"]:
            out.add(f"harness:{r['harness']}")
    return out


def next_reviewer_route(con, run: dict, role: str, task_id: str | None, tried: set[str]) -> str | None:
    """The route the router would pick for this review once `tried` are out, in the
    `harness/model[@effort]` form --review-as takes, or None when none qualifies.
    Read-only: it names the next fallback, it does not run it, and it probes no quota (it
    runs on every `office status`), so the route it names may still be walled when it runs."""
    try:
        decision = candidates.route_role(con, state.pinned_config(run), run, role, task_id=task_id, exclude=set(tried),
                                         probe=False)
    except Exception:  # noqa: BLE001 - a routing failure leaves the next step to the orchestrator
        return None
    if decision.get("status") != "selected":
        return None
    return candidates.declared_route(decision["candidate"])


def _pinned_stall_hint(con, run: dict, gate: dict, role: str, stall: tuple, excluded: set) -> str:
    """A pinned reviewer is never substituted, so a wall on it ends the review:
    say so, and name the command that re-runs only the review on another route."""
    triple, wall = stall
    target = review_target(gate)
    nxt = next_reviewer_route(con, run, role, gate.get("task_id"),
                              excluded | {triple} | reviewer_tried(con, run["id"], [gate["id"]]))
    kind = (wall or {}).get("kind")
    return (f"pinned reviewer {triple} {'hit a ' + kind + ' wall' if kind else 'ended without a review'} and is not "
            "substituted; re-run only the review on another route: "
            + (f"office rerun {target} --review --review-as {nxt or '<harness/model[@effort]>'}" if target
               else "office resume"))


def job_review(con, run: dict, job: dict) -> dict:
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (job["payload"]["gate_id"],)).fetchone())
    if gate["status"] not in ("queued", "running"):
        return {"skipped": gate["status"]}
    task = state.get_task(con, run["id"], gate["task_id"])
    rev = dict(con.execute("SELECT * FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone())
    checkout = detached_checkout(run, rev["commit_sha"], f"review-{gate['id']}", purpose="review")
    try:
        diff = paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"])
        diff = cap_diff(diff)
        checks = con.execute("SELECT summary, verdict FROM gates WHERE revision_id=? AND kind='checks' AND status='done'",
                             (rev["id"],)).fetchone()
        carried = [dict(r) for r in con.execute("SELECT code, severity, level, location, summary FROM findings WHERE run_id=? "
                                                "AND task_id=? AND gate_kind='code_review' AND state='open'",
                                                (run["id"], task["id"])).fetchall()]
        evidence = None
        if not task["scope"]:
            ev = briefs.evidence_path(run, rev["dispatch_id"], rev["id"])
            evidence = ev.read_text(encoding="utf-8", errors="replace") if ev.is_file() else None
        brief = briefs.code_review_brief(run, task, rev, diff, checks["summary"] if checks else "none declared",
                                         carried, str(checkout), evidence=evidence,
                                         verify_only=_verify_only(con, run, gate))
        exclude = [job["payload"]["exclude_route"]] if job["payload"].get("exclude_route") else None
        outcome = run_reviewer(con, run, gate, "code_reviewer", brief, cwd=checkout, include_dirs=[checkout],
                               exclude=exclude)
    finally:
        remove_checkout(run, checkout)
    with db.transaction(con):
        ingest_task_gate(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome["verdict"]}


def detached_checkout(run: dict, commit: str, name: str, *, purpose: str) -> Path:
    """A detached worktree at `commit`. `purpose` is required: only "check" runs worktree.setup;
    review, deploy, rebase-trial, and other checkouts stay exactly at the commit."""
    path = paths.run_dir(run["id"]) / "checkouts" / name
    if path.exists():
        remove_checkout(run, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.git(Path(run["repo_root"]), "worktree", "add", "--detach", str(path), commit)
    if purpose == "check":
        worktree_setup.prepare(run, path, "check", paths.run_dir(run["id"]) / "setup" / f"{name}.log", created=True)
    plans = Path(run["repo_root"]) / ".office" / "plans"
    if plans.is_dir():
        dest = path / ".office" / "plans"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            try:
                dest.symlink_to(plans, target_is_directory=True)
            except OSError:
                shutil.copytree(plans, dest)
    return path


def remove_checkout(run: dict, path: Path) -> None:
    subprocess.run(["git", "-C", run["repo_root"], "worktree", "remove", "--force", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------------ ingest

def _fingerprint(f: dict) -> str:
    words = re.findall(r"[a-z0-9_./]+", ((f.get("location") or "") + " " + (f.get("summary") or "")).lower())
    return sha256_obj(sorted(set(w for w in words if len(w) > 2))[:40])


def checks_outcome(outcome: dict) -> dict:
    """A run_commands result in convergence-contract terms: all checks passed is
    APPROVED, a failing check is RECHECK (a blocking finding), and a check that
    could not run is UNAVAILABLE status with no verdict. STALE stays STALE."""
    v = outcome.get("verdict")
    if v == "STALE":
        return outcome
    if v == "UNAVAILABLE":
        return {**outcome, "status": contract.UNAVAILABLE, "verdict": None}
    parsed = outcome.get("parsed")
    findings = [{**f, "severity": "high", "level": "high", "blocking": True} for f in (parsed.findings if parsed else [])]
    verdict = "RECHECK" if findings else "APPROVED"
    return {**outcome, "status": contract.COMPLETED, "verdict": verdict,
            "parsed": review_parse.Parsed(verdict=verdict, findings=findings, contract=contract.CONVERGENCE,
                                          next_action="make the failing check pass" if findings else "proceed")}


def _ingest_checks_convergence(con, run: dict, gate: dict, task: dict, outcome: dict) -> None:
    """#337 task gate: deterministic checks only. Caller holds tx."""
    outcome = checks_outcome(outcome)
    verdict, status = outcome.get("verdict"), outcome.get("status")
    if verdict == "STALE" or (verdict is None and status is None):
        con.execute("UPDATE gates SET status='stale', stale_reason=?, finished_at=? WHERE id=?",
                    (outcome.get("summary"), now_iso(), gate["id"]))
        return
    if task["current_revision_id"] != gate["revision_id"] or gate["status"] == "cancelled":
        con.execute("UPDATE gates SET status='stale', verdict=?, review_status=?, stale_reason=?, finished_at=? WHERE id=?",
                    (verdict, status, f"revision {gate['revision_id']} is no longer current", now_iso(), gate["id"]))
        return
    con.execute("UPDATE gates SET status='done', verdict=?, review_status=?, summary=?, finished_at=?, "
                "route=COALESCE(?, route), contract=? WHERE id=?",
                (verdict, status, outcome.get("summary"), now_iso(), outcome.get("route"), contract.CONVERGENCE, gate["id"]))
    if status == contract.UNAVAILABLE:
        state.emit(con, run, "gate.unavailable", f"{task['id']} checks UNAVAILABLE (not a verdict): "
                   f"{outcome.get('summary', '')[:400]}", task_id=task["id"])
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"checks gate unavailable: {outcome.get('summary', '')[:400]}")
        return
    parsed = outcome["parsed"]
    if verdict == "APPROVED":
        con.execute("UPDATE findings SET state='resolved', updated_at=? WHERE run_id=? AND task_id=? AND gate_kind='checks' "
                    "AND state='open'", (now_iso(), run["id"], task["id"]))
        state.emit(con, run, "gate.approved", f"{task['id']} checks APPROVED on {gate['revision_id']}", audience="runtime",
                   task_id=task["id"])
        evaluate_acceptance(con, run, task["id"])
        return
    for f in parsed.findings:
        _upsert_finding(con, run, task, gate, f, outcome)
    if int(gate["round"] or 1) >= contract.MAX_ROUNDS:
        # Deterministic checks are the producer's job, not a reviewer's: at the
        # cap the operator decides, nothing escalates on its own.
        _pause(con, run, task, f"checks still failing after {contract.MAX_ROUNDS} rounds; the operator decides "
                               f"(office rerun {task['id']} --fresh, amend the task, or stop it)")
        return
    deliver_findings(con, run, task, gate)


def ingest_task_gate(con, run: dict, gate_id: str, outcome: dict) -> None:
    """Record one task-gate result and re-evaluate the task. Caller holds tx."""
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
    if contract.is_convergence(run) and gate["kind"] == "checks":
        _ingest_checks_convergence(con, run, gate, state.get_task(con, run["id"], gate["task_id"]), outcome)
        return
    task = state.get_task(con, run["id"], gate["task_id"])
    verdict = outcome["verdict"]
    parsed: review_parse.Parsed | None = outcome.get("parsed")
    current = task["current_revision_id"] == gate["revision_id"]
    if verdict == "STALE":
        con.execute("UPDATE gates SET status='stale', stale_reason=?, finished_at=? WHERE id=?",
                    (outcome.get("summary"), now_iso(), gate_id))
        return
    if not current or gate["status"] == "cancelled":
        # Stale result: audit only. Its open findings carry forward for the
        # next review of the current revision to confirm or retract.
        con.execute("UPDATE gates SET status='stale', verdict=?, stale_reason=?, finished_at=?, route=COALESCE(route, ?) "
                    "WHERE id=?", (verdict, f"revision {gate['revision_id']} is no longer current", now_iso(),
                                   outcome.get("route"), gate_id))
        if parsed:
            for f in parsed.findings:
                if f["severity"] == "material":
                    _upsert_finding(con, run, task, gate, f, outcome, carried=True)
        return
    evidence_status = outcome.get("evidence_status")
    con.execute("UPDATE gates SET status='done', verdict=?, evidence_status=COALESCE(?, evidence_status), summary=?, "
                "finished_at=?, route=COALESCE(?, route) WHERE id=?",
                (verdict, evidence_status, outcome.get("summary"), now_iso(), outcome.get("route"), gate_id))
    kind_label = {"checks": "checks", "code_review": "code", "visual": "ui"}.get(gate["kind"], gate["kind"])
    if gate["kind"] in ("code_review", "visual") and verdict in ("PASS", "CHANGES_REQUIRED"):
        from office import prs
        prs.queue(con, run, task["id"], "verdict", gate_id)
    if parsed:
        for code in parsed.resolved:
            _set_state(con, run, task, gate["kind"], code, "resolved")
        for r in parsed.retracted:
            _set_state(con, run, task, gate["kind"], r["code"], "retracted")
        seen = set()
        for f in parsed.findings:
            seen.add(f["code"])
            _upsert_finding(con, run, task, gate, f, outcome)
        if verdict == "CHANGES_REQUIRED" and _verify_only(con, run, gate) and not _open_high(con, run, task, gate["kind"]):
            _defer_findings(con, run, task, gate)
            verdict = "PASS"
        if verdict == "PASS":
            # A PASS on the current revision resolves every earlier open
            # finding of this gate kind that it did not restate.
            con.execute("UPDATE findings SET state='resolved', updated_at=? WHERE run_id=? AND task_id=? AND gate_kind=? "
                        "AND state='open'", (now_iso(), run["id"], task["id"], gate["kind"]))
    if verdict == "PASS":
        state.emit(con, run, "gate.pass", f"{task['id']} {kind_label} PASS on {gate['revision_id']}", audience="runtime",
                   task_id=task["id"])
        if gate["kind"] == "checks":
            start_waiting(con, run, task["id"], gate["revision_id"])
    elif verdict == "ATTENTION":
        # The reviewer answered but never left a readable reply file. Its work
        # and pane are kept; the orchestrator decides (R11). Never UNAVAILABLE.
        state.emit(con, run, "gate.attention", f"{task['id']} {kind_label} needs attention: "
                   f"{outcome.get('summary', '')[:220]}", task_id=task["id"])
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"{kind_label} review needs attention: {outcome.get('summary', '')[:200]}")
        return
    elif verdict == "UNAVAILABLE":
        state.emit(con, run, "gate.unavailable", f"{task['id']} {kind_label} UNAVAILABLE: {outcome.get('summary', '')[:400]}; "
                   "valid unrelated results are preserved", task_id=task["id"])
        if gate["kind"] == "checks":
            con.execute("UPDATE gates SET status='cancelled', stale_reason='checks unavailable' WHERE revision_id=? "
                        "AND status='waiting'", (gate["revision_id"],))
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"{kind_label} gate unavailable: {outcome.get('summary', '')[:400]}")
        return
    elif verdict == "BRIEF_DEFECT":
        state.update_task(con, run["id"], task["id"], status="paused", pause_reason=f"brief defect from {kind_label} review")
        state.emit(con, run, "gate.brief_defect", f"{task['id']} BRIEF_DEFECT from {kind_label} review: "
                   + (parsed.findings[0]["summary"][:140] if parsed and parsed.findings else ""), task_id=task["id"])
        return
    elif verdict == "CHANGES_REQUIRED":
        if gate["kind"] == "checks":
            con.execute("UPDATE gates SET status='cancelled', stale_reason='checks failed' WHERE revision_id=? AND status='waiting'",
                        (gate["revision_id"],))
        _converge(con, run, task, gate, outcome)
        return
    evaluate_acceptance(con, run, task["id"])


def _upsert_finding(con, run, task, gate, f, outcome, carried: bool = False) -> None:
    fp = _fingerprint(f)
    existing = con.execute("SELECT id, state FROM findings WHERE run_id=? AND task_id=? AND gate_kind=? AND code=? "
                           "AND state IN ('open','minor')", (run["id"], task["id"], gate["kind"], f["code"])).fetchone()
    if "blocking" in f:
        # Convergence contract: blocking is the reviewer's call, not the severity's.
        new_state = "open" if f["blocking"] else "minor"
        f = {**f, "severity": "material" if f["blocking"] else "minor", "level": f.get("level") or f.get("severity")}
    else:
        new_state = "open" if f["severity"] == "material" else "minor"
    if existing:
        con.execute("UPDATE findings SET summary=?, location=?, action=?, fingerprint=?, gate_id=?, revision_id=?, "
                    "measurement_json=?, updated_at=?, state=?, level=? WHERE id=?",
                    (f["summary"], f.get("location"), f.get("action"), fp, gate["id"], gate["revision_id"],
                     dumps(f.get("measurement")) if f.get("measurement") else None, now_iso(), new_state,
                     f.get("level") or "high", existing["id"]))
        return
    fid = "F" + uuid.uuid4().hex[:10]
    reviewer = outcome.get("dispatch_id")
    producer = con.execute("SELECT dispatch_id FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone()
    con.execute("INSERT INTO findings(id, dispatch_id, reviewer_dispatch_id, status, severity, summary, evidence_hash, created_at, "
                "run_id, task_id, gate_id, revision_id, gate_kind, code, fingerprint, location, category, action, "
                "measurement_json, state, origin_gate_id, updated_at, level) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, producer["dispatch_id"] if producer else None, reviewer,
                 "accepted-material" if f["severity"] == "material" else "minor", f["severity"], f["summary"],
                 sha256_obj(f), now_iso(), run["id"], task["id"], gate["id"], gate["revision_id"], gate["kind"], f["code"], fp,
                 f.get("location"), "carried" if carried else gate["kind"], f.get("action"),
                 dumps(f.get("measurement")) if f.get("measurement") else None, new_state, gate["id"], now_iso(),
                 f.get("level") or "high"))


def _set_state(con, run, task, kind, code, new_state):
    con.execute("UPDATE findings SET state=?, updated_at=? WHERE run_id=? AND task_id=? AND gate_kind=? AND code=? AND state='open'",
                (new_state, now_iso(), run["id"], task["id"], kind, code))


def _max_rounds(run: dict, kind: str) -> int:
    g = run.get("gates") or {}
    return int({"code_review": g.get("code_review_max_rounds"), "visual": g.get("visual_review_max_rounds"),
                "checks": g.get("code_review_max_rounds")}.get(kind) or 2)


def _open_high(con, run: dict, task: dict, kind: str) -> bool:
    """Is an open blocking finding graded high? A finding recorded before
    levels existed has none and counts as high."""
    return con.execute("SELECT 1 FROM findings WHERE run_id=? AND task_id=? AND gate_kind=? AND state='open' "
                       "AND COALESCE(level, 'high')='high' LIMIT 1", (run["id"], task["id"], kind)).fetchone() is not None


def _final_fix_used(con, run: dict, task_id: str) -> bool:
    return con.execute("SELECT 1 FROM events WHERE run_id=? AND task_id=? AND kind='gate.final_fix_round' LIMIT 1",
                       (run["id"], task_id)).fetchone() is not None


def _verify_only(con, run: dict, gate: dict) -> bool:
    """A code review after the final fix round, or past the round budget, is
    verify-only: only a high finding blocks. Visual review keeps its own
    material|minor format and its escalation path."""
    if gate["kind"] != "code_review":
        return False
    return int(gate["round"] or 0) > _max_rounds(run, "code_review") or _final_fix_used(con, run, gate["task_id"])


def _defer_findings(con, run: dict, task: dict, gate: dict) -> None:
    """Verify-only review with no high finding: the open medium findings become
    follow-ups and the gate passes. The reviewer's own verdict stays in the summary."""
    rows = con.execute("SELECT code, level, location, summary FROM findings WHERE run_id=? AND task_id=? AND gate_kind=? "
                       "AND state='open'", (run["id"], task["id"], gate["kind"])).fetchall()
    con.execute("UPDATE findings SET state='deferred', updated_at=? WHERE run_id=? AND task_id=? AND gate_kind=? "
                "AND state='open'", (now_iso(), run["id"], task["id"], gate["kind"]))
    text = "; ".join(f"{r['code']} [{r['level'] or 'medium'}] {r['location'] or ''} {r['summary'][:100]}" for r in rows)
    con.execute("UPDATE gates SET verdict='PASS', summary=? WHERE id=?",
                (f"verify-only: CHANGES_REQUIRED with no high finding; {len(rows)} deferred as follow-ups", gate["id"]))
    state.emit(con, run, "gate.followups", f"{task['id']} {gate['kind']} passed its verify-only round; follow-ups to file: "
               f"{text or 'none'}", task_id=task["id"], payload={"findings": [dict(r) for r in rows]})


def _converge(con, run: dict, task: dict, gate: dict, outcome: dict) -> None:
    """Bounded convergence: fix round, or no-progress / budget -> one
    escalation -> pause with work preserved. With no high finding open, the
    budget buys one final fix round and a verify-only review instead."""
    maximum = _max_rounds(run, gate["kind"])
    repeats = int((state.pinned_config(run).get("verification") or {}).get("no_progress_repeats", 2))
    no_progress = _no_progress(con, run, task, gate, repeats)
    budget_spent = gate["round"] >= int(maximum)
    if (no_progress or budget_spent) and gate["kind"] == "code_review" \
            and not _open_high(con, run, task, gate["kind"]) and not _final_fix_used(con, run, task["id"]):
        why = "no progress on a repeated finding" if no_progress else "round budget spent"
        state.emit(con, run, "gate.final_fix_round", f"{task['id']} {gate['kind']}: {why} and every open "
                   "finding is medium or low; one final fix round, then a verify-only review where only a high "
                   "finding blocks", task_id=task["id"])
        deliver_findings(con, run, task, gate)
        return
    if gate["escalated"]:
        _pause(con, run, task, f"{gate['kind']} still CHANGES_REQUIRED after escalation")
        return
    if no_progress or budget_spent:
        why = "no progress on a repeated finding" if no_progress else f"round budget {maximum} spent"
        if not task["escalations_used"] and gate["kind"] != "checks":
            state.update_task(con, run["id"], task["id"], escalations_used=1)
            gid = _new_gate(con, run, task, gate["revision_id"], gate["kind"], gate["input_key"] + ":escalation", "queued",
                            escalated=1, round_no=gate["round"])
            kind = "review" if gate["kind"] == "code_review" else "visual_review"
            payload = {"gate_id": gid, "task_id": task["id"], "exclude_route": outcome.get("route"), "escalation": True}
            if gate["kind"] == "visual":
                payload["capture_gate_id"] = gate["id"]
            state.enqueue(con, run, kind, payload, dedup_key=f"escalate:{gid}", max_attempts=2)
            state.emit(con, run, "gate.escalated", f"{task['id']} {gate['kind']}: {why}; escalation 1/1 queued",
                       task_id=task["id"])
            return
        _pause(con, run, task, f"{gate['kind']}: {why}; escalation {'exhausted' if task['escalations_used'] else 'not applicable'}")
        return
    deliver_findings(con, run, task, gate)


def _no_progress(con, run, task, gate, repeats: int) -> bool:
    fps_now = {r["fingerprint"] for r in con.execute(
        "SELECT fingerprint FROM findings WHERE gate_id=? AND state='open'", (gate["id"],)).fetchall()}
    if not fps_now:
        return False
    history = con.execute("SELECT id FROM gates WHERE run_id=? AND task_id=? AND kind=? AND verdict='CHANGES_REQUIRED' "
                          "AND status IN ('done','stale') AND id!=? ORDER BY created_at DESC LIMIT ?",
                          (run["id"], task["id"], gate["kind"], gate["id"], repeats - 1)).fetchall()
    if len(history) < repeats - 1:
        return False
    for h in history:
        prior = {r["fingerprint"] for r in con.execute("SELECT fingerprint FROM findings WHERE origin_gate_id=? OR gate_id=?",
                                                       (h["id"], h["id"])).fetchall()}
        if not fps_now & prior:
            return False
    return True


def _pause(con, run, task, reason: str) -> None:
    preserved = task["current_revision_id"]
    passing = [r["kind"] for r in con.execute("SELECT kind FROM gates WHERE revision_id=? AND verdict IN ('PASS','APPROVED') AND status='done'",
                                              (preserved,)).fetchall()]
    state.update_task(con, run["id"], task["id"], status="paused", pause_reason=reason)
    state.emit(con, run, "task.paused", f"PAUSED {task['id']}: {reason}; preserved {preserved}"
               + (f", passing {', '.join(passing)}" if passing else ""), task_id=task["id"],
               payload={"reason": reason, "preserved_revision": preserved, "passing": passing})


def worker_live(con, dispatch_id: str | None) -> bool:
    d = state.get_dispatch(con, dispatch_id) if dispatch_id else None
    if not d or d["status"] not in ("running", "launching"):
        return False
    if d.get("launcher") in ("herdr", "external"):
        return True
    return bool(d.get("pid") and pid_alive(d["pid"]))


def deliver_findings(con, run: dict, task: dict, gate: dict) -> None:
    """Route actionable findings: a live worker gets them on its next command;
    otherwise they wait for the orchestrator to run office rerun (R8)."""
    rows = con.execute("SELECT code, severity, location, summary FROM findings WHERE run_id=? AND task_id=? AND state='open' "
                       "ORDER BY created_at", (run["id"], task["id"])).fetchall()
    text = "; ".join(f"{r['code']} {r['location'] or ''} {r['summary'][:100]}" for r in rows[:6])
    state.update_task(con, run["id"], task["id"], status="changes_required")
    if contract.is_convergence(run):
        state.emit(con, run, "gate.recheck", f"RECHECK {gate['revision_id']}: {text}",
                   audience=f"task:{task['id']}", task_id=task["id"])
    else:
        state.emit(con, run, "gate.changes_required", f"CHANGES_REQUIRED {gate['revision_id']}: {text}",
                   audience=f"task:{task['id']}", task_id=task["id"])
    if worker_live(con, task["current_dispatch_id"]):
        state.enqueue(con, run, "notify_worker", {"dispatch_id": task["current_dispatch_id"], "task_id": task["id"],
                                                  "text": f"Findings on {gate['revision_id']}: run office status, fix, then office submit."},
                      dedup_key=f"notify:{gate['id']}", max_attempts=1)
        return
    # No live worker: the findings wait for the orchestrator, who chooses to
    # resume the earlier session or start a fresh one (R8). Nothing launches here.
    state.emit(con, run, "task.findings_queued", f"{task['id']} findings on {gate['revision_id']} are waiting for you: "
               f"office rerun {task['id']} --resume | --fresh", task_id=task["id"])


# ------------------------------------------------------------------ acceptance

def required_gates(con, run: dict, task: dict, rev_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND revision_id=? AND status!='cancelled' "
                       "ORDER BY created_at", (run["id"], task["id"], rev_id)).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        latest[r["kind"]] = r
    return list(latest.values())


def evaluate_acceptance(con, run: dict, task_id: str) -> bool:
    """The one acceptance evaluator. Caller holds the tx."""
    from office import dispatch as dispatch_mod, plans
    task = state.get_task(con, run["id"], task_id)
    rev_id = task["current_revision_id"]
    if not rev_id or task["status"] in ("accepted", "cancelled", "paused", "blocked"):
        return False
    rev = con.execute("SELECT * FROM revisions WHERE id=?", (rev_id,)).fetchone()
    if rev is None or rev["status"] != "current":
        return False
    from office import authority
    convergence = contract.is_convergence(run)
    waived = authority.waived(con, run["id"], task_id)
    if convergence:
        # Only the checks gate is a task gate here; code and visual review are lane gates (waived per lane).
        waived &= {"checks"}
    gates_now = [g for g in required_gates(con, run, task, rev_id) if g["kind"] not in waived]
    basis = "all required gates PASS"
    if convergence:
        # #337: the task gate is its checks; independent review happens per lane, so a checks
        # waiver accepts the task but the lane's review still runs on the composed result.
        basis = "checks APPROVED; lane convergence pending" if gates_now else "no checks declared; lane convergence pending"
        for g in gates_now:
            if g["kind"] == "checks" and (g["status"] != "done" or g["verdict"] != "APPROVED"):
                return False
        gates_now = []
    elif "checks" in waived and _revive_cancelled_reviews(con, run, task, rev_id, waived):
        return False
    elif not gates_now and not waived:
        # A revision with no gate is accepted only when policy explicitly
        # requires none: the plan declared `checks: none`, the gear funds no
        # independent review, and nothing user-visible is in the acceptance.
        from office import visual
        explicit_none = task["checks"] == [] and not (run.get("gates") or {}).get("code_review")
        if not explicit_none or visual.applicability(con, run, task, [])["status"] != "not_applicable":
            return False
        basis = "no gate required by policy (checks: none; gear funds no review)"
    for g in gates_now:
        if g["status"] != "done" or g["verdict"] != "PASS":
            return False
        if g["kind"] == "visual" and g.get("evidence_status") not in ("COMPARABLE", "NOT_APPLICABLE"):
            return False
    d = state.get_dispatch(con, rev["dispatch_id"])
    if (rev["applied_version"] or 0) < task["contract_version"]:
        return False
    if con.execute("SELECT 1 FROM deliveries WHERE run_id=? AND task_id=? AND status IN ('queued','delivered')",
                   (run["id"], task_id)).fetchone():
        return False
    for dfct in plans.open_defects(con, run["id"]) if not convergence else plans.blocking_findings(con, run):
        ids = plans._task_ids_in(dfct.get("location") or "")
        if not ids or task_id in ids:
            return False
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep)
        if not dt or dt["status"] != "accepted":
            return False
    stale = stale_dependency(con, run, task)
    if stale:
        # Built on a dependency revision that was superseded before acceptance:
        # say so once per revision, or the task sits at `submitted` unexplained.
        if not con.execute("SELECT 1 FROM events WHERE run_id=? AND kind='task.restack_needed' AND task_id=? "
                           "AND summary LIKE ?", (run["id"], task_id, f"{task_id} {rev_id} %")).fetchone():
            state.emit(con, run, "task.restack_needed", f"{task_id} {rev_id} {stale}", task_id=task_id)
        return False
    state.update_task(con, run["id"], task_id, status="accepted", accepted_revision_id=rev_id, pause_reason=None)
    con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND task_id=? AND released_at IS NULL AND revoked_at IS NULL",
                (now_iso(), run["id"], task_id))
    con.execute("UPDATE dispatches SET outcome='pending' WHERE id=? AND outcome IS NULL", (rev["dispatch_id"],))
    state.emit(con, run, "task.accepted", f"{task_id} accepted on {rev_id}"
               + (f" ({basis})" if basis != "all required gates PASS" else "")
               + (f" with user waiver of {', '.join(sorted(waived))}" if waived else ""), task_id=task_id,
               payload={"basis": basis, "waived": sorted(waived)})
    from office import prs
    prs.queue(con, run, task_id, "accepted", rev_id)
    dispatch_mod.start_stacked(con, run, task_id)
    for other in state.tasks(con, run["id"]):
        if task_id in other["depends"] and other["status"] == "submitted":
            evaluate_acceptance(con, run, other["id"])
    if convergence:
        from office import convergence as convergence_mod
        convergence_mod.on_task_accepted(con, state.get_run(con, run["id"]), task_id)
        return True
    from office import integration
    integration.maybe_queue(con, run)
    return True


def reevaluate_submitted(con, run: dict) -> list[str]:
    """Re-run acceptance for every submitted task whose current revision has no
    gate still pending. Acceptance is otherwise evaluated only when a gate ends,
    so a revision whose gates all ended while a plan review, blocking finding or
    pause held it would sit at `submitted` forever once that hold lifts.
    Returns the tasks accepted. Caller holds the tx."""
    accepted = []
    for t in con.execute("SELECT t.id FROM tasks t WHERE t.run_id=? AND t.status='submitted' AND t.current_revision_id IS NOT NULL "
                         "AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.revision_id=t.current_revision_id "
                         "AND g.status IN ('queued','running','waiting'))", (run["id"],)).fetchall():
        if evaluate_acceptance(con, run, t["id"]):
            accepted.append(t["id"])
    return accepted


def _revive_cancelled_reviews(con, run: dict, task: dict, rev_id: str, waived: set[str]) -> bool:
    """A failed (or unavailable) check cancels the review gates that waited on it. When the
    user then waives the checks, those reviews were never run: accepting would land the
    revision with no independent review (#306). Queue each again and report that acceptance
    waits for them. A review the user waived by name stays waived. Caller holds the tx."""
    revived = False
    for g in con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND revision_id=? AND status='cancelled' "
                         "AND stale_reason IN ('checks failed','checks unavailable') AND kind IN ('code_review','visual') "
                         "ORDER BY created_at", (run["id"], task["id"], rev_id)).fetchall():
        g = dict(g)
        if g["kind"] in waived or any(o["status"] != "cancelled" for o in con.execute(
                "SELECT status FROM gates WHERE revision_id=? AND kind=? AND id!=?", (rev_id, g["kind"], g["id"]))):
            continue
        _new_gate(con, run, task, rev_id, g["kind"], f"{g['input_key']}:revived:{uuid.uuid4().hex[:6]}", "waiting",
                  round_no=g["round"], escalated=g["escalated"])
        revived = True
    if revived:
        start_waiting(con, run, task["id"], rev_id)
        state.emit(con, run, "gate.revived", f"{task['id']} {rev_id}: checks are waived, so the reviews they cancelled "
                   "now run; the task is not accepted without them", task_id=task["id"])
    return revived or bool(con.execute("SELECT 1 FROM gates WHERE revision_id=? AND status IN ('queued','running','waiting') "
                                       "AND kind IN ('code_review','visual')", (rev_id,)).fetchone())


def stale_dependency(con, run: dict, task: dict) -> str | None:
    """Why the task's current revision cannot be accepted although its
    dependencies are: it does not contain a dependency's accepted revision
    (it was built on, or stacked onto, a revision that review superseded)."""
    rev = con.execute("SELECT commit_sha, base_commit FROM revisions WHERE id=?",
                      (task.get("current_revision_id"),)).fetchone()
    if rev is None:
        return None
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep)
        if not dt or dt["status"] != "accepted" or not dt.get("accepted_revision_id"):
            continue
        dep_rev = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (dt["accepted_revision_id"],)).fetchone()
        if dep_rev is None or _is_ancestor(run, dep_rev["commit_sha"], rev["commit_sha"]):
            continue
        built = con.execute("SELECT id FROM revisions WHERE run_id=? AND task_id=? AND commit_sha=?",
                            (run["id"], dep, rev["base_commit"])).fetchone()
        on = f"{dep} {built['id']}" if built else f"{dep} {(rev['base_commit'] or '')[:7]}"
        return (f"built on {on}, but {dep} was accepted on {dt['accepted_revision_id']}; restack: "
                f"office rerun {task['id']} --resume|--fresh merges it into the worktree first")
    return None


def _is_ancestor(run: dict, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(["git", "-C", run["repo_root"], "merge-base", "--is-ancestor", ancestor, descendant],
                          capture_output=True)
    return proc.returncode == 0


def derive_status(con, run: dict, task: dict) -> str:
    if task.get("accepted_revision_id") and task["accepted_revision_id"] == task.get("current_revision_id"):
        return "accepted"
    if task.get("current_revision_id"):
        pending = con.execute("SELECT 1 FROM gates WHERE revision_id=? AND status IN ('queued','running','waiting')",
                              (task["current_revision_id"],)).fetchone()
        if pending:
            return "submitted"
    if worker_live(con, task.get("current_dispatch_id")):
        return "running"
    return "planned" if not task.get("current_dispatch_id") else "changes_required"


# ------------------------------------------------------------------ closeout

def owning_jobs(con, run: dict, gate) -> list[dict]:
    """The queued or claimed jobs that can still finish `gate`. A task or plan
    gate's job names it in its payload. An integration gate is created by the
    integrate job while it runs, so its owner is an integrate job claimed no
    later than the gate was created. Unrelated jobs in the run never count."""
    if gate["subject"] == "integration":
        rows = con.execute("SELECT id, status, claimed_pid, claimed_by FROM outbox WHERE run_id=? AND kind='integrate' "
                           "AND status='claimed' AND (claimed_at IS NULL OR claimed_at<=?)",
                           (run["id"], gate["created_at"])).fetchall()
    else:
        rows = con.execute("SELECT id, status, claimed_pid, claimed_by FROM outbox WHERE run_id=? AND status IN ('queued','claimed') "
                           "AND payload_json LIKE ?", (run["id"], f'%"{gate["id"]}"%')).fetchall()
    return [dict(r) for r in rows]


def superseded_integration_gate(con, run: dict, gate) -> bool:
    """A check or review of a composed revision that integration has since
    replaced. Its verdict can no longer matter, so it never blocks close."""
    if gate["subject"] != "integration":
        return False
    commit = ((state.get_run(con, run["id"]).get("landing") or {}).get("integration") or {}).get("commit")
    return bool(commit) and gate["input_key"] != f"integration:{commit}"


def close_blockers(con, run: dict) -> list[str]:
    from office import plans
    out = []
    for t in state.tasks(con, run["id"]):
        if t["status"] not in ("accepted", "cancelled"):
            out.append(f"{t['id']} is {t['status']}")
    if con.execute("SELECT 1 FROM deliveries WHERE run_id=? AND status IN ('queued','delivered')", (run["id"],)).fetchone():
        out.append("an amendment is delivered but not applied")
    if any(not superseded_integration_gate(con, run, g) for g in
           con.execute("SELECT * FROM gates WHERE run_id=? AND status IN ('queued','running','waiting')", (run["id"],))):
        out.append("reviews are still running")
    if contract.is_convergence(run):
        from office import convergence
        out.extend(convergence.close_blockers(con, run))
    elif plans.open_defects(con, run["id"]):
        out.append("a plan defect is open")
    rs = plans.review_state(con, run)
    if rs["required"] and rs["pending"]:
        out.append("a plan re-review is pending")
    from office import integration
    integ = integration.status(con, run)
    if integ["required"] and integ["status"] != "accepted":
        out.append(f"integration {integ['status']}")
    return out


def landing_state(con, run: dict, handoff: str | None) -> dict:
    from office import integration
    commit = integration.final_commit(con, run)
    if not commit:
        return {"status": "none", "detail": "no accepted work"}
    recorded = state.get_run(con, run["id"]).get("landing") or {}
    repo = Path(run["repo_root"])
    # A landing record covers the integration it names. When the integration
    # moved afterwards, the record says nothing about the current one. Records
    # from an older Office name no integration and keep their old meaning.
    def tree(c: str) -> str:
        return paths.git(repo, "rev-parse", f"{c}^{{tree}}", check=False)

    merged = recorded.get("merged")
    if merged:
        landed = merged.get("integration_commit")
        if landed and tree(landed) != tree(commit):
            return {"status": "pending", "detail": f"the landing at {merged['commit'][:12]} is for integration "
                    f"{landed[:12]}, but the accepted integration is now {commit[:12]}: office land", "commit": commit}
        return {"status": "landed", "detail": f"task PRs merged at {merged['commit'][:12]}", "commit": merged["commit"]}
    if recorded.get("delivered"):
        previewed = ((recorded.get("deployed") or {}).get("preview") or {}).get("tree")
        if previewed and previewed != tree(commit):
            return {"status": "pending", "detail": f"the preview deploy was of another tree ({previewed[:12]}) than the "
                    f"accepted integration {commit[:12]}: office land --preview", "commit": commit}
        return {"status": "landed", "detail": recorded["delivered"], "commit": commit}
    for target in ("origin/main", "main", "origin/master", "master"):
        proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", target], capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        if _is_ancestor(run, commit, proc.stdout.strip()):
            return {"status": "landed", "detail": f"{commit[:12]} is in {target}", "commit": commit, "target": target}
        return {"status": "pending", "detail": f"{commit[:12]} is not in {target}; open a PR or pass --handoff <ref>",
                "commit": commit, "target": target}
    return {"status": "pending", "detail": "no default branch found; pass --handoff <ref>", "commit": commit}
