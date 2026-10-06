"""Which web commands a run may take, from the runtime that would serve it.

A run is pinned to a release line; the frontdoor serves it with the newest
registered patch on that line (or this process when it is that patch or
newer). New controls need a runtime that has `office queue` and `office amend
route`; runs on older lines (3.0 legacy, 3.1, 3.2) are read-only, and say why.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Callable

from office import frontdoor, version

CONTROL_LINE = "3.3"
RUN_KINDS = ("resume_run", "attach_run", "pause", "resume", "set_priority", "demote", "set_auto_mode",
             "change_route", "approve_plan", "chat_send")


def _probe_runtime(argv: tuple[str, ...]) -> bool:
    """Whether the runtime at `argv` has `office queue` and `office amend route --restart`."""
    try:
        queue = subprocess.run([*argv, "queue", "--help"], capture_output=True, text=True, timeout=20)
        amend = subprocess.run([*argv, "amend", "--help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return queue.returncode == 0 and amend.returncode == 0 and "--restart" in amend.stdout


@lru_cache(maxsize=32)
def _cached_probe(argv: tuple[str, ...]) -> bool:
    return _probe_runtime(argv)


class Resolver:
    """Resolves the serving runtime of a release line and whether it has the new controls.

    `registry(line)` returns the newest registered entry on a line
    (frontdoor.newest_on_line); `probe(argv)` checks a runtime's commands;
    `current` is this process's version, which has them by construction.
    """

    def __init__(self, registry: Callable[[str], dict | None] = frontdoor.newest_on_line,
                 probe: Callable[[tuple[str, ...]], bool] = _cached_probe, current: str | None = None):
        self.registry, self.probe = registry, probe
        self.current = current or version.current()

    def serving(self, office_version: str | None) -> dict:
        if not office_version:
            return {"version": None, "controls": False, "reason": "the run records no office_version (3.0 legacy)"}
        line = version.release_line(office_version)
        if version.release_key(line)[:2] < version.release_key(CONTROL_LINE)[:2]:
            return {"version": None, "controls": False,
                    "reason": f"Auto Office {line} run: web controls need the {CONTROL_LINE} line"}
        best = self.registry(line)
        mine = version.same_line(self.current, line)
        if mine and (best is None or version.release_key(best["office_version"]) <= version.release_key(self.current)):
            return {"version": self.current, "controls": True, "reason": None}
        if best is None:
            return {"version": None, "controls": False,
                    "reason": f"no {line}.x runtime is registered on this machine"}
        ok = self.probe(tuple(best.get("argv") or ()))
        return {"version": best["office_version"], "controls": ok,
                "reason": None if ok else
                f"runtime {best['office_version']} lacks office queue / office amend route; upgrade its patch"}


def for_run(run: dict | None, resolver: Resolver, *, launcher_reason: str | None = None) -> dict:
    """{kind: {allowed, reason}} for one projected run (T1 `projection.run`)."""
    if run is None:
        return {k: {"allowed": False, "reason": "run not found"} for k in RUN_KINDS}
    serving = resolver.serving(run.get("office_version"))
    terminal = run.get("liveness") == "terminal"
    out = {}
    for kind in RUN_KINDS:
        reason = serving["reason"]
        if reason is None and terminal:
            reason = f"run is {run.get('phase')}"
        if reason is None and kind in ("resume_run", "attach_run") and launcher_reason:
            reason = launcher_reason
        out[kind] = {"allowed": reason is None, "reason": reason}
    out["runtime"] = {"version": serving["version"], "read_only": not serving["controls"]}
    return out
