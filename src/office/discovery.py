"""Ambiguity-safe run resolution.

Order: explicit --run/--state-dir > OFFICE_STATE_DIR/OFFICE_RUN_ID > session
binding > the sole active run in this repository > error. Latest modification
time never selects a run, and opening a session never creates or binds one.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from office import legacy, paths
from office.state import NoRun, Usage, find_run, get_run, TERMINAL_PHASES
from office.util import now_iso, short

HARNESS_NAMES = ("claude", "codex", "gemini", "agy", "hermes")


@dataclass
class Target:
    run: dict | None = None
    legacy: legacy.LegacyRun | None = None
    source: str = ""


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


def bind(con, run: dict, keys: list[tuple[str, str]], bound_by: str) -> list[tuple[str, str]]:
    """Record bindings (caller holds the transaction) and write fast-path stubs."""
    from office.util import atomic_write_json
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
    # 3. session binding
    run = bound_run(con, session_keys(harness, session))
    if run:
        return Target(run=run, source="session")
    # 4. sole active run in this repository
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
