"""Run the `office` CLI for a web command and map its exit to a receipt outcome.

Run-scoped commands run in the run's checkout so the frontdoor pins the
runtime; machine-level ones run the runtime module of this process. A clean
exit is `completed`; an exit Office reports as a refusal is `failed`; a
timeout, signal or launch failure is `unknown` (the effect may have happened).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from office import frontdoor

TIMEOUT = 300.0
# Exit codes an OfficeError (Refused, Usage, RuntimeUnavailable, ...) returns.
REFUSAL_EXITS = range(1, 64)


@dataclass
class Outcome:
    status: str  # completed | failed | unknown
    result: dict
    error: str | None = None


def office_argv() -> list[str]:
    argv, _ = frontdoor.current_argv()
    return argv


def _env(override: dict | None = None) -> dict:
    _, extra = frontdoor.current_argv()
    env = dict(os.environ)
    env.update(extra)
    env.update(override or {})
    for key in [k for k in env if k.startswith(("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID",
                                                  "OFFICE_ROLE", "OFFICE_FRONT_DOOR_HOPS"))]:
        del env[key]  # the web service acts as the operator, never as a dispatch
    return env


def _tail(text: str | None, n: int = 2000) -> str:
    return (text or "")[-n:]


Runner = Callable[..., subprocess.CompletedProcess]


class Executor:
    """`run(args, cwd)` executes `office <args>`; `runner` is injectable for tests."""

    def __init__(self, runner: Runner = subprocess.run, timeout: float = TIMEOUT, env: dict | None = None):
        self.runner, self.timeout, self.env = runner, timeout, dict(env or {})

    def run(self, args: Sequence[str], cwd: str | Path | None) -> Outcome:
        argv = [*office_argv(), *args]
        where = str(cwd) if cwd and Path(cwd).is_dir() else None
        try:
            proc = self.runner(argv, cwd=where, capture_output=True, text=True, timeout=self.timeout,
                               env=_env(self.env))
        except subprocess.TimeoutExpired:
            return Outcome("unknown", {"args": list(args)}, f"office {args[0]} did not finish in {self.timeout:g}s")
        except OSError as exc:
            return Outcome("unknown", {"args": list(args)}, f"could not run office: {type(exc).__name__}")
        result = {"args": list(args), "exit_code": proc.returncode, "stdout": _tail(proc.stdout)}
        if proc.returncode == 0:
            return Outcome("completed", result)
        if proc.returncode in REFUSAL_EXITS:
            return Outcome("failed", result, _tail(proc.stdout or proc.stderr, 500).strip() or "office refused")
        return Outcome("unknown", result, f"office exited {proc.returncode}")
