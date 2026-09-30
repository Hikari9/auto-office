"""Version-pinned runtime dispatch.

The globally installed `office` resolves the target run first. When that run
is pinned to a different Office version, the command is re-executed under the
registered runtime for exactly that version. There is no fallback to the
current runtime: an old packet is never reinterpreted by new code.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from office import paths, version
from office.state import OfficeError
from office.util import atomic_write_json, now_iso

HOP_ENV = "OFFICE_FRONT_DOOR_HOPS"


class RuntimeUnavailable(OfficeError):
    exit_code = 5


def registry_path(ver: str) -> Path:
    safe = ver.replace("/", "_")
    return paths.runtimes_dir() / f"{safe}.json"


def current_argv() -> tuple[list[str], dict]:
    env = {}
    src = Path(__file__).resolve().parents[1]
    if (src / "office").is_dir() and src.name == "src":
        env["PYTHONPATH"] = str(src)
    if "PYTHONUSERBASE" in os.environ:
        env["PYTHONUSERBASE"] = os.environ["PYTHONUSERBASE"]
    return [sys.executable, "-m", "office"], env


def register_current() -> dict:
    ver = version.current()
    argv, env = current_argv()
    entry = {"office_version": ver, "argv": argv, "env": env, "registered_at": now_iso()}
    atomic_write_json(registry_path(ver), entry)
    return entry


def registered(ver: str) -> dict | None:
    p = registry_path(ver)
    if not p.is_file():
        return None
    try:
        entry = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    exe = (entry.get("argv") or [None])[0]
    if not exe or not Path(exe).exists():
        return None
    return entry


def list_registered() -> list[dict]:
    out = []
    d = paths.runtimes_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def ensure_runtime(run: dict) -> None:
    """Raise unless this process may operate on `run`, re-executing under the
    pinned runtime when one is registered. Returns only when versions match."""
    pinned = run.get("office_version")
    if not pinned:
        raise RuntimeUnavailable("unpinned-run", f"run {run['id'][:8]} has no office_version",
                                 next_step="office doctor")
    if pinned == version.current():
        return
    hops = int(os.environ.get(HOP_ENV, "0"))
    entry = registered(pinned)
    if entry is None or hops >= 1:
        raise RuntimeUnavailable(
            "pinned-runtime-unavailable",
            f"run {run['id'][:8]} is pinned to Auto Office {pinned}; this is {version.current()}",
            scope=f"run {run['id'][:8]}",
            preserved="all run state; nothing was changed",
            next_step=(f"install that exact runtime and register it (office install from it), "
                       f"or check office doctor; the run never migrates to {version.current()}"),
            data={"pinned": pinned, "current": version.current()})
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    env[HOP_ENV] = str(hops + 1)
    argv = list(entry["argv"]) + sys.argv[1:]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(argv[0], argv, env)
