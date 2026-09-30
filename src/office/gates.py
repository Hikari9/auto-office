"""Gate engine: revision-bound verdicts, selective invalidation, bounded
convergence, and the single acceptance evaluator.

Verdicts: PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT | UNAVAILABLE.
A gate that is skipped, unavailable, malformed, stale, or missing its command
is never PASS. Acceptance requires every required gate to PASS on the task's
current revision, the worker's applied version to be current, and no open
plan defect on the task's scope.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from office import briefs, candidates, db, jobs, paths, planfile, review_parse, routing, state
from office.util import dumps, now_iso, pid_alive, sha256_bytes, sha256_obj

TASK_GATES = ("checks", "code_review", "visual")
MAX_DIFF_CHARS = 120_000


# ------------------------------------------------------------------ planning

def plan_for_revision(con, run: dict, task: dict, rev_id: str, changed: list[str], d: dict) -> dict:
    """Create gate rows for a new revision and queue the first step. Caller holds tx."""
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


def _new_gate(con, run, task, rev_id, kind, key, status, *, verdict=None, reused_from=None, evidence_status=None,
              escalated=0, round_no=None) -> str:
    gid = "G" + uuid.uuid4().hex[:8]
    if round_no is None:
        round_no = current_round(con, run["id"], task["id"], kind)
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, input_key, status, verdict, "
                "evidence_status, round, escalated, reused_from, created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, run["id"], "task", task["id"], rev_id, run["plan_version"], kind, key, status, verdict, evidence_status,
                 round_no, escalated, reused_from, now_iso(), now_iso() if status == "done" else None))
    return gid


def _reusable(con, run, task, kind, key):
    row = con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND kind=? AND input_key=? AND verdict='PASS' "
                      "AND status IN ('done','stale') ORDER BY created_at DESC LIMIT 1", (run["id"], task["id"], kind, key)).fetchone()
    return dict(row) if row else None


def current_round(con, run_id: str, task_id: str, kind: str) -> int:
    rows = con.execute("SELECT DISTINCT revision_id FROM gates WHERE run_id=? AND task_id=? AND kind=? AND "
                       "verdict='CHANGES_REQUIRED' AND escalated=0", (run_id, task_id, kind)).fetchall()
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
        ingest_task_gate(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome["verdict"]}


def run_commands(con, run: dict, commands: list[str], cwd: Path, rev: dict, gate: dict, *, check_tree: bool = True) -> dict:
    from office.submit import matches_revision
    cfgv = state.pinned_config(run).get("verification") or {}
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
        if code == 127 or ("command not found" in out[-400:] and code != 0):
            return {"verdict": "UNAVAILABLE", "summary": f"check command not found: {command}", "results": results}
        if not passed:
            tail = "\n".join(out.strip().splitlines()[-12:])
            findings.append({"code": f"C{i}", "severity": "material", "location": command,
                             "summary": f"check failed (exit {code}): {tail[-600:]}", "action": "make this check pass"})
        if check_tree and not matches_revision(cwd, rev["commit_sha"], paths.run_dir(run["id"]) / "tmp"):
            return {"verdict": "STALE", "summary": "worktree changed while checks ran", "results": results}
    verdict = "PASS" if not findings else "CHANGES_REQUIRED"
    parsed = review_parse.Parsed(verdict=verdict, findings=findings)
    return {"verdict": verdict, "parsed": parsed, "results": results, "route": "deterministic",
            "summary": f"{len(commands) - len(findings)}/{len(commands)} checks passed"}


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
                 exclude: list[str] | None = None, kind: str | None = None, resume_from: str | None = None) -> dict:
    """Route, launch, and parse one independent review, substituting routes on
    environment/adapter/schema failure up to the configured bound.

    `resume_from` names an earlier reviewer dispatch to continue (a plan-defect
    redirect with --reviewer same): its session is resumed when the harness can,
    else a fresh session runs on the same route."""
    limit = int((run.get("gates") or {}).get("environment_retry_max", 2))
    excluded = set(exclude or [])
    producer = _producer_route(con, gate)
    failures = []
    task = state.get_task(con, run["id"], gate["task_id"]) if gate.get("task_id") else None
    # A user-pinned code reviewer (dispatch --review-as) replaces routing; a
    # declared executor's family is excluded from routed review.
    pinned = (task or {}).get("review_override") if role == "code_reviewer" else None
    producer_model, producer_declared = _producer_model(con, gate)
    producer_family = candidates.model_family(producer_model)
    if producer_declared and producer_family and not pinned:
        excluded.add(f"family:{producer_family}")
    profile_kind = kind or ("vision" if visual else "reviewer")
    resume, same_route = (_reviewer_resume(con, run, resume_from, profile_kind, cwd) if resume_from
                          else (None, None))
    for attempt in range(limit + 1):
        if attempt == 0 and same_route:
            decision = candidates.route_role(con, state.pinned_config(run), run, role, task_id=gate.get("task_id"),
                                             exact=same_route)
            if decision.get("status") != "selected":
                resume = None
                decision = candidates.route_role(con, state.pinned_config(run), run, role,
                                                 task_id=gate.get("task_id"), exclude=excluded)
        elif pinned:
            decision = candidates.declared_decision(pinned["as"], flag="--review-as")
            decision["launch"] = {k: pinned[k] for k in ("cli", "external") if pinned.get(k)}
            if producer_family and candidates.model_family(decision["candidate"]["model_id"]) == producer_family:
                failures.append(f"--review-as {pinned['as']} is the same model family ({producer_family}) as the "
                                f"producer {producer_model}; not independent")
                break
        else:
            decision = candidates.route_role(con, state.pinned_config(run), run, role, task_id=gate.get("task_id"),
                                             exclude=excluded)
        if decision.get("status") != "selected":
            failures.append(f"no qualifying {role} route ({decision.get('status')})")
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
        parsed = review_parse.parse(_last_block(text), plan_review=plan_review, visual=visual)
        wrote_file = output.is_file() and output.stat().st_size > 0
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
            if attention:
                with db.transaction(con):
                    state.record_evidence(con, run["id"], "review_output", output if output.is_file() else None,
                                          task_id=gate.get("task_id"), revision_id=gate.get("revision_id"),
                                          gate_id=gate["id"], meta={"route": triple, "exit": d.get("exit_code")},
                                          digest=sha256_bytes(text.encode()))
                return {"verdict": "ATTENTION", "parsed": None, "route": triple, "dispatch_id": dispatch_id,
                        "summary": attention, "producer_route": producer}
            d = state.get_dispatch(con, dispatch_id)
        with db.transaction(con):
            state.record_evidence(con, run["id"], "review_output", output if output.is_file() else None,
                                  task_id=gate.get("task_id"), revision_id=gate.get("revision_id"), gate_id=gate["id"],
                                  meta={"route": triple, "exit": d.get("exit_code")},
                                  digest=sha256_bytes(text.encode()))
        if parsed.valid:
            # A valid reply file is the result, whatever the exit classification.
            # The reviewer is done: snapshot and close its pane (R1).
            dispatch_mod.reclaim_pane(run, dispatch_id)
            return {"verdict": parsed.verdict, "parsed": parsed, "route": triple, "dispatch_id": dispatch_id,
                    "summary": f"{parsed.verdict} by {triple}", "producer_route": producer}
        # Only a launch failure reaches here: the agent never produced a reply
        # (never started, died first, or hit an auth or quota wall). That is the
        # one case where another route is substituted.
        log_text = _log_text(d, ddir)
        reason = f"{triple}: exit {d.get('exit_code')} ({d.get('terminal_classification')}) with no reply"
        wall = "quota" if _quota_signature(log_text) else "auth" if _auth_signature(log_text) else None
        if wall:
            reason += f" [{wall}]"
        failures.append(reason)
        if pinned:
            break  # the user named this reviewer; never substitute another
        excluded.add(triple)
        if wall:
            excluded.add(f"harness:{cand['harness']}")
        with db.transaction(con):
            con.execute("UPDATE gates SET env_failures=env_failures+1 WHERE id=?", (gate["id"],))
            con.execute("UPDATE dispatches SET attribution='adapter', outcome='environment_failure' WHERE id=?", (dispatch_id,))
    return {"verdict": "UNAVAILABLE", "parsed": None, "route": None, "summary": "; ".join(failures)[:600]}


REPLY_FILE_RULE = ("Office reads your review only from that file; text you print in the terminal is not read.")


def _reply_text(d: dict, ddir: Path, output: Path) -> str:
    """The reviewer's reply: its reply file, or (headless only) the captured
    stdout log, which is also a file the harness wrote. Never pane text."""
    if output.is_file() and output.stat().st_size:
        return output.read_text(encoding="utf-8", errors="replace")
    if d.get("launcher") in ("herdr", "external"):
        return ""
    return _log_text(d, ddir)


def _log_text(d: dict, ddir: Path) -> str:
    log = Path(d.get("log_path") or ddir / "output.log")
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


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
    for n in range(1, limit + 1):
        if d.get("launcher") != "herdr" or not _agent_alive(name):
            break  # no live session to ask: the orchestrator decides (never a substitute)
        if output.is_file():
            output.replace(output.with_name(f"reply.invalid-{n}.txt"))
        prompt = (f"Office could not read your review ({'; '.join(errors[:4])}). Write your complete review again, "
                  f"in the format the brief requires (a VERDICT line first), to {output}. {REPLY_FILE_RULE}")
        try:
            subprocess.run(["herdr", "agent", "prompt", name, prompt], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            break
        with db.transaction(con):
            state.emit(con, run, "review.reprompt", f"{d.get('task_id') or 'plan'} {d['role']} {d['id']}: re-prompted "
                       f"{n}/{limit} ({'; '.join(errors[:2])})", audience="runtime", task_id=d.get("task_id"),
                       dispatch_id=d["id"])
        text = _await_file(output, wait_s, poll)
        parsed = review_parse.parse(_last_block(text), plan_review=plan_review, visual=visual)
        if parsed.valid:
            return text, parsed, None
        errors = parsed.errors or ["no reply file"]
    reason = (f"reviewer {d['id']} ({d.get('triple')}) left no valid reply file after re-prompting: "
              f"{'; '.join(errors[:3])}; its pane is kept. Re-prompt it (herdr agent prompt {name} ...) "
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


def _producer_model(con, gate: dict) -> tuple[str | None, bool]:
    """(model_id, declared) of the dispatch that produced the gate's revision."""
    if not gate.get("revision_id"):
        return None, False
    row = con.execute("SELECT d.route_json, d.override_json FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id "
                      "WHERE r.id=?", (gate["revision_id"],)).fetchone()
    if not row:
        return None, False
    cand = (json.loads(row["route_json"] or "{}").get("candidate") or {})
    declared = bool(json.loads(row["override_json"] or "{}").get("declared"))
    return cand.get("model_id"), declared


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
                 run["office_version"], "launching", cand["harness"], cand.get("invocation_model_id"), cand.get("effort"),
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
    while start > 0 and re.match(r"^(EVIDENCE_STATUS|FINDING|DEFECT|RESOLVED|CLEARED|RETRACT)",
                                 review_parse._clean(lines[start - 1]), re.I):
        start -= 1
    return "\n".join(lines[start:])


def _quota_signature(text: str) -> bool:
    return bool(re.search(r"rate.?limit|quota|usage limit|429|too many requests|exhausted", text[-2000:], re.I))


def _auth_signature(text: str) -> bool:
    """The harness is not signed in: every route on it will fail the same way."""
    return bool(re.search(r"not logged in|please run /login|authentication (failed|required)|unauthorized|\b401\b",
                          text[-2000:], re.I))


def job_review(con, run: dict, job: dict) -> dict:
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (job["payload"]["gate_id"],)).fetchone())
    if gate["status"] not in ("queued", "running"):
        return {"skipped": gate["status"]}
    task = state.get_task(con, run["id"], gate["task_id"])
    rev = dict(con.execute("SELECT * FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone())
    checkout = detached_checkout(run, rev["commit_sha"], f"review-{gate['id']}")
    try:
        diff = paths.git(Path(run["repo_root"]), "diff", rev["base_commit"], rev["commit_sha"])
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated; inspect the checkout for the rest]"
        checks = con.execute("SELECT summary, verdict FROM gates WHERE revision_id=? AND kind='checks' AND status='done'",
                             (rev["id"],)).fetchone()
        carried = [dict(r) for r in con.execute("SELECT code, severity, location, summary FROM findings WHERE run_id=? AND "
                                                "task_id=? AND gate_kind='code_review' AND state='open'",
                                                (run["id"], task["id"])).fetchall()]
        brief = briefs.code_review_brief(run, task, rev, diff, checks["summary"] if checks else "none declared",
                                         carried, str(checkout))
        exclude = [job["payload"]["exclude_route"]] if job["payload"].get("exclude_route") else None
        outcome = run_reviewer(con, run, gate, "code_reviewer", brief, cwd=checkout, include_dirs=[checkout],
                               exclude=exclude)
    finally:
        remove_checkout(run, checkout)
    with db.transaction(con):
        ingest_task_gate(con, state.get_run(con, run["id"]), gate["id"], outcome)
    return {"verdict": outcome["verdict"]}


def detached_checkout(run: dict, commit: str, name: str) -> Path:
    path = paths.run_dir(run["id"]) / "checkouts" / name
    if path.exists():
        remove_checkout(run, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.git(Path(run["repo_root"]), "worktree", "add", "--detach", str(path), commit)
    return path


def remove_checkout(run: dict, path: Path) -> None:
    subprocess.run(["git", "-C", run["repo_root"], "worktree", "remove", "--force", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------------ ingest

def _fingerprint(f: dict) -> str:
    words = re.findall(r"[a-z0-9_./]+", ((f.get("location") or "") + " " + (f.get("summary") or "")).lower())
    return sha256_obj(sorted(set(w for w in words if len(w) > 2))[:40])


def ingest_task_gate(con, run: dict, gate_id: str, outcome: dict) -> None:
    """Record one task-gate result and re-evaluate the task. Caller holds tx."""
    gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone())
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
    if parsed:
        for code in parsed.resolved:
            _set_state(con, run, task, gate["kind"], code, "resolved")
        for r in parsed.retracted:
            _set_state(con, run, task, gate["kind"], r["code"], "retracted")
        seen = set()
        for f in parsed.findings:
            seen.add(f["code"])
            _upsert_finding(con, run, task, gate, f, outcome)
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
        state.emit(con, run, "gate.unavailable", f"{task['id']} {kind_label} UNAVAILABLE: {outcome.get('summary', '')[:160]}; "
                   "valid unrelated results are preserved", task_id=task["id"])
        if gate["kind"] == "checks":
            con.execute("UPDATE gates SET status='cancelled', stale_reason='checks unavailable' WHERE revision_id=? "
                        "AND status='waiting'", (gate["revision_id"],))
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"{kind_label} gate unavailable: {outcome.get('summary', '')[:160]}")
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
    new_state = "open" if f["severity"] == "material" else "minor"
    if existing:
        con.execute("UPDATE findings SET summary=?, location=?, action=?, fingerprint=?, gate_id=?, revision_id=?, "
                    "measurement_json=?, updated_at=?, state=? WHERE id=?",
                    (f["summary"], f.get("location"), f.get("action"), fp, gate["id"], gate["revision_id"],
                     dumps(f.get("measurement")) if f.get("measurement") else None, now_iso(), new_state, existing["id"]))
        return
    fid = "F" + uuid.uuid4().hex[:10]
    reviewer = outcome.get("dispatch_id")
    producer = con.execute("SELECT dispatch_id FROM revisions WHERE id=?", (gate["revision_id"],)).fetchone()
    con.execute("INSERT INTO findings(id, dispatch_id, reviewer_dispatch_id, status, severity, summary, evidence_hash, created_at, "
                "run_id, task_id, gate_id, revision_id, gate_kind, code, fingerprint, location, category, action, "
                "measurement_json, state, origin_gate_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, producer["dispatch_id"] if producer else None, reviewer,
                 "accepted-material" if f["severity"] == "material" else "minor", f["severity"], f["summary"],
                 sha256_obj(f), now_iso(), run["id"], task["id"], gate["id"], gate["revision_id"], gate["kind"], f["code"], fp,
                 f.get("location"), "carried" if carried else gate["kind"], f.get("action"),
                 dumps(f.get("measurement")) if f.get("measurement") else None, new_state, gate["id"], now_iso()))


def _set_state(con, run, task, kind, code, new_state):
    con.execute("UPDATE findings SET state=?, updated_at=? WHERE run_id=? AND task_id=? AND gate_kind=? AND code=? AND state='open'",
                (new_state, now_iso(), run["id"], task["id"], kind, code))


def _converge(con, run: dict, task: dict, gate: dict, outcome: dict) -> None:
    """Bounded convergence: fix round, or no-progress / budget -> one
    escalation -> pause with work preserved."""
    g = run.get("gates") or {}
    maximum = {"code_review": g.get("code_review_max_rounds"), "visual": g.get("visual_review_max_rounds"),
               "checks": g.get("code_review_max_rounds")}.get(gate["kind"]) or 2
    repeats = int((state.pinned_config(run).get("verification") or {}).get("no_progress_repeats", 2))
    no_progress = _no_progress(con, run, task, gate, repeats)
    budget_spent = gate["round"] >= int(maximum)
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
    passing = [r["kind"] for r in con.execute("SELECT kind FROM gates WHERE revision_id=? AND verdict='PASS' AND status='done'",
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
    waived = authority.waived(con, run["id"], task_id)
    gates_now = [g for g in required_gates(con, run, task, rev_id) if g["kind"] not in waived]
    basis = "all required gates PASS"
    if not gates_now and not waived:
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
    for dfct in plans.open_defects(con, run["id"]):
        ids = plans._task_ids_in(dfct.get("location") or "")
        if not ids or task_id in ids:
            return False
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep)
        if not dt or dt["status"] != "accepted":
            return False
        dep_rev = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (dt["accepted_revision_id"],)).fetchone()
        if dep_rev and not _is_ancestor(run, dep_rev["commit_sha"], rev["commit_sha"]):
            return False
    state.update_task(con, run["id"], task_id, status="accepted", accepted_revision_id=rev_id, pause_reason=None)
    con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND task_id=? AND released_at IS NULL AND revoked_at IS NULL",
                (now_iso(), run["id"], task_id))
    con.execute("UPDATE dispatches SET outcome='pending' WHERE id=? AND outcome IS NULL", (rev["dispatch_id"],))
    state.emit(con, run, "task.accepted", f"{task_id} accepted on {rev_id}"
               + (f" ({basis})" if basis != "all required gates PASS" else "")
               + (f" with user waiver of {', '.join(sorted(waived))}" if waived else ""), task_id=task_id,
               payload={"basis": basis, "waived": sorted(waived)})
    dispatch_mod.start_stacked(con, run, task_id)
    for other in state.tasks(con, run["id"]):
        if task_id in other["depends"] and other["status"] == "submitted":
            evaluate_acceptance(con, run, other["id"])
    from office import integration
    integration.maybe_queue(con, run)
    return True


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

def close_blockers(con, run: dict) -> list[str]:
    from office import plans
    out = []
    for t in state.tasks(con, run["id"]):
        if t["status"] not in ("accepted", "cancelled"):
            out.append(f"{t['id']} is {t['status']}")
    if con.execute("SELECT 1 FROM deliveries WHERE run_id=? AND status IN ('queued','delivered')", (run["id"],)).fetchone():
        out.append("an amendment is delivered but not applied")
    if con.execute("SELECT 1 FROM gates WHERE run_id=? AND status IN ('queued','running','waiting')", (run["id"],)).fetchone():
        out.append("reviews are still running")
    if plans.open_defects(con, run["id"]):
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
    repo = Path(run["repo_root"])
    for target in ("origin/main", "main", "origin/master", "master"):
        proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", target], capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        if _is_ancestor(run, commit, proc.stdout.strip()):
            return {"status": "landed", "detail": f"{commit[:12]} is in {target}", "commit": commit, "target": target}
        return {"status": "pending", "detail": f"{commit[:12]} is not in {target}; open a PR or pass --handoff <ref>",
                "commit": commit, "target": target}
    return {"status": "pending", "detail": "no default branch found; pass --handoff <ref>", "commit": commit}
