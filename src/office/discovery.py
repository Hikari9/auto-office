"""Ambiguity-safe run resolution.

Order: explicit --run/--state-dir > OFFICE_STATE_DIR/OFFICE_RUN_ID > session
binding > the task worktree of an open executor dispatch > the sole active run
in this repository > error. Latest modification
time never selects a run, and opening a session never creates or binds one.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from office import legacy, paths
from office.state import NoRun, Refused, Usage, find_run, get_dispatch, get_run, get_task, TERMINAL_PHASES
from office.util import now_iso, short

HARNESS_NAMES = ("claude", "codex", "gemini", "agy", "hermes")


@dataclass
class Target:
    run: dict | None = None
    legacy: legacy.LegacyRun | None = None
    source: str = ""
    dispatch: dict | None = None  # the executor dispatch a task-worktree cwd names


def session_keys(harness: str | None = None, session: str | None = None) -> list[tuple[str, str]]:
    """Binding keys that identify the calling agent session, most specific first."""
    keys = []
    if harness and session:
        keys.append((harness, session))
    env_h, env_s = os.environ.get("OFFICE_HARNESS"), os.environ.get("OFFICE_SESSION")
    if env_h and env_s:
        keys.append((env_h, env_s))
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        keys.append(("herdr", pane))
    proc = process_key()
    if proc:
        keys.append(("proc", proc))
    return keys


def process_key() -> str | None:
    """'<harness>:<pid>:<start>' for the nearest ancestor harness process."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,lstart=,comm="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), "-".join(parts[2:7]), os.path.basename(" ".join(parts[7:])))
        except ValueError:
            continue
    pid = os.getppid()
    for _ in range(16):
        if pid <= 1 or pid not in table:
            return None
        ppid, start, comm = table[pid]
        if comm in HARNESS_NAMES:
            return f"{comm}:{pid}:{start}"
        pid = ppid
    return None


def binding_file(primary: Path, harness: str, session: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_.:" else "_" for c in session).replace(":", "_")
    return primary / ".office" / "sessions" / f"{harness}-{safe}.json"


def bound_run(con, keys: list[tuple[str, str]]) -> dict | None:
    for harness, session in keys:
        row = con.execute("SELECT run_id FROM session_bindings WHERE harness=? AND session_id=? AND ended_at IS NULL",
                          (harness, session)).fetchone()
        if row:
            run = get_run(con, row[0])
            if run and run["phase"] not in TERMINAL_PHASES:
                return run
    return None


OPEN_DISPATCH = ("launching", "running")


def recovery_command(run: dict, d: dict) -> str:
    """The shell line that restores an executor's dispatch identity."""
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    return f". {shlex.quote(str(ddir / 'agent.env'))} && cd {shlex.quote(str(d.get('worktree') or ''))} && office submit"


def open_executor_dispatches(con, run: dict) -> list[dict]:
    rows = con.execute("SELECT id FROM dispatches WHERE run_id=? AND role='executor' AND status IN ('launching','running') "
                       "ORDER BY started_at, id", (run["id"],)).fetchall()
    return [get_dispatch(con, r[0]) for r in rows]


def task_worktree(con, cwd: Path | str | None = None) -> tuple[dict, dict] | None:
    """(run, dispatch) when cwd is inside an Office task worktree
    (<worktrees>/<run>/<T>) whose task's current executor dispatch is open."""
    try:
        rel = Path(cwd or os.getcwd()).resolve().relative_to(paths.worktrees_dir().resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) < 2:
        return None
    try:
        run = find_run(con, rel.parts[0])
    except Usage:
        return None
    if run is None or not run.get("office_version"):
        return None
    task = get_task(con, run["id"], rel.parts[1])
    d = get_dispatch(con, task["current_dispatch_id"]) if task and task.get("current_dispatch_id") else None
    if d and d["role"] == "executor" and d.get("status") in OPEN_DISPATCH:
        return run, d
    return None


def executor_session_dispatch(con, keys: list[tuple[str, str]]) -> tuple[dict, dict] | None:
    """(run, dispatch) of an open executor dispatch whose recorded session is one of `keys`."""
    for _harness, session in keys:
        row = con.execute("SELECT id, run_id FROM dispatches WHERE session_id=? AND role='executor' "
                          "AND status IN ('launching','running')", (session,)).fetchone()
        if row:
            return get_run(con, row["run_id"]), get_dispatch(con, row["id"])
    return None


def _end_stale_bindings(con, keys: list[tuple[str, str]], run: dict) -> None:
    """End bindings of an executor's own session; never rebinds."""
    from office import db
    live = [(h, s) for h, s in keys if con.execute(
        "SELECT 1 FROM session_bindings WHERE harness=? AND session_id=? AND ended_at IS NULL", (h, s)).fetchone()]
    if live:
        with db.transaction(con):
            for h, s in live:
                con.execute("UPDATE session_bindings SET ended_at=? WHERE harness=? AND session_id=?", (now_iso(), h, s))
        if run.get("git_common_dir"):
            primary = paths.primary_checkout(Path(run["git_common_dir"]))
            for h, s in live:
                try:
                    binding_file(primary, h, s).unlink(missing_ok=True)
                except OSError:
                    pass


def refuse_executor_binding(run: dict, d: dict, what: str) -> Refused:
    return Refused("executor-lost-binding",
                   f"{what} belongs to executor dispatch {d['id']} for {d['task_id']} in run {short(run['id'])}; "
                   "it must not be bound as the orchestrator",
                   next_step=recovery_command(run, d))


def bind(con, run: dict, keys: list[tuple[str, str]], bound_by: str) -> list[tuple[str, str]]:
    """Record bindings (caller holds the transaction) and write fast-path stubs.
    A session that belongs to an executor is never recorded as an orchestrator."""
    from office.util import atomic_write_json
    if os.environ.get("OFFICE_DISPATCH_ID"):
        return []
    found = executor_session_dispatch(con, keys)
    if found:
        raise refuse_executor_binding(found[0], found[1], "this session")
    made = []
    primary = paths.primary_checkout(Path(run["git_common_dir"])) if run.get("git_common_dir") else None
    for harness, session in keys:
        con.execute(
            "INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) VALUES(?,?,?,?,?) "
            "ON CONFLICT(harness, session_id) DO UPDATE SET run_id=excluded.run_id, bound_at=excluded.bound_at, "
            "bound_by=excluded.bound_by, ended_at=NULL",
            (harness, session, run["id"], now_iso(), bound_by))
        if primary is not None:
            try:
                atomic_write_json(binding_file(primary, harness, session),
                                  {"schema": 1, "harness": harness, "harness_session_id": session,
                                   "run_id": run["id"], "bound_at": now_iso(), "bound_by": bound_by,
                                   "_authority": "runs.db"})
            except OSError:
                pass
        made.append((harness, session))
    return made


def active_in_repo(con, common_dir: Path) -> tuple[list[dict], list[legacy.LegacyRun]]:
    rows = con.execute(
        "SELECT id FROM runs WHERE git_common_dir=? AND office_version IS NOT NULL AND phase NOT IN ('closed','abandoned') "
        "ORDER BY created_at", (str(common_dir),)).fetchall()
    runs = [get_run(con, r[0]) for r in rows]
    legacy_active = [r for r in legacy.legacy_runs(paths.primary_checkout(common_dir)) if r.active]
    return runs, legacy_active


def resolve(con, *, run_arg: str | None = None, state_dir: str | None = None,
            harness: str | None = None, session: str | None = None, require_active: bool = False,
            cwd: Path | None = None) -> Target:
    # 1. explicit flags
    if run_arg:
        run = find_run(con, run_arg)
        if run and run.get("office_version"):
            return Target(run=run, source="flag")
        leg = _legacy_by_id(con, run_arg, cwd)
        if leg:
            return Target(legacy=leg, source="flag")
        raise NoRun("no-such-run", f"no Office run matches {run_arg!r}", next_step="office list")
    if state_dir:
        return _from_state_dir(con, Path(state_dir).expanduser(), "flag")
    # 2. environment
    env_dir = os.environ.get("OFFICE_STATE_DIR")
    if env_dir:
        return _from_state_dir(con, Path(env_dir).expanduser(), "env")
    env_run = os.environ.get("OFFICE_RUN_ID")
    if env_run:
        run = find_run(con, env_run)
        if run is None:
            raise NoRun("no-such-run", f"OFFICE_RUN_ID={env_run} names no run", next_step="office list")
        return Target(run=run, source="env")
    # 3. an executor that lost its env: its open task worktree, or a session id
    # recorded on its open dispatch, names run and dispatch. This wins over a
    # session binding, which an older `office resume` may have wrongly made.
    keys = session_keys(harness, session)
    found = task_worktree(con, cwd)
    source = "task-worktree"
    if not found:
        found, source = executor_session_dispatch(con, keys), "executor-session"
    if found:
        _end_stale_bindings(con, keys, found[0])
        return Target(run=found[0], source=source, dispatch=found[1])
    # 4. session binding
    run = bound_run(con, keys)
    if run:
        return Target(run=run, source="session")
    # 5. sole active run in this repository
    ident = paths.repo_identity(cwd)
    if ident is None:
        raise NoRun("no-repository", "not inside a git repository and no run was named",
                    next_step="office list, then office resume <run>")
    runs, legacy_active = active_in_repo(con, ident[1])
    total = len(runs) + len(legacy_active)
    if total == 1:
        return Target(run=runs[0], source="sole-active") if runs else Target(legacy=legacy_active[0], source="sole-active")
    if total == 0:
        raise NoRun("no-active-run", "no active Office run in this repository",
                    next_step='office start "<goal>"')
    listing = [f"{short(r['id'])}  {r['phase']:<10} {r['goal'][:60]}" for r in runs]
    listing += [f"{short(r.run_id)}  {r.phase:<10} {r.goal[:60]} (3.0 legacy)" for r in legacy_active]
    raise NoRun("ambiguous-run", f"{total} active Office runs in this repository; none is bound to this session",
                next_step="office resume <id> to bind one, or pass --run <id>",
                data={"candidates": listing})


def _legacy_by_id(con, run_arg: str, cwd) -> legacy.LegacyRun | None:
    candidate = paths.runs_dir() / run_arg
    found = legacy.read_legacy_state(candidate)
    if found:
        return found
    ident = paths.repo_identity(cwd)
    if ident:
        for r in legacy.legacy_runs(paths.primary_checkout(ident[1])):
            if r.run_id.startswith(run_arg):
                return r
    return None


def _from_state_dir(con, path: Path, source: str) -> Target:
    leg = legacy.read_legacy_state(path)
    if leg:
        return Target(legacy=leg, source=source)
    run = get_run(con, path.name) if path.name else None
    if run is None:
        raise NoRun("no-such-run", f"{path} is not an Office run directory", next_step="office list")
    return Target(run=run, source=source)
