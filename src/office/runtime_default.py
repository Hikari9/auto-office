"""Which Office version creates new runs.

A run keeps the runtime that created it until it is terminal; nothing here
touches an existing run. The 3.0 runtime and its `new_runs: "3.0"` rollback
were removed: that setting is refused.

  ~/.config/auto-office/config.yaml:
    runtime:
      new_runs: "3.2"   # optional pin to a 3.1+ line; unset means this installed runtime
"""
from __future__ import annotations

import os
import re

import yaml

from office import paths, version
from office.state import OfficeError


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
        raise OfficeError("new-runs-on-3.0",
                          f"runtime.new_runs={setting!r}: the Auto Office 3.0 runtime was removed and cannot create runs",
                          next_step="remove runtime.new_runs (or set it to a 3.1+ line)", exit_code=4)
    if not re.fullmatch(r"(?:3\.[1-9]\d*|(?:[4-9]|[1-9]\d)\.\d+)(\..*)?", setting):
        raise OfficeError("bad-runtime-setting", f"runtime.new_runs={setting!r} is not a 3.1+ line (a later MAJOR.MINOR line is accepted)", exit_code=2)
