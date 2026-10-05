"""office preflight: read-only checks an executor runs before office submit.

Each check names what is wrong and the exact repair; preflight itself changes
nothing. The verdict line and exit code say what the caller does next:

  PREFLIGHT ready  exit 0   run office submit
  PREFLIGHT fix    exit 1   apply the listed repairs, then preflight again
  PREFLIGHT wait   exit 75  the task is paused but this session still holds it;
                            poll preflight until it is ready, then submit
  PREFLIGHT stop   exit 4   terminal for this session (lease lost, superseded,
                            cancelled, blocked on the orchestrator): emit the
                            status block and stop; never retry

A lost lease is never reacquired here. Only the orchestrator moves a task to a
new holder (office revoke / rerun); a worker that reclaims its own lease would
undo the takeover that revoked it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from office import discovery, paths, planfile, state
from office.result import Result

EXIT = {"ready": 0, "fix": 1, "stop": 4, "wait": 75}
# Pauses the orchestrator resolves while the worker keeps its lease; anything
# else that pauses or blocks a task ends this session's part in it.
WAITABLE = ("plan defect", "contract amendment", "brief defect", "stacked after")


def _git(wt: Path, *args: str) -> str:
    return paths.git(wt, *args, check=False)


def _packet(run: dict, d: dict) -> dict:
    try:
        return json.loads((paths.run_dir(run["id"]) / "dispatches" / d["id"] / "packet.json").read_text())
    except (OSError, ValueError):
        return {}


def _sed_backups(wt: Path) -> list[str]:
    """Untracked `<file>-e` next to a tracked `<file>`: BSD sed took `-e` as the
    -i backup suffix, so a GNU-style `sed -i -e` edit was not what it looked like."""
    tracked = set(_git(wt, "ls-files", "-z").split("\0"))
    others = _git(wt, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(f for f in others if f.endswith("-e") and f[:-2] in tracked)


def preflight(con, run: dict, cwd: Path) -> Result:
    from office import submit
    res = Result()
    stop: list[str] = []
    fix: list[str] = []
    wait: list[str] = []

    d = submit._worktree_dispatch(con, run, cwd)
    env_dispatch = os.environ.get("OFFICE_DISPATCH_ID")
    if d is None and env_dispatch:
        d = state.get_dispatch(con, env_dispatch)
    if d is None or d["role"] != "executor":
        found = discovery.task_worktree(con, cwd)
        res.lines = ["PREFLIGHT stop", "role: no executor dispatch owns this directory"]
        res.next = (f"cd {found[1]['worktree']} and run office preflight there" if found
                    else "run office preflight from your task worktree; if you are not an executor, do not submit")
        res.exit_code = EXIT["stop"]
        return res
    task = state.get_task(con, run["id"], d["task_id"])
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    wt = Path(d["worktree"]).resolve()
    res.data = {"task": task["id"], "dispatch": d["id"], "worktree": str(wt)}

    # 1. Role and identity. A shell env cannot be repaired from a child process,
    # so the repair is the line to prefix to office submit in the same shell.
    role = os.environ.get("OFFICE_ROLE")
    source = f". {ddir / 'agent.env'}"
    if role != "executor" or (env_dispatch and env_dispatch != d["id"]):
        fix.append(f"role: OFFICE_ROLE={role or 'unset'}, dispatch={env_dispatch or 'unset'}; "
                   f"submit in one command: {source} && office submit")
    res.data["source"] = source
    here = paths.repo_identity(cwd)
    if here is None or here[0] != wt:
        fix.append(f"worktree: submit refuses outside the task worktree; cd {wt}")

    # 2. Ownership. Superseded or lease-lost is terminal for this session.
    if task["current_dispatch_id"] != d["id"]:
        stop.append(f"superseded: {task['id']} now belongs to dispatch {task['current_dispatch_id']}")
    else:
        from office import dispatch as dispatch_mod
        if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
            stop.append(f"lease-lost: {task['id']} lease {d['lease_id']} was revoked or taken over")

    # 3. Task state.
    status, reason = task["status"], task.get("pause_reason") or ""
    if status in ("paused", "blocked", "cancelled") and not submit.self_blocked(task):
        if status == "paused" and reason.startswith(WAITABLE) and not stop:
            wait.append(f"paused: {task['id']} {reason}")
        else:
            stop.append(f"{status}: {task['id']} {reason}".rstrip())

    # 4. A fix round must have findings to fix.
    packet = _packet(run, d)
    if packet.get("fix_of"):
        rows = con.execute("SELECT code, severity, location, summary FROM findings WHERE run_id=? AND task_id=? "
                           "AND state='open' ORDER BY created_at", (run["id"], task["id"])).fetchall()
        if rows:
            res.lines += [f"finding: {r['code']} [{r['severity']}] {r['location'] or ''} {r['summary']}" for r in rows]
        else:
            stop.append(f"findings: fix round for {packet['fix_of']} but no open findings are recorded; "
                        "the orchestrator must name them (office prompt) or rerun --fresh")

    # 5. Scope: tracked edits outside the contract are refused at submit.
    base = d["base_commit"]
    head = _git(wt, "rev-parse", "HEAD")
    changed = [f for f in _git(wt, "diff", "--name-only", "-z", base, head).split("\0") if f]
    dep_bases = [b for b in submit._dependency_bases(con, run, task, head) if b != base]
    for b in dep_bases:
        also = set(_git(wt, "diff", "--name-only", "-z", b, head).split("\0"))
        changed = [f for f in changed if f in also]
    outside = [f for f in changed if not planfile.path_in_scope(f, task["scope"]) and not submit._harness_path(f)]
    if outside:
        listed = " ".join(outside[:10])
        fix.append(f"scope: tracked edits outside SCOPE: {listed}; revert tool-stamped ones with "
                   f"git checkout {base[:12]} -- <file>, or ask: office submit --request-scope <file> -- \"<reason>\"")
    res.data["outside_scope"] = outside

    # 5b. Scope-none evidence: submit refuses a file it already ingested.
    if not task["scope"]:
        from office import briefs
        ev = submit._read_evidence(wt)
        if ev and ev[2] in submit._ingested_digests(con, run, task["id"]):
            fix.append(f"evidence: {briefs.EVIDENCE_FILE} repeats evidence already submitted for {task['id']}; "
                       "write it fresh for this submission")

    # 6. Silent BSD sed failures.
    backups = _sed_backups(wt)
    if backups:
        fix.append(f"sed: BSD sed wrote backup files {' '.join(backups[:10])}: a `sed -i -e` edit used `-e` as the "
                   "backup suffix; check git diff that each edit landed, delete the backups, and rebuild anything "
                   "generated from those files")
    res.data["sed_backups"] = backups

    res.data["head"] = _git(wt, "rev-parse", "HEAD")
    verdict = "stop" if stop else "fix" if fix else "wait" if wait else "ready"
    res.lines = [f"PREFLIGHT {verdict}"] + [f"stop: {s}" for s in stop] + [f"fix: {f}" for f in fix] \
        + [f"wait: {w}" for w in wait] + res.lines
    res.data["verdict"] = verdict
    res.exit_code = EXIT[verdict]
    res.next = {
        "ready": f"{source} && office submit",
        "fix": "apply each fix above, then office preflight",
        "wait": "poll office preflight every 60s (Monitor or a sleep loop) until it prints ready, then submit; "
                "stop if it prints stop",
        "stop": "do not retry; end with the STATUS block (SUBMIT=refused: <the stop line>) and stop",
    }[verdict]
    return res
