"""Version-pinned runtime dispatch.

The globally installed `office` resolves the target run first. A run is pinned
to a MAJOR.MINOR release line; the newest installed PATCH on that line serves
it. When this process is on another line, or a newer patch of the run's line
is registered, the command is re-executed under that runtime. A run never
crosses a MINOR or MAJOR implicitly: that takes `office upgrade`.
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


def newest_on_line(line: str) -> dict | None:
    """The registered runtime with the highest patch on `line`, if any."""
    best = None
    for entry in list_registered():
        ver = entry.get("office_version") or ""
        if not version.same_line(ver, line) or registered(ver) is None:
            continue
        if best is None or version.release_key(ver) > version.release_key(best["office_version"]):
            best = entry
    return best


def installed_lines() -> list[str]:
    """Release lines this machine can serve, newest first."""
    lines = {version.release_line(version.current())}
    for entry in list_registered():
        ver = entry.get("office_version") or ""
        if ver and registered(ver) is not None:
            lines.add(version.release_line(ver))
    return sorted(lines, key=version.release_key, reverse=True)


def ensure_runtime(run: dict) -> None:
    """Raise unless this process may operate on `run`, re-executing under the
    newest registered patch of the run's release line when that is not this
    process. Returns only when this runtime serves the run."""
    pinned = run.get("office_version")
    if not pinned:
        raise RuntimeUnavailable("unpinned-run", f"run {run['id'][:8]} has no office_version",
                                 next_step="office doctor")
    line = version.release_line(pinned)
    cur = version.current()
    best = newest_on_line(line)
    if version.same_line(cur, line) and (best is None or version.release_key(best["office_version"])
                                          <= version.release_key(cur)):
        return
    hops = int(os.environ.get(HOP_ENV, "0"))
    if best is None or hops >= 1:
        if version.same_line(cur, line):
            return  # the newer patch is registered but unreachable; this patch is compatible
        raise RuntimeUnavailable(
            "pinned-runtime-unavailable",
            f"run {run['id'][:8]} is on Auto Office {line}; this is {cur}",
            scope=f"run {run['id'][:8]}",
            preserved="all run state; nothing was changed",
            next_step=(f"office upgrade {run['id'][:8]} to move it to {version.release_line(cur)}, "
                       f"or install a {line}.x runtime and register it (office install from it)"),
            data={"pinned": line, "current": cur})
    entry = best
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    env[HOP_ENV] = str(hops + 1)
    argv = list(entry["argv"]) + sys.argv[1:]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(argv[0], argv, env)
