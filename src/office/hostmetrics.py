"""Host pressure samples for the scheduler.

Every sample is `{status, value, unit, source, observed_at}`. A probe that
fails yields `status: unavailable` with `value: None`: an unmeasured host is
reported as unmeasured, never as idle. Probes are injectable for tests.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable

from office.util import now_iso

Probe = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=5).stdout


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _sample(status: str, value, unit: str, source: str) -> dict:
    return {"status": status, "value": value, "unit": unit, "source": source, "observed_at": now_iso()}


def _unavailable(unit: str, source: str) -> dict:
    return _sample("unavailable", None, unit, source)


def cpu(*, loadavg: Callable[[], tuple] = os.getloadavg, cores: Callable[[], int | None] = os.cpu_count) -> dict:
    """1-minute load average per core (1.0 means every core busy)."""
    source = "loadavg/cpu_count"
    try:
        load, n = loadavg()[0], cores()
    except (OSError, AttributeError, IndexError, TypeError):
        return _unavailable("load_per_core", source)
    if not n:
        return _unavailable("load_per_core", source)
    return _sample("ok", round(load / n, 3), "load_per_core", source)


def ram(*, platform: str | None = None, run: Probe = _run, read: Callable[[str], str] = _read) -> dict:
    """Fraction of physical memory in use (0..1)."""
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            return _ram_darwin(run)
        if platform.startswith("linux"):
            return _ram_linux(read)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, ZeroDivisionError):
        pass
    return _unavailable("fraction_used", "sysctl/vm_stat" if platform == "darwin" else "/proc/meminfo")


def _ram_darwin(run: Probe) -> dict:
    total = int(run(["sysctl", "-n", "hw.memsize"]).strip())
    text = run(["vm_stat"])
    page = int(re.search(r"page size of (\d+) bytes", text).group(1)) if "page size of" in text else 4096
    pages = {m.group(1).strip().lower(): int(m.group(2))
             for m in re.finditer(r"^(.+?):\s+(\d+)\.?\s*$", text, re.M)}
    available = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages speculative",
                                              "pages purgeable")) * page
    if total <= 0:
        raise ValueError("hw.memsize is not positive")
    return _sample("ok", round(max(0.0, 1 - available / total), 3), "fraction_used", "sysctl/vm_stat")


def _ram_linux(read: Callable[[str], str]) -> dict:
    info = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            info[key.strip()] = int(rest.split()[0])
    total = info["MemTotal"]
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return _sample("ok", round(max(0.0, 1 - available / total), 3), "fraction_used", "/proc/meminfo")


def process(pid: int, *, run: Probe = _run) -> dict:
    """Per-pid CPU percent and RSS (KiB), via `ps`."""
    source = "ps"
    try:
        out = run(["ps", "-o", "%cpu=,rss=", "-p", str(int(pid))]).split()
        pct, rss = float(out[0]), int(out[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"cpu": _unavailable("percent", source), "rss": _unavailable("kib", source)}
    return {"cpu": _sample("ok", pct, "percent", source), "rss": _sample("ok", rss, "kib", source)}


def host(**probes) -> dict:
    return {"cpu": cpu(), "ram": ram(**probes)}
