"""The repo-declared `worktree.setup` command (#255).

Office never picks a package manager. The repo declares one command in
`.auto-office/config.yaml`, it is pinned into the run policy at `office start`,
and Office runs it once in each new task, integration, and check worktree.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from office import db, state
from office.util import sha256_bytes

KINDS = ("task", "integration", "check")
DEFAULT_TIMEOUT_S = 600
LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock", "poetry.lock",
             "Cargo.lock", "Gemfile.lock", "go.sum", "Pipfile.lock", "composer.lock", "mix.lock", "pubspec.lock",
             "packages.lock.json")
SKIP_DIRS = {"node_modules", ".git", ".office"}
MARKER = Path(".office") / "setup.json"


def settings(run: dict) -> dict:
    """The pinned worktree settings: {setup, timeout, applies_to}. `setup` is '' when unset."""
    raw = state.pinned_config(run).get("worktree") or {}
    applies = raw.get("applies_to")
    applies = [a for a in applies if a in KINDS] if isinstance(applies, list) else list(KINDS)
    try:
        timeout = int(raw.get("setup_timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    inputs = raw.get("setup_inputs")
    inputs = [str(i) for i in inputs if str(i).strip()] if isinstance(inputs, list) else []
    return {"setup": str(raw.get("setup") or "").strip(), "timeout": timeout, "applies_to": applies,
            "inputs": inputs}


def _input_files(wt: Path, patterns: list[str]) -> list[Path]:
    """The files whose content decides whether setup is stale: exactly `setup_inputs` when set,
    else the known lockfiles at the root and one directory deep. Never walks node_modules or .git."""
    wt = Path(wt)
    if patterns:
        found = {p for pat in patterns if not Path(pat).is_absolute() for p in wt.glob(pat)}
    else:
        found = {p for name in LOCKFILES for p in (wt / name, *wt.glob(f"*/{name}"))}
    keep = []
    for p in found:
        rel = p.relative_to(wt) if p.is_relative_to(wt) else None
        if rel is not None and not SKIP_DIRS.intersection(rel.parts) and p.is_file():
            keep.append(p)
    return sorted(keep)


def lock_hash(run: dict, wt: Path) -> str:
    """A hash of the setup command and its input files, so a changed dependency set or an edited
    `setup` re-runs setup."""
    cfg = settings(run)
    parts = [b"setup\0" + cfg["setup"].encode()]
    for f in _input_files(wt, cfg["inputs"]):
        parts.append(str(f.relative_to(wt)).encode() + b"\0" + f.read_bytes())
    return sha256_bytes(b"\0\0".join(parts))


def _marker(wt: Path) -> dict:
    try:
        return json.loads((Path(wt) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def should_run(run: dict, wt: Path, kind: str, *, created: bool) -> bool:
    cfg = settings(run)
    if not cfg["setup"] or kind not in cfg["applies_to"]:
        return False
    if created:
        return True
    # Only a finished setup leaves a marker, so a missing one means setup never completed (an
    # interrupted launch, or a failed run) and it runs before the agent starts. A completed
    # marker suppresses reruns until a lockfile changes.
    marker = _marker(wt)
    return not marker.get("ok") or marker.get("lock_hash") != lock_hash(run, wt)


def tail(log: Path, lines: int = 5) -> str:
    try:
        return " | ".join(log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return ""


def execute(run: dict, wt: Path, log: Path, *, marker: bool = False) -> dict:
    """Run the setup command in `wt`, output to `log`. Returns command, exit, seconds, timed_out, lock_hash, log."""
    cfg = settings(run)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("OFFICE_") and k not in ("OFFICE_DATA_HOME", "OFFICE_STATE_HOME"))}
    started, timed_out = time.time(), False
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cfg["setup"], shell=True, cwd=str(wt), stdout=fh, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            code = 124
            fh.write(f"\nsetup timed out after {cfg['timeout']}s\n")
    os.chmod(log, 0o600)
    result = {"command": cfg["setup"], "exit": code, "seconds": round(time.time() - started, 2),
              "timed_out": timed_out, "lock_hash": lock_hash(run, wt), "log": str(log)}
    if not marker or code != 0:  # only task worktrees outlive one run (and ignore .office/); only success marks done
        return result
    try:
        (Path(wt) / MARKER).parent.mkdir(parents=True, exist_ok=True)
        (Path(wt) / MARKER).write_text(json.dumps({**result, "ok": True}), encoding="utf-8")
    except OSError:
        pass
    return result


def failure_text(result: dict) -> str:
    why = f"timed out after {result['seconds']:.0f}s" if result.get("timed_out") else f"exit {result['exit']}"
    note = tail(Path(result["log"]))
    return f"worktree setup failed ({why}); log {result['log']}" + (f"; last lines: {note}" if note else "")


COMMAND_NOT_FOUND = 127


def deterministic_failure(result: dict) -> bool:
    """A failure every retry and every new worktree repeats: the shell found no such command."""
    return result.get("exit") == COMMAND_NOT_FOUND and not result.get("timed_out")


def stop_text(result: dict, task_id: str) -> str:
    """Why a dispatch stops before its executor starts, naming the failing command and the doctor check."""
    note = tail(Path(result["log"]), 1)
    return (f"worktree setup `{result['command']}` exited 127 (command not found){': ' + note if note else ''}; the executor "
            f"was not started. Install the missing tool or fix worktree.setup (office doctor checks it), then office rerun "
            f"{task_id} --fresh; log {result['log']}")


def record(run: dict, kind: str, result: dict, *, task_id: str | None = None, dispatch_id: str | None = None) -> None:
    """Emit setup.done (runtime audience) or setup.failed (orchestrator audience, shown by office wait)."""
    con = db.connect()
    try:
        ok = result["exit"] == 0
        summary = (f"{kind} worktree setup ok in {result['seconds']}s" if ok
                   else f"{kind} {failure_text(result)}")
        payload = {k: result[k] for k in ("command", "exit", "seconds", "log")}
        with db.transaction(con):
            state.emit(con, run, "setup.done" if ok else "setup.failed", summary,
                       audience="runtime" if ok else "orchestrator", task_id=task_id, dispatch_id=dispatch_id,
                       payload={**payload, "kind": kind})
    finally:
        con.close()


def prepare(run: dict, wt: Path, kind: str, log: Path, *, created: bool, task_id: str | None = None,
            dispatch_id: str | None = None) -> dict | None:
    """Run setup when due and record it. Returns the result, or None when nothing ran."""
    if not should_run(run, wt, kind, created=created):
        return None
    result = execute(run, wt, log, marker=kind == "task")
    record(run, kind, result, task_id=task_id, dispatch_id=dispatch_id)
    return result
