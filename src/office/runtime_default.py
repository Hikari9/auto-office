"""Which Office version creates new runs (staged cutover and rollback).

Cutover and rollback change only this default. A run keeps the runtime that
created it until it is terminal; nothing here touches an existing run.

  ~/.config/auto-office/config.yaml:
    runtime:
      new_runs: "3.0"   # roll new runs back to the retained v3 runtime; unset (or any
                        # 3.1+ value) means this installed runtime
"""
from __future__ import annotations

import os
import re

import yaml

from office import legacy, paths, version
from office.state import OfficeError

# The last Auto Office 3.0 commit on main before the 3.1 implementation.
LEGACY_V3_FINAL = "3bce9b1ae0896d0d8e474d81eb30c7ae11da1e63"


def new_runs_setting() -> str:
    env = os.environ.get("OFFICE_NEW_RUNS")
    if env:
        return env
    p = paths.user_config_path()
    if p.is_file():
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            value = ((cfg.get("runtime") or {}).get("new_runs"))
            if value:
                return str(value)
        except (OSError, yaml.YAMLError):
            pass
    return version.release_line(version.current())


def require_new_run_runtime() -> None:
    setting = new_runs_setting()
    if setting.startswith("3.0"):
        retained = legacy.retained_runtime(LEGACY_V3_FINAL)
        where = f"python3 {retained}/scripts/office_runtime.py start ..." if retained else "the retained v3 runtime (office doctor)"
        raise OfficeError("new-runs-on-3.0",
                          f"new runs are configured to use Auto Office 3.0 (rollback); {version.current()} does not create them",
                          next_step=f"start with {where}, or remove runtime.new_runs", exit_code=4)
    if not re.fullmatch(r"3\.[1-9]\d*(\..*)?", setting):
        raise OfficeError("bad-runtime-setting", f"runtime.new_runs={setting!r} is not 3.0 or 3.1+", exit_code=2)
