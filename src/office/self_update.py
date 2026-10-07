"""Update the installed Auto Office runtime from its latest release."""
from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from office import version
from office.result import Result


PYPI_JSON = "https://pypi.org/pypi/auto-office/json"


def _latest_release(timeout: float = 5) -> str:
    request = urllib.request.Request(PYPI_JSON, headers={"User-Agent": "auto-office-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(json.load(response)["info"]["version"])


def _release(ver: str) -> tuple[int, ...]:
    """Compare public release numbers while ignoring local checkout metadata."""
    return version.release_key(ver)


def _source_checkout() -> Path | None:
    source = version.SOURCE_ROOT if (version.SOURCE_ROOT / "pyproject.toml").is_file() else version.install_source()
    source = Path(source) if source else None
    return source if source and (source / ".git").exists() and (source / "pyproject.toml").is_file() else None


def _upstream_distance(source: Path) -> tuple[int, int] | None:
    """Return (upstream-ahead, local-ahead), or None without a usable upstream."""
    upstream = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        capture_output=True, text=True, timeout=10)
    if upstream.returncode:
        return None
    remote_branch = upstream.stdout.strip()
    remote, _, branch = remote_branch.partition("/")
    if not remote or not branch:
        return None
    fetched = subprocess.run(["git", "-C", str(source), "fetch", "--quiet", remote, branch],
                             capture_output=True, text=True, timeout=20)
    if fetched.returncode:
        raise RuntimeError(fetched.stderr.strip() or "git fetch failed")
    distance = subprocess.run(["git", "-C", str(source), "rev-list", "--left-right", "--count",
                               "HEAD...FETCH_HEAD"], capture_output=True, text=True, timeout=10)
    if distance.returncode:
        raise RuntimeError(distance.stderr.strip() or "could not compare source with its upstream")
    local_ahead, upstream_ahead = (int(value) for value in distance.stdout.split())
    return upstream_ahead, local_ahead


def check() -> Result:
    current = version.current()
    lines = []
    available = False
    source = _source_checkout()
    diverged = False
    if source:
        try:
            distance = _upstream_distance(source)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            lines.append(f"could not check Auto Office source upstream: {exc}")
        else:
            if distance and distance[0] and distance[1]:
                lines.append(f"source checkout diverged: {distance[1]} local and {distance[0]} upstream commit(s) on {source}; "
                             "office update cannot fast-forward it")
                diverged = True
            elif distance and distance[0]:
                lines.append(f"source update available: {distance[0]} upstream commit(s) on {source}")
                available = True
            elif distance == (0, 0):
                lines.append(f"Auto Office source checkout is current with its upstream: {source}")
            elif distance is None:
                lines.append(f"Auto Office source checkout has no tracked upstream to check: {source}")
    else:
        try:
            latest = _latest_release()
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            lines.append(f"could not check latest Auto Office release: {exc}")
        else:
            if _release(latest) > _release(current):
                lines.append(f"update available: Auto Office {current} → {latest}")
                available = True
            else:
                lines.append(f"Auto Office {current} is current with PyPI (latest: {latest})")

    if available:
        return Result(lines=lines, next="ask the user during intake whether to run office update")
    if diverged:
        return Result(lines=lines, next="continue intake with the current runtime, or reconcile the source checkout before updating")
    if not lines:
        lines.append("no update source could be checked")
    return Result(lines=lines, next="continue intake")


def _run(argv: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def update() -> Result:
    source = _source_checkout()
    source_checkout = source is not None
    uv = shutil.which("uv")
    pipx = shutil.which("pipx")
    # Keep the active runtime under the tool manager that owns its environment.
    pipx_managed = (Path(sys.prefix) / "pipx_metadata.json").is_file()
    manager = "pipx" if pipx_managed else "uv" if uv else "pipx" if pipx else None
    executable = pipx if manager == "pipx" else uv
    if not executable:
        return Result(lines=["uv or pipx is required to update Auto Office"], exit_code=1,
                      next="install uv or pipx, then run office update")

    if source_checkout:
        dirty = subprocess.run(["git", "-C", str(source), "status", "--porcelain"],
                               capture_output=True, text=True)
        if dirty.returncode:
            return Result(lines=["could not inspect the Auto Office checkout", dirty.stderr.strip()], exit_code=1,
                          next="resolve the git error, then run office update")
        if dirty.stdout.strip():
            return Result(lines=["Auto Office checkout has uncommitted changes; update stopped"], exit_code=1,
                          next="commit or stash those changes, then run office update")
        pull = subprocess.run(["git", "-C", str(source), "pull", "--ff-only"],
                              capture_output=True, text=True)
        if pull.returncode:
            return Result(lines=["could not pull the latest Auto Office checkout", pull.stderr.strip()], exit_code=1,
                          next="resolve the git error, then run office update")
        origin = f"pulled Auto Office source at {source} and reinstalled it with {manager}"
    else:
        origin = f"upgraded the auto-office tool from PyPI with {manager}"

    # Ensure hook integration and the runtime registry point at the updated CLI.
    try:
        if source_checkout:
            extra = "[visual]" if importlib.util.find_spec("playwright") is not None else ""
            spec = f"auto-office{extra} @ {source.as_uri()}"
            install_command = ([executable, "install", "--force", spec] if manager == "pipx" else
                               [executable, "tool", "install", "--force", "--reinstall", "--no-cache", spec])
            _run(install_command)
        elif manager == "pipx":
            _run([executable, "upgrade", "auto-office"])
        else:
            _run([executable, "tool", "upgrade", "auto-office"])
        _run(["office", "install"])
        _run(["office", "doctor"])
    except subprocess.CalledProcessError as exc:
        return Result(lines=[origin, f"update follow-up command failed with exit code {exc.returncode}: {exc.cmd}"],
                      exit_code=exc.returncode or 1,
                      next="resolve the reported install or doctor issue, then run office doctor")
    return Result(lines=[origin, "Auto Office update and installation checks completed"],
                  next="continue with office status or office start")
