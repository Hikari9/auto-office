"""Legacy v3 runs: detection and exact-commit runtime retention.

A v3 run keeps `state.json` as its own authority and pins the plugin commit it
started under. It is never rewritten into the 3.1 contract. It drains on a
retained copy of exactly that commit, materialized with `git archive`.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import io
from dataclasses import dataclass
from pathlib import Path

from office import paths
from office.version import SOURCE_ROOT

LEGACY_TERMINAL = ("closed", "abandoned")


@dataclass
class LegacyRun:
    run_id: str
    state_dir: Path
    phase: str
    goal: str
    plugin_commit: str
    repo_root: str | None

    @property
    def office_version(self) -> str:
        return f"3.0-legacy@{self.plugin_commit[:12]}"

    @property
    def active(self) -> bool:
        return self.phase not in LEGACY_TERMINAL


def read_legacy_state(state_dir: Path) -> LegacyRun | None:
    state_path = Path(state_dir) / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("_authority") == "runs.db" or state.get("office_version"):
        return None
    if not state.get("run_id"):
        return None
    return LegacyRun(
        run_id=state["run_id"], state_dir=Path(state_dir), phase=str(state.get("phase") or "intake"),
        goal=str(state.get("goal") or ""), plugin_commit=str(state.get("plugin_commit") or "unknown"),
        repo_root=state.get("repo_root"))


def legacy_runs(primary_checkout: Path) -> list[LegacyRun]:
    """Runs a v3 `start` registered for this repository via `.office/runs/*.ref`."""
    out = []
    ref_dir = Path(primary_checkout) / ".office" / "runs"
    if not ref_dir.is_dir():
        return out
    for ref in sorted(ref_dir.glob("*.ref")):
        try:
            target = Path(ref.read_text(encoding="utf-8").strip())
        except OSError:
            continue
        run = read_legacy_state(target)
        if run is not None:
            out.append(run)
    return out


def retained_dir(plugin_commit: str) -> Path:
    return paths.runtimes_dir() / f"legacy-{plugin_commit[:12]}"


def retained_runtime(plugin_commit: str, *, materialize: bool = True) -> Path | None:
    """Path to the exact v3 runtime a legacy run pinned, creating it from this
    checkout's git history when possible. None when it cannot be provided."""
    target = retained_dir(plugin_commit)
    if (target / "scripts" / "office_runtime.py").is_file():
        return target
    if not materialize or not _is_sha(plugin_commit):
        return None
    if not (SOURCE_ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(["git", "-C", str(SOURCE_ROOT), "archive", "--format=tar", plugin_commit],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    staging = target.with_name(target.name + ".partial")
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(staging, filter="data")
    staging.rename(target)
    return target


def _is_sha(value: str) -> bool:
    return len(value) >= 7 and all(c in "0123456789abcdef" for c in value.lower())


def guidance(run: LegacyRun) -> tuple[str, str]:
    """(message, next) for an agent that reached a legacy run through `office`."""
    retained = retained_runtime(run.plugin_commit)
    if retained is None:
        return (f"{run.run_id[:8]} is an Auto Office 3.0 run pinned to plugin commit {run.plugin_commit[:12]}; "
                "that runtime is not available on this machine",
                f"restore it: git -C <auto-office checkout> archive {run.plugin_commit[:12]} | tar -x -C "
                f"{retained_dir(run.plugin_commit)} ; then office doctor")
    return (f"{run.run_id[:8]} is an Auto Office 3.0 run (phase {run.phase}); it stays on its pinned runtime",
            f"continue with the v3 skill at {retained}/SKILL.md using python3 {retained}/scripts/office_runtime.py "
            f"--state-dir {run.state_dir}")
