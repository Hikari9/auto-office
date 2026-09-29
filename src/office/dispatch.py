"""Strategy-owned dispatch with runtime-owned mechanics.

The orchestrator says which tasks run and whether in parallel. The runtime
routes each role, checks dependencies and scope ownership, takes a fenced lease,
creates the worktree, builds the packet (with the run-pinned office_version),
injects the environment, and launches through a durable outbox job.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office import adapters, briefs, candidates, db, frontdoor, jobs, paths, planfile, routing, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_json, dumps, now_iso, pid_alive, sha256_obj, short

LEASE_TTL_SECONDS = 4 * 3600
IDENTITY_ENV = ("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE", "OFFICE_STATE_DIR",
                "OFFICE_SESSION", "OFFICE_HARNESS", "OFFICE_VERSION", "OFFICE_FRONT_DOOR_HOPS")
PLANNER_TASK = "P1"


# ------------------------------------------------------------------ planner task

def create_planner_task(con, run: dict, *, contract_request: str | None = None, decision: dict | None = None) -> str:
    """Queue the dedicated planner. Caller holds the transaction."""
    now = now_iso()
    existing = state.get_task(con, run["id"], PLANNER_TASK)
    if existing is None:
        con.execute(
            "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
            "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], PLANNER_TASK, "Plan the run", "planner", dumps([".office/PLAN.md"]), dumps([]), dumps([]),
             dumps(["a plan that satisfies the frozen requirements"]), dumps([]), None, "queued", 0, 0, 0, now, now))
    else:
        state.update_task(con, run["id"], PLANNER_TASK, status="queued", pause_reason=None)
    return request_launch(con, run, PLANNER_TASK, role="planner", decision=decision,
                          extra={"contract_request": contract_request})


# ------------------------------------------------------------------ dispatch command

def dispatch(con, run: dict, task_ids: list[str], *, parallel: bool = False, route: str | None = None) -> Result:
    if not task_ids:
        raise Usage("no-task", "name at least one task", next_step="office dispatch T1 [T2 ...] [--parallel]")
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}")
    from office import guide, plans
    plans.require_dispatchable(con, run)
    # Route before the write transaction: routing reads evidence and probes quota.
    routes = {}
    for tid in task_ids:
        task = state.get_task(con, run["id"], tid)
        if task is None or task["role"] == "planner":
            raise Usage("unknown-task", f"{tid} is not a task in plan p{run['plan_version']}",
                        next_step="office status lists the ready tasks")
        routes[tid] = candidates.route_role(con, state.pinned_config(run), run, "executor",
                                            task_id=tid, override=route)
    res = Result()
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        graph = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
        previous = None
        for tid in task_ids:
            task = state.get_task(con, run["id"], tid)
            if task["status"] in ("running", "launching", "submitted", "changes_required", "queued"):
                res.add(f"{tid} already {task['status']}")
                previous = tid
                continue
            if task["status"] == "accepted":
                res.add(f"{tid} already accepted")
                previous = tid
                continue
            plans.require_scope_clear(con, run, tid)
            stack_after = None if parallel or previous is None else previous
            base = _base_for(con, run, task, graph, stack_after)
            decision = routes[tid]
            if decision.get("status") != "selected":
                raise Refused("no-route", _route_failure(tid, decision), scope=tid,
                              preserved="plan and other dispatches", next_step=_route_next(decision, tid))
            _record_routing(con, run, decision)
            if stack_after:
                state.update_task(con, run["id"], tid, status="queued", stack_after=stack_after,
                                  pause_reason=f"stacked after {stack_after}")
                con.execute("INSERT INTO artifact_versions(id, run_id, kind, version, content_hash, created_at) "
                            "VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], "stack", 0,
                                                     sha256_obj({"task": tid, "after": stack_after, "route": decision["selected"]}), now_iso()))
                _stash_route(con, run, tid, decision)
                res.add(f"{tid} stacked after {stack_after}")
            else:
                did = request_launch(con, run, tid, role="executor", decision=decision, base=base)
                res.add(f"{tid} -> {did} executor/{decision['selection_disclosure']['triple']} launching")
            previous = tid
        if run["phase"] == "planning":
            state.update_run(con, run["id"], phase="executing")
        state.emit(con, run, "dispatch", f"dispatched {' '.join(task_ids)}{' in parallel' if parallel else ''}",
                   payload={"tasks": task_ids, "parallel": parallel})
    jobs.kick(con, run["id"])
    res.next = "exceptions only; office status"
    return res


def _route_failure(tid: str, decision: dict) -> str:
    status = decision.get("status")
    rejected = decision.get("rejected") or []
    top = "; ".join(f"{r['candidate']}: {r['reason']}" for r in rejected[:3])
    skipped = "; ".join(f"{s['candidate']}: {s['reason']}" for s in (decision.get("skipped") or [])[:2])
    return f"{tid}: no qualifying executor route ({status}){': ' + top if top else ''}{' | ' + skipped if skipped else ''}"


def _route_next(decision: dict, tid: str) -> str:
    for r in decision.get("rejected") or []:
        if r.get("stage") == 2:
            return (f"a user may promote a route: office approve trust {r['candidate']} --quote \"<user's words>\"; "
                    "or office inspect route for details")
    if decision.get("status") == "protected_quota_would_be_consumed":
        return "wait for quota, choose a cheaper strategy, or obtain explicit user authority"
    return f"office inspect route {tid}"


def _record_routing(con, run: dict, decision: dict) -> None:
    req = decision.get("request") or {}
    con.execute("INSERT INTO routing_decisions(id, run_id, role, request_hash, selected_triple, decision_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run["id"], req.get("role"), sha256_obj({k: v for k, v in req.items() if k != "candidates"}),
                 decision.get("selected"), decision.get("decision_hash"), now_iso()))


def _stash_route(con, run, tid, decision):
    state.update_task(con, run["id"], tid, route_json=dumps({"candidate": decision.get("candidate"),
                                                          "selection_disclosure": decision.get("selection_disclosure")}))


def _base_for(con, run: dict, task: dict, graph: dict, stack_after: str | None) -> str:
    """Base commit: run base, or the dependency revision this task builds on."""
    deps = list(task["depends"]) + ([stack_after] if stack_after else [])
    base = run["base_sha"]
    for dep in deps:
        d = state.get_task(con, run["id"], dep)
        rev_id = (d or {}).get("accepted_revision_id") or (d or {}).get("current_revision_id")
        if not rev_id:
            if dep == stack_after:
                continue
            raise Refused("dependency-not-ready", f"{task['id']} depends on {dep}, which has no submitted revision",
                          scope=task["id"], next_step=f"dispatch {dep} first, or office dispatch {dep} {task['id']} (stacked)")
        rev = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone()
        base = rev["commit_sha"]
    return base


# ------------------------------------------------------------------ launch request

def request_launch(con, run: dict, task_id: str, *, role: str, decision: dict | None = None,
                   base: str | None = None, extra: dict | None = None, fix_of: str | None = None) -> str:
    """Create the dispatch row, lease and launch job. Caller holds the tx.

    Routing for executors happens before the transaction; planner and fix
    rounds route here from pinned state because they have no orchestrator turn.
    """
    task = state.get_task(con, run["id"], task_id)
    prior = None
    if task.get("current_dispatch_id"):
        prior = state.get_dispatch(con, task["current_dispatch_id"])
    if decision is None:
        if prior and prior.get("route"):
            decision = {"status": "selected", "candidate": prior["route"].get("candidate"),
                        "selection_disclosure": prior["route"].get("selection_disclosure"),
                        "selected": prior.get("triple")}
        elif task.get("route_json"):
            s = json.loads(task["route_json"])
            decision = {"status": "selected", "candidate": s["candidate"], "selection_disclosure": s["selection_disclosure"],
                        "selected": routing.candidate_id(s["candidate"])}
    if decision is None:
        raise Refused("no-route", f"{task_id}: no route recorded for relaunch", scope=task_id)
    cand = decision["candidate"]
    dispatch_id = "D" + uuid.uuid4().hex[:8]
    lease = acquire_lease(con, run, task, dispatch_id, role)
    worktree = prior["worktree"] if prior and prior.get("worktree") else str(paths.worktrees_dir() / run["id"][:8] / task_id)
    branch = prior["branch"] if prior and prior.get("branch") else f"office/{run['id'][:8]}/{task_id}"
    applied = task["contract_version"] if role != "planner" else run["plan_version"]
    con.execute(
        "INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, selection_reason, task_shape, "
        "started_at, task_id, kind, office_version, status, worktree, branch, base_commit, lease_id, harness, model, effort, "
        "adapter_id, applied_plan_version, route_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dispatch_id, run["id"], role, dispatch_id, routing.candidate_id(cand), cand.get("invocation_model_id"),
         (decision.get("selection_disclosure") or {}).get("reason"), run.get("playbook"), now_iso(), task_id, role,
         run["office_version"], "launching", worktree, branch, base or (prior or {}).get("base_commit") or run["base_sha"],
         lease["id"], cand["harness"], cand.get("invocation_model_id"), cand.get("effort"), cand.get("adapter_id"),
         applied, dumps({"candidate": cand, "selection_disclosure": decision.get("selection_disclosure")})))
    state.update_task(con, run["id"], task_id, status="launching", current_dispatch_id=dispatch_id,
                      pause_reason=None, stack_after=None)
    payload = {"dispatch_id": dispatch_id, "task_id": task_id, "role": role, "fix_of": fix_of, **(extra or {})}
    state.enqueue(con, run, "launch_agent", payload, dedup_key=f"launch:{dispatch_id}", max_attempts=2)
    return dispatch_id


def acquire_lease(con, run: dict, task: dict, holder: str, role: str) -> dict:
    """One fenced lease per task scope. Overlapping live scopes are refused."""
    now = datetime.now(timezone.utc)
    rows = con.execute("SELECT * FROM leases WHERE run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                       (run["id"],)).fetchall()
    for row in rows:
        if row["task_id"] == task["id"]:
            # Handing the task's lease to a new holder (fix round, relaunch):
            # revoke the old one so a late submit from it is fenced out.
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='superseded' WHERE id=?", (now.isoformat(), row["id"]))
            continue
        other = state.get_task(con, run["id"], row["task_id"]) if row["task_id"] else None
        other_scope = other["scope"] if other else json.loads(row["scope"] or "[]")
        if planfile.scopes_overlap(task["scope"], other_scope):
            raise Refused("scope-held", f"{task['id']} scope overlaps {row['task_id']}, which holds a live lease",
                          scope=task["id"], preserved="both tasks' work",
                          next_step=f"wait for {row['task_id']} to be accepted, or office dispatch {row['task_id']} {task['id']} (stacked)")
    fencing = (con.execute("SELECT COALESCE(MAX(fencing),0) FROM leases WHERE run_id=?", (run["id"],)).fetchone()[0] or 0) + 1
    lease_id = "L" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, task_id, fencing, dispatch_id, renewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lease_id, run["id"], role, dumps(task["scope"]), holder, now.isoformat(),
                 (now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(), task["id"], fencing, holder, now.isoformat()))
    con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], role, task["id"], None, holder, "acquire", now.isoformat()))
    return {"id": lease_id, "fencing": fencing}


def live_lease(con, run_id: str, lease_id: str) -> dict | None:
    row = con.execute("SELECT * FROM leases WHERE id=? AND run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                      (lease_id, run_id)).fetchone()
    return dict(row) if row else None


def renew_lease(con, lease_id: str) -> None:
    now = datetime.now(timezone.utc)
    con.execute("UPDATE leases SET expires_at=?, renewed_at=? WHERE id=?",
                ((now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(), now.isoformat(), lease_id))


def revoke(con, run: dict, task_id: str, reason: str) -> Result:
    with db.transaction(con):
        task = state.get_task(con, run["id"], task_id)
        if task is None:
            raise Usage("unknown-task", f"no task {task_id}")
        con.execute("UPDATE leases SET revoked_at=?, revoke_reason=? WHERE run_id=? AND task_id=? AND released_at IS NULL "
                    "AND revoked_at IS NULL", (now_iso(), reason, run["id"], task_id))
        state.update_task(con, run["id"], task_id, status="paused", pause_reason=f"lease revoked: {reason}")
        state.emit(con, run, "lease.revoked", f"{task_id} lease revoked", task_id=task_id)
    live = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE run_id=? AND task_id=? AND ended_at IS NULL "
                                         "AND status IN ('launching', 'running')", (run["id"], task_id)).fetchall()]
    stopped = [d["id"] for d in live if stop_dispatch(run, d)]
    lines = [f"{task_id} lease revoked | later submits from its holder are rejected"]
    if stopped:
        lines.append(f"stopped {', '.join(stopped)} (SIGTERM)")
    return Result(lines=lines, next=f"office dispatch {task_id} to relaunch")


def _agent_pgid_file(run: dict, dispatch_id: str) -> Path:
    return paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "agent.pgid"


def _killpg(pid, sig=signal.SIGTERM) -> bool:
    if not pid or pid <= 0 or pid == os.getpid() or pid == os.getpgrp():
        return False
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            return False
    return True


def stop_dispatch(run: dict, d: dict, *, wait: float = 5.0) -> bool:
    """SIGTERM a live dispatch's supervisor and agent process groups, close its
    herdr pane, and make sure the end is recorded as `signal`."""
    if d.get("launcher") in (None, "external", "sync"):
        return False  # not under Office's process control
    sup = d.get("pid") if pid_alive(d.get("pid")) else None
    pgid_file = _agent_pgid_file(run, d["id"])
    agent = None
    if pgid_file.is_file():
        try:
            agent = int(pgid_file.read_text().strip())
        except ValueError:
            agent = None
    signalled = _killpg(sup)
    signalled = _killpg(agent) or signalled
    deadline = time.time() + wait
    while sup and pid_alive(sup) and time.time() < deadline:
        time.sleep(0.05)  # let the supervisor record its own `signal` end
    if d.get("launcher") == "herdr" and d.get("pane_id") and shutil.which("herdr"):
        subprocess.run(["herdr", "pane", "close", d["pane_id"]], capture_output=True, timeout=30)
        signalled = True
    con = db.connect()
    try:
        ended = state.get_dispatch(con, d["id"]).get("ended_at")
    finally:
        con.close()
    if not ended:
        # The supervisor was already gone or did not get to record the end.
        _finish(d["id"], None, int(signal.SIGTERM), "signal", 0.0)
    return signalled or not ended


# ------------------------------------------------------------------ launch job

def ensure_worktree(run: dict, dispatch: dict) -> Path:
    wt = Path(dispatch["worktree"])
    repo = Path(run["repo_root"])
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    branches = paths.git(repo, "branch", "--list", dispatch["branch"])
    if branches.strip():
        paths.git(repo, "worktree", "add", str(wt), dispatch["branch"])
    else:
        paths.git(repo, "worktree", "add", "-b", dispatch["branch"], str(wt), dispatch["base_commit"])
    exclude = Path(paths.git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if ".office/" not in text.split():
            with exclude.open("a") as fh:
                fh.write("\n.office/\n")
    except OSError:
        pass
    return wt


def build_packet(con, run: dict, dispatch: dict, role: str, extra: dict) -> dict:
    task = state.get_task(con, run["id"], dispatch["task_id"])
    plan = state.current_plan(con, run["id"])
    req = state.current_requirements(con, run["id"])
    body = {
        "role": role,
        "dispatch_id": dispatch["id"],
        "task_id": task["id"],
        "title": task["title"],
        "scope": task["scope"],
        "depends": task["depends"],
        "accept": task["accept"],
        "checks": task["checks"],
        "visual": task["visual"],
        "requirements_version": run["requirements_version"],
        "plan_version": plan["version"] if plan else 0,
        "contract_version": task["contract_version"],
        "lease_id": dispatch["lease_id"],
        "base_commit": dispatch["base_commit"],
        "worktree": dispatch["worktree"],
        "route": dispatch["route"].get("selection_disclosure"),
        "requirements": req["frozen"],
        "fix_of": extra.get("fix_of"),
        "contract_request": extra.get("contract_request"),
    }
    return state.packet_envelope(run, f"{role}-dispatch", body)


def job_launch_agent(con, run: dict, job: dict) -> dict:
    payload = job["payload"]
    dispatch = state.get_dispatch(con, payload["dispatch_id"])
    if dispatch["status"] not in ("launching",):
        return {"skipped": dispatch["status"]}
    wt = ensure_worktree(run, dispatch)
    role = payload["role"]
    packet = build_packet(con, run, dispatch, role, payload)
    state.check_packet(run, packet)
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ddir / "packet.json", packet)
    brief = briefs.worker_brief(con, run, packet)
    (ddir / "brief.md").write_text(brief, encoding="utf-8")
    with db.transaction(con):
        con.execute("UPDATE dispatches SET packet_hash=?, packet_path=?, log_path=? WHERE id=?",
                    (packet["packet_hash"], str(ddir / "packet.json"), str(ddir / "output.log"), dispatch["id"]))
    launcher = launch(run, dispatch, "worker", ddir, cwd=wt)
    return {"dispatch_id": dispatch["id"], **launcher}


def worker_env(run: dict, dispatch: dict, role: str) -> dict:
    env = dict(os.environ)
    # Identity is always re-injected for this dispatch; configuration switches
    # (data homes, job/launcher modes, quota fixtures) carry through unchanged.
    for key in IDENTITY_ENV:
        env.pop(key, None)
    env.update({
        "OFFICE_RUN_ID": run["id"],
        "OFFICE_TASK_ID": dispatch["task_id"] or "",
        "OFFICE_DISPATCH_ID": dispatch["id"],
        "OFFICE_ROLE": role,
        "OFFICE_VERSION": run["office_version"],
    })
    argv, extra = frontdoor.current_argv()
    env.update(extra)
    shim = paths.data_home() / "bin"
    if (shim / "office").exists():
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    return env


def launch(run: dict, dispatch: dict, kind: str, ddir: Path, *, cwd: Path, wait: bool = False,
           output: Path | None = None, images: list[Path] | None = None, include_dirs: list[Path] | None = None,
           prompt_file: Path | None = None) -> dict:
    """Start `office _supervise` for a dispatch, in a Herdr pane when running
    inside Herdr (visible delegation), else as a detached process."""
    spec = {"dispatch_id": dispatch["id"], "kind": kind, "cwd": str(cwd), "output": str(output) if output else None,
            "images": [str(i) for i in images or []], "include_dirs": [str(d) for d in include_dirs or []],
            "prompt_file": str(prompt_file or ddir / "brief.md"), "log_path": str(ddir / "output.log")}
    # The supervisor always finds its spec here, whatever directory holds the brief.
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET log_path=COALESCE(log_path, ?) WHERE id=?", (spec["log_path"], dispatch["id"]))
    finally:
        con.close()
    argv, extra = frontdoor.current_argv()
    sup = argv + ["_supervise", dispatch["id"]]
    env = dict(os.environ)
    env.update(extra)
    env.pop(frontdoor.HOP_ENV, None)
    launcher = os.environ.get("OFFICE_LAUNCHER", "auto")
    if kind == "worker" and os.environ.get("OFFICE_WORKER_LAUNCHER") == "external":
        # A worker hosted outside Office's process control (an interactive
        # session the user or a test drives). It is live until it says otherwise.
        write_agent_env(run, dispatch, ddir)
        _record_launch(run, dispatch["id"], launcher="external")
        return {"launcher": "external"}
    if launcher == "sync":
        # Deterministic mode for tests and fixtures: supervise in the foreground.
        _record_launch(run, dispatch["id"], launcher="sync", pid=os.getpid())
        subprocess.run(sup, cwd=str(cwd), stdin=subprocess.DEVNULL, env=env)
        return _wait_terminal(dispatch["id"], timeout=5) if wait else {"launcher": "sync"}
    headless = "process"
    use_herdr = (launcher in ("auto", "herdr") and os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr"))
    if use_herdr:
        inter = _interactive(dispatch, kind, cwd, include_dirs)
        pane = _herdr_pane(run, cwd, label=f"office {dispatch.get('role') or kind} {dispatch['id']}") if inter else None
        if inter and not pane:
            _launch_notice(run, dispatch, "no herdr pane could be opened; running headless instead")
        if pane:
            started = _herdr_agent_start(run, dispatch, spec, env, inter, pane, cwd, ddir)
            if started:
                if wait:
                    return _wait_terminal(dispatch["id"])
                return started
            # The agent never came up in the pane: fall back to a plain process,
            # and keep that visible on the dispatch (its notice says why).
            headless = "process-fallback"
    log = open(ddir / "supervisor.log", "ab")
    try:
        proc = subprocess.Popen(sup, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    finally:
        log.close()
    _record_launch(run, dispatch["id"], launcher=headless, pid=proc.pid)
    if wait:
        proc.wait()
        return _wait_terminal(dispatch["id"])
    return {"launcher": headless, "pid": proc.pid}


def _interactive(dispatch: dict, kind: str, cwd: Path, include_dirs: list[Path] | None = None) -> tuple[list[str], str] | None:
    """The pane-hosted form of this dispatch's harness, or None (headless)."""
    adapter = adapters.load_all().get(dispatch.get("adapter_id") or "")
    if not adapter or not dispatch.get("model"):
        return None
    return adapters.interactive_argv(adapter, kind, model=dispatch["model"], effort=dispatch.get("effort") or "none",
                                     cwd=cwd, include_dirs=include_dirs)


def write_agent_env(run: dict, dispatch: dict, ddir: Path, *, worker: bool = True) -> Path:
    """agent.env: the dispatch identity a pane-hosted agent sources before it
    starts, whether Office or the user launches it."""
    wenv = worker_env(run, dispatch, dispatch.get("role") if worker else "reviewer")
    if not worker:
        # Reviewers get no Office authority: they cannot submit or ack.
        for k in ("OFFICE_TASK_ID", "OFFICE_DISPATCH_ID"):
            wenv.pop(k, None)
    carry = {k: v for k, v in wenv.items() if k.startswith(("OFFICE_", "AUTO_OFFICE_", "XDG_")) or k in ("PYTHONPATH", "PATH")}
    env_file = ddir / "agent.env"
    env_file.write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(carry.items())))
    env_file.chmod(0o600)
    return env_file


def _herdr_agent_start(run: dict, dispatch: dict, spec: dict, env: dict, inter: tuple[list[str], str], pane: str,
                       cwd: Path, ddir: Path) -> dict | None:
    """Start the real harness in the pane with `herdr agent start`, hand it a
    one-line brief pointer, and leave a detached watcher to record the end.
    The pane runs the agent itself, never a shell wrapper around it."""
    args, herdr_kind = inter
    name = herdr_agent_name(dispatch["id"])
    worker = spec["kind"] == "worker"
    # The pane's shell does not inherit this process's environment: source the
    # dispatch identity into it first, so the agent's own `office submit` works.
    env_file = write_agent_env(run, dispatch, ddir, worker=worker)
    subprocess.run(["herdr", "pane", "run", pane, f". {shlex.quote(str(env_file))} && cd {shlex.quote(str(cwd))}"],
                   capture_output=True, timeout=30)
    try:
        proc = subprocess.run(["herdr", "agent", "start", name, "--kind", herdr_kind, "--pane", pane, "--", *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        _launch_notice(run, dispatch, f"herdr agent start failed ({exc}); running headless instead")
        return None
    if proc.returncode != 0:
        why = (proc.stdout or proc.stderr or "").strip()[:200]
        _launch_notice(run, dispatch, f"herdr agent start failed ({why}); running headless instead")
        return None
    spec.update({"herdr_agent": name, "pane": pane})
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    _record_launch(run, dispatch["id"], launcher="herdr", pane_id=pane)
    _pane_ledger(run, dispatch, pane)
    # A long brief pasted as the prompt does not land; a one-line pointer does.
    if worker:
        pointer = (f"Read and carry out the brief at {spec['prompt_file']} exactly. "
                   "When the work and its checks are complete, run: office submit")
    else:
        images = f" Inspect each evidence image: {' '.join(spec['images'])}." if spec.get("images") else ""
        pointer = (f"Read and carry out the review brief at {spec['prompt_file']} exactly.{images} "
                   f"Write your complete review to {spec['output']}. If your tools cannot write files, "
                   "end your reply with the complete review instead. Do not edit anything else.")
    landed = _deliver_prompt(name, pane, pointer)
    if not landed:
        # The agent is up in a pane the user can see; a second headless copy
        # would race it. Say so and leave the pane for a manual re-prompt.
        _launch_notice(run, dispatch, f"brief pointer did not land in herdr agent {name} (pane {pane}); "
                                      f"re-prompt it: herdr agent prompt {name} {shlex.quote(pointer)}")
    spec["prompt_landed"] = landed
    atomic_write_json(paths.run_dir(run["id"]) / "dispatches" / dispatch["id"] / "launch.json", spec)
    log = open(ddir / "supervisor.log", "ab")
    try:
        watcher = subprocess.Popen(frontdoor.current_argv()[0] + ["_supervise", dispatch["id"]], cwd=str(cwd),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                   start_new_session=True, close_fds=True)
    finally:
        log.close()
    return {"launcher": "herdr", "pane": pane, "agent": name, "watcher_pid": watcher.pid, "prompt_landed": landed}


def _herdr_agent_sample(name: str) -> dict | None:
    """One `herdr agent get` sample: {"status", "content_hash"}, or None when
    the agent is gone (its process exited)."""
    try:
        proc = subprocess.run(["herdr", "agent", "get", name], capture_output=True, text=True, timeout=30)
        res = json.loads(proc.stdout or "{}").get("result") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        # Unreadable is not idle: busy stays unknown (None), which never settles.
        return {"status": "unknown", "content_hash": None, "busy": None}
    agent = res.get("agent") or res
    if proc.returncode != 0 or not agent:
        return None
    read = _herdr_agent_text(name)
    return {"status": agent.get("status") or agent.get("agent_status"), "content_hash": sha256_obj(read),
            "busy": None if read is None else _pane_busy(read)}


def _herdr_agent_text(name: str, *extra: str) -> str | None:
    """Pane text, or None when it could not be read."""
    try:
        proc = subprocess.run(["herdr", "agent", "read", name, *extra], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


# Footer text a harness shows only while a turn is running. agy's reported
# status is not a liveness signal (it reads idle mid-turn); its pane is.
BUSY_MARKERS = ("esc to cancel", "esc to interrupt")


def _pane_busy(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in BUSY_MARKERS)


def herdr_agent_name(dispatch_id: str) -> str:
    """Herdr agent names must match [a-z][a-z0-9_-]{0,31}; dispatch ids start
    with an uppercase D, which herdr rejects."""
    return re.sub(r"[^a-z0-9_-]", "-", f"office-{dispatch_id}".lower())[:32]


def _prompt_landed(name: str, timeout: float) -> bool:
    """A prompt has landed once the pane shows a busy footer. `agent prompt`
    returning without error proves nothing, and neither does a `working`
    status: codex reports it while a startup trust dialog holds the composer
    empty, and agy reads `idle` mid-turn."""
    deadline = time.time() + timeout
    while True:
        if _pane_busy(_herdr_agent_text(name, "--source", "visible", "--lines", "15")):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1)


def _deliver_prompt(name: str, pane: str, pointer: str) -> bool:
    """Send the one-line pointer and confirm it landed; retry once by typing it
    into the pane and pressing Enter (agy can drop a prompt sent right after
    `agent start` returns)."""
    timeout = float(os.environ.get("OFFICE_HERDR_LAND_TIMEOUT", "30"))

    def herdr(*args: str) -> None:
        # The agent is already recorded as launched: a hung or missing herdr
        # here must not escape before the watcher starts.
        try:
            subprocess.run(["herdr", *args], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    herdr("agent", "prompt", name, pointer)
    if _prompt_landed(name, timeout):
        return True
    herdr("pane", "send-text", pane, pointer)
    herdr("pane", "send-keys", pane, "Enter")
    return _prompt_landed(name, timeout)


def _launch_notice(run: dict, dispatch: dict, text: str) -> None:
    """Record a launch problem where `office status` shows it."""
    con = db.connect()
    try:
        with db.transaction(con):
            state.emit(con, run, "launch", f"{dispatch.get('task_id') or dispatch['id']}: {text}",
                       task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"])
    finally:
        con.close()


def _submitted(con, dispatch: dict) -> bool:
    return bool(con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (dispatch["id"],)).fetchone()
                or con.execute("SELECT 1 FROM plans WHERE run_id=? AND created_by=?",
                               (dispatch["run_id"], dispatch["id"])).fetchone())


def watch_herdr_agent(dispatch_id: str, spec: dict, *, poll: float | None = None, stable_samples: int = 3) -> tuple:
    """(exit_code, classification) for a pane-hosted agent. A submit or the
    output file ends the dispatch. A single `done`/`idle` sample is never
    trusted: only `stable_samples` consecutive settled samples with unchanged
    pane content end it. The agent disappearing (process exit) ends it."""
    poll = float(os.environ.get("OFFICE_HERDR_POLL", "5")) if poll is None else poll
    output = Path(spec["output"]) if spec.get("output") else None
    history: list[dict] = []
    last_size = None
    while True:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
            if d["status"] in ("exited", "failed", "cancelled"):
                # Already recorded (cancelled, or ended by another path): not ours to finish.
                return None, "cancelled" if d["status"] == "cancelled" else "duplicate_ignored"
            if _submitted(con, d):
                return 0, "success"
        finally:
            con.close()
        size = output.stat().st_size if output and output.is_file() else 0
        sample = _herdr_agent_sample(spec["herdr_agent"])
        if size and size == last_size and (sample is None or sample.get("busy") is False):
            # Complete: written, no longer growing between two polls, and the
            # agent is not still mid-turn (it may rewrite the file).
            return 0, "success"
        last_size = size or None
        if sample is None:
            return None, "nonzero"
        history.append(sample)
        window = history[-stable_samples:]
        if (len(window) == stable_samples and all(w["status"] in ("done", "idle") for w in window)
                and all(w.get("busy") is False for w in window)
                and len({w["content_hash"] for w in window}) == 1):
            if output and not (output.is_file() and output.stat().st_size):
                # A read-only reviewer that could not write the file left its
                # review in the pane: keep that as the reply.
                output.write_text(_herdr_agent_text(spec["herdr_agent"]) or "", encoding="utf-8")
            return 0, "success"
        time.sleep(poll)


def _herdr_json(args: list[str]) -> dict:
    try:
        proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=30)
        return json.loads(proc.stdout or "{}").get("result") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _herdr_pane(run: dict, cwd: Path, label: str | None = None) -> str | None:
    """A visible Herdr pane for a dispatch, split beside the caller's own pane.

    The first dispatch splits the orchestrator's pane (`HERDR_PANE_ID`, or
    `OFFICE_HERDR_ANCHOR` when set) to the right, so the agent appears in the
    tab the user is watching. Later dispatches reuse a pane whose dispatch has
    ended, else stack down in that column. The caller's pane is only ever split,
    never run in or closed. Without a caller pane, the run gets its own tab."""
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    layout = json.loads(tab_file.read_text()) if tab_file.is_file() else None
    anchor = os.environ.get("OFFICE_HERDR_ANCHOR") or os.environ.get("HERDR_PANE_ID")
    if layout and layout.get("mode") != "split" or (layout is None and not anchor):
        pane = _herdr_own_tab_pane(run, cwd, tab_file, layout)
    else:
        pane = _herdr_split_pane(run, cwd, tab_file, layout, anchor)
    if pane and label:
        subprocess.run(["herdr", "pane", "rename", pane, label], capture_output=True, timeout=30)
    return pane


def _busy_panes(run: dict) -> set:
    con = db.connect()
    try:
        return {r["pane_id"] for r in con.execute("SELECT pane_id FROM dispatches WHERE run_id=? AND launcher='herdr' "
                                                  "AND status IN ('launching','running')", (run["id"],)).fetchall()}
    finally:
        con.close()


def _herdr_split_pane(run: dict, cwd: Path, tab_file: Path, layout: dict | None, anchor: str | None) -> str | None:
    layout = layout or {"mode": "split", "anchor": anchor, "panes": []}
    anchor = layout.get("anchor") or anchor
    live = [p for p in layout["panes"] if _herdr_json(["pane", "get", p])]  # the user may close panes
    busy = _busy_panes(run)
    for pane in live:
        if pane not in busy:
            subprocess.run(["herdr", "pane", "run", pane, f"cd {shlex.quote(str(cwd))}"], capture_output=True, timeout=30)
            layout["panes"] = live
            atomic_write_json(tab_file, layout)
            return pane
    # Split the caller's pane vertically (side by side); stack further agents in that column.
    target, direction = (live[-1], "down") if live else (anchor, "right")
    res = _herdr_json(["pane", "split", "--pane", target, "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    info = res.get("pane") or {}
    pane = info.get("pane_id")
    if not pane:
        return None
    layout["panes"] = live + [pane]
    layout.setdefault("tab_id", info.get("tab_id"))
    atomic_write_json(tab_file, layout)
    return pane


def _herdr_own_tab_pane(run: dict, cwd: Path, tab_file: Path, tab: dict | None) -> str | None:
    """No caller pane to split (or a run begun before split mode): a tab owned by the run."""
    if tab and not _herdr_json(["tab", "get", tab["tab_id"]]):
        tab = None  # the user closed it
    if tab is None:
        workspace = (os.environ.get("HERDR_WORKSPACE_ID") or os.environ.get("HERDR_PANE_ID") or "").split(":")[0]
        args = ["tab", "create", "--label", f"office-{run['id'][:8]}", "--cwd", str(cwd), "--no-focus"]
        if workspace:
            args[2:2] = ["--workspace", workspace]
        res = _herdr_json(args)
        root = (res.get("root_pane") or {}).get("pane_id")
        if not root:
            return None
        tab = {"mode": "tab", "tab_id": (res.get("tab") or {}).get("tab_id"), "panes": [root]}
        atomic_write_json(tab_file, tab)
        return root
    busy = _busy_panes(run)
    for pane in tab["panes"]:
        if pane not in busy:
            subprocess.run(["herdr", "pane", "run", pane, f"cd {shlex.quote(str(cwd))}"], capture_output=True, timeout=30)
            return pane
    direction = "right" if len(tab["panes"]) % 2 else "down"
    res = _herdr_json(["pane", "split", "--pane", tab["panes"][-1], "--direction", direction, "--cwd", str(cwd), "--no-focus"])
    pane = (res.get("pane") or {}).get("pane_id")
    if pane:
        tab["panes"].append(pane)
        atomic_write_json(tab_file, tab)
    return pane


def close_herdr_tab(run: dict) -> None:
    """Close what the run created: its panes in the caller's tab, or its own tab."""
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    if tab_file.is_file() and shutil.which("herdr"):
        try:
            tab = json.loads(tab_file.read_text())
            if tab.get("mode") == "split":
                for pane in tab.get("panes") or []:
                    subprocess.run(["herdr", "pane", "close", pane], capture_output=True, timeout=30)
            else:
                subprocess.run(["herdr", "tab", "close", tab["tab_id"]], capture_output=True, timeout=30)
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            pass


def _pane_ledger(run: dict, dispatch: dict, pane: str) -> None:
    ledger = paths.run_dir(run["id"]) / "panes.jsonl"
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pane_id": pane, "agent": None, "dispatch_id": dispatch["id"], "run_id": run["id"],
                             "role": dispatch.get("role"), "recorded_at": now_iso()}) + "\n")


def _record_launch(run: dict, dispatch_id: str, *, launcher: str, pid: int | None = None, pane_id: str | None = None) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET launcher=?, pid=COALESCE(?, pid), pane_id=?, launched_at=?, "
                        "status=CASE WHEN status='launching' THEN 'running' ELSE status END WHERE id=?",
                        (launcher, pid, pane_id, now_iso(), dispatch_id))
            d = state.get_dispatch(con, dispatch_id)
            if d.get("task_id"):
                task = state.get_task(con, run["id"], d["task_id"])
                if task and task["current_dispatch_id"] == dispatch_id and task["status"] == "launching":
                    state.update_task(con, run["id"], d["task_id"], status="running")
    finally:
        con.close()


def _started(dispatch_id: str, timeout: float) -> bool:
    """True once the supervisor has marked the dispatch (it sets last_seen_at)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
        finally:
            con.close()
        if d.get("last_seen_at") or d["status"] in ("exited", "failed", "cancelled"):
            return True
        time.sleep(1)
    return False


def _wait_terminal(dispatch_id: str, timeout: float | None = None) -> dict:
    deadline = time.time() + (timeout or 4 * 3600)
    while time.time() < deadline:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
        finally:
            con.close()
        if d["status"] in ("exited", "failed", "cancelled"):
            return {"exit_code": d["exit_code"], "terminal": d["terminal_classification"], "signal": d["signal"]}
        if d.get("pid") and d.get("launcher") in ("process", "process-fallback") and not pid_alive(d["pid"]) and d["status"] == "running":
            time.sleep(1)
            continue
        time.sleep(2)
    return {"exit_code": None, "terminal": "timeout"}


# ------------------------------------------------------------------ supervisor

def supervise(dispatch_id: str) -> int:
    """Run one agent process and always record how it ended, including when
    the supervisor itself cannot start the agent."""
    started = time.time()
    code, sig, classification = None, None, None
    child = None
    state_box = {"signal": None, "watching": False}

    def forward(signum, _frame):
        state_box["signal"] = signum
        if state_box["watching"]:
            raise _Stopped(signum)
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except OSError:
                pass

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, forward)
    log_path = None
    try:
        con = db.connect()
        try:
            d = state.get_dispatch(con, dispatch_id)
            run = state.get_run(con, d["run_id"])
        finally:
            con.close()
        if os.environ.get("OFFICE_SUPERVISOR_VIA") == "herdr" and d.get("launcher") != "herdr":
            # This pane command arrived after the launch fell back to a plain
            # process; running it too would mean two writers for one dispatch.
            classification = "duplicate_ignored"
            return 0
        spec = json.loads((paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "launch.json").read_text())
        if spec.get("herdr_agent"):
            # The agent runs in its herdr pane; this process only watches for the end.
            _mark(dispatch_id, pid_child=os.getpid())
            state_box["watching"] = True
            code, classification = watch_herdr_agent(dispatch_id, spec)
            state_box["watching"] = False
            return 0 if classification == "success" else 1
        log_path = Path(d.get("log_path") or spec.get("log_path"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        adapter = adapters.load_all()[d["adapter_id"]]
        output = Path(spec["output"]) if spec.get("output") else None
        argv, prof = adapters.build_argv(adapter, spec["kind"], model=d["model"], effort=d["effort"] or "none",
                                         cwd=Path(spec["cwd"]), output=output,
                                         images=[Path(i) for i in spec.get("images") or []],
                                         include_dirs=[Path(i) for i in spec.get("include_dirs") or []])
        prompt = Path(spec["prompt_file"]).read_text(encoding="utf-8")
        if prof.get("image_transport") in ("prompt-at", "prompt-path") and spec.get("images"):
            prefix = "@" if prof["image_transport"] == "prompt-at" else ""
            prompt += "\n\nEvidence images (inspect each one):\n" + "\n".join(f"{prefix}{i}" for i in spec["images"])
        env = worker_env(run, d, d["role"] if spec["kind"] == "worker" else "reviewer")
        if spec["kind"] != "worker":
            # Reviewers get no Office authority: they cannot submit or ack.
            for k in ("OFFICE_TASK_ID", "OFFICE_DISPATCH_ID"):
                env.pop(k, None)
            env["OFFICE_ROLE"] = "reviewer"
        with log_path.open("ab") as log:
            os.chmod(log_path, 0o600)
            stdin = subprocess.PIPE if prof.get("prompt") == "stdin" else subprocess.DEVNULL
            if prof.get("prompt") == "argv":
                argv = argv + [prompt]
            elif prof.get("prompt") == "argv-bound":
                argv = argv + [prof.get("prompt_flag", "--prompt=") + prompt]
            child = subprocess.Popen(argv, cwd=spec["cwd"], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
            _mark(dispatch_id, pid_child=child.pid)
            _agent_pgid_file(run, dispatch_id).write_text(str(child.pid))
            if stdin == subprocess.PIPE:
                try:
                    child.stdin.write(prompt.encode())
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            for chunk in iter(lambda: child.stdout.read1(65536), b""):
                log.write(chunk)
                log.flush()
                if sys.stdout.isatty():
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
            child.wait()
        code = child.returncode
        if code is not None and code < 0:
            sig, classification = -code, "signal"
        elif state_box["signal"]:
            sig, classification = state_box["signal"], "signal"
        else:
            classification = "success" if code == 0 else "nonzero"
    except _Stopped as stop:
        sig, classification = stop.signum, "signal"
    except FileNotFoundError as exc:
        code, classification = 127, "launch_failed"
        _note(log_path, f"launch failed: {exc}")
    except BaseException as exc:  # the classification must still be recorded
        classification = "supervisor_error"
        _note(log_path, f"supervisor error: {exc!r}")
    finally:
        if classification != "duplicate_ignored":
            _finish(dispatch_id, code, sig, classification or "unknown", time.time() - started)
    return 0 if classification == "success" else 1


class _Stopped(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = int(signum)


def _note(log_path, text: str) -> None:
    if log_path is None:
        return
    try:
        with Path(log_path).open("ab") as log:
            log.write(f"\n[office] {text}\n".encode())
    except OSError:
        pass


def _mark(dispatch_id: str, pid_child: int) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET pid=?, status=CASE WHEN status='launching' THEN 'running' ELSE status END, "
                        "last_seen_at=? WHERE id=?", (os.getpid(), now_iso(), dispatch_id))
    finally:
        con.close()


def _finish(dispatch_id: str, code, sig, classification: str, wall: float) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            d = state.get_dispatch(con, dispatch_id)
            status = "cancelled" if d["status"] == "cancelled" else ("exited" if classification in ("success", "nonzero", "signal") else "failed")
            con.execute("UPDATE dispatches SET status=?, exit_code=?, signal=?, terminal_classification=?, ended_at=?, "
                        "wall_clock_seconds=? WHERE id=?", (status, code, sig, classification, now_iso(), wall, dispatch_id))
            run = state.get_run(con, d["run_id"])
            state.emit(con, run, "dispatch.ended", f"{d['task_id'] or d['role']} {d['role']} ended: {classification}"
                       + (f" (exit {code})" if code not in (None, 0) else ""), audience="runtime",
                       task_id=d["task_id"], dispatch_id=dispatch_id,
                       payload={"exit_code": code, "signal": sig, "classification": classification})
            if d["kind"] in ("planner", "executor"):
                after_worker_exit(con, run, dispatch_id)
    finally:
        con.close()
    con = db.connect()
    try:
        jobs.kick(con, d["run_id"])
    finally:
        con.close()


def after_worker_exit(con, run: dict, dispatch_id: str) -> None:
    """A worker that exits without submitting leaves a blocker, not a pass."""
    d = state.get_dispatch(con, dispatch_id)
    task = state.get_task(con, run["id"], d["task_id"]) if d.get("task_id") else None
    if task is None or task["current_dispatch_id"] != dispatch_id:
        return
    if task["status"] == "changes_required":
        # Findings arrived while this session was still alive; it ended without
        # acting on them, so the next fix round starts in a fresh session.
        request_launch(con, run, task["id"], role=d["role"], fix_of=task.get("current_revision_id"))
        return
    if task["status"] in ("running", "launching"):
        submitted = con.execute("SELECT 1 FROM revisions WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        plan_sub = con.execute("SELECT 1 FROM plans WHERE run_id=? AND created_by=?", (run["id"], dispatch_id)).fetchone()
        if submitted or plan_sub:
            return
        refused = con.execute("SELECT summary FROM events WHERE run_id=? AND dispatch_id=? AND kind='submit.refused' "
                              "ORDER BY seq DESC LIMIT 1", (run["id"], dispatch_id)).fetchone()
        if refused:
            # The worker's submit was refused for a reason a fresh session would
            # hit again; the orchestrator must amend the scope or the brief.
            state.update_task(con, run["id"], task["id"], status="blocked", pause_reason=f"submit refused: {refused[0]}")
            state.emit(con, run, "task.blocked", f"{task['id']} submit refused ({refused[0]}); amend the scope "
                       f"(office amend {task['id']} -- ...) or the brief; work is preserved in its worktree",
                       task_id=task["id"])
            return
        retries = con.execute("SELECT COUNT(*) FROM dispatches d WHERE d.run_id=? AND d.task_id=? AND d.terminal_classification "
                              "IS NOT NULL AND NOT EXISTS (SELECT 1 FROM revisions r WHERE r.dispatch_id=d.id)",
                              (run["id"], task["id"])).fetchone()[0]
        limit = (state.pinned_config(run).get("verification") or {}).get("environment_retry_max", 2)
        if retries <= limit:
            state.emit(con, run, "task.relaunch", f"{task['id']} worker ended ({d['terminal_classification']}) "
                       f"without submitting; relaunching {retries}/{limit}", audience="runtime", task_id=task["id"])
            request_launch(con, run, task["id"], role=d["role"])
            return
        state.update_task(con, run["id"], task["id"], status="blocked",
                          pause_reason=f"worker ended ({d['terminal_classification']}) without submitting")
        state.emit(con, run, "task.blocked", f"{task['id']} worker ended without submitting "
                   f"({d['terminal_classification']}); work is preserved in its worktree", task_id=task["id"])


def job_notify_worker(con, run: dict, job: dict) -> dict:
    """Best-effort native nudge to a live Herdr-hosted worker. Delivery truth
    stays in runs.db and rides on the worker's next office command."""
    d = state.get_dispatch(con, job["payload"]["dispatch_id"])
    if not d or d.get("launcher") != "herdr" or not d.get("pane_id") or d["status"] != "running":
        return {"sent": False}
    text = job["payload"].get("text", "office status has an update for you.")
    subprocess.run(["herdr", "agent", "prompt", d["pane_id"], text], capture_output=True, timeout=30)
    return {"sent": True}


def start_stacked(con, run: dict, accepted_task: str) -> list[str]:
    """Launch tasks the orchestrator stacked after `accepted_task`. Caller holds tx."""
    started = []
    for t in state.tasks(con, run["id"]):
        if t["status"] == "queued" and t.get("stack_after") == accepted_task:
            graph = {x["id"]: x["depends"] for x in state.tasks(con, run["id"])}
            base = _base_for(con, run, t, graph, accepted_task)
            request_launch(con, run, t["id"], role="executor", base=base)
            started.append(t["id"])
    return started
