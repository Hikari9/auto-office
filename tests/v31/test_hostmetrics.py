"""Host probes: injected output in, `{status, value, unit, source, observed_at}` out."""
from __future__ import annotations

import subprocess

import pytest

from office import hostmetrics

KEYS = {"status", "value", "unit", "source", "observed_at"}

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           20000.
Pages speculative:                         5000.
Pages wired down:                         50000.
Pages purgeable:                           5000.
"""

MEMINFO = """MemTotal:       16000000 kB
MemFree:         1000000 kB
MemAvailable:    4000000 kB
"""


def _fail(*_a, **_k):
    raise subprocess.CalledProcessError(1, "probe")


def test_cpu_is_load_per_core():
    s = hostmetrics.cpu(loadavg=lambda: (6.0, 1.0, 1.0), cores=lambda: 8)
    assert set(s) == KEYS and s["status"] == "ok" and s["value"] == 0.75 and s["unit"] == "load_per_core"


@pytest.mark.parametrize("loadavg,cores", [(lambda: (_ for _ in ()).throw(OSError()), lambda: 8),
                                           (lambda: (1.0, 1.0, 1.0), lambda: None)])
def test_cpu_unavailable_when_a_probe_fails(loadavg, cores):
    s = hostmetrics.cpu(loadavg=loadavg, cores=cores)
    assert set(s) == KEYS and s["status"] == "unavailable" and s["value"] is None


def test_ram_darwin_from_sysctl_and_vm_stat():
    total = 1_000_000 * 16384

    def run(argv):
        return {"sysctl": f"{total}\n", "vm_stat": VM_STAT}[argv[0]]

    s = hostmetrics.ram(platform="darwin", run=run)
    # available = free + inactive + speculative + purgeable = 40000 pages of 1_000_000
    assert set(s) == KEYS and s["status"] == "ok" and s["value"] == 0.96
    assert s["source"] == "sysctl/vm_stat" and s["unit"] == "fraction_used"


def test_ram_linux_from_meminfo():
    s = hostmetrics.ram(platform="linux", read=lambda path: MEMINFO)
    assert s["status"] == "ok" and s["value"] == 0.75 and s["source"] == "/proc/meminfo"


@pytest.mark.parametrize("platform,kw", [
    ("darwin", {"run": _fail}),
    ("darwin", {"run": lambda argv: "garbage\n"}),
    ("linux", {"read": lambda p: (_ for _ in ()).throw(FileNotFoundError(p))}),
    ("linux", {"read": lambda p: "MemFree: 1 kB\n"}),
    ("sunos", {}),
])
def test_ram_unavailable_when_a_probe_fails(platform, kw):
    s = hostmetrics.ram(platform=platform, **kw)
    assert set(s) == KEYS and s["status"] == "unavailable" and s["value"] is None


def test_process_cpu_and_rss_via_ps():
    seen = []

    def run(argv):
        seen.append(argv)
        return " 12.5 20480\n"

    s = hostmetrics.process(321, run=run)
    assert seen == [["ps", "-o", "%cpu=,rss=", "-p", "321"]]
    assert s["cpu"]["value"] == 12.5 and s["cpu"]["unit"] == "percent"
    assert s["rss"]["value"] == 20480 and s["rss"]["unit"] == "kib"
    assert set(s["cpu"]) == KEYS and set(s["rss"]) == KEYS


@pytest.mark.parametrize("run", [_fail, lambda argv: ""])
def test_process_unavailable_for_a_gone_pid(run):
    s = hostmetrics.process(321, run=run)
    assert s["cpu"]["status"] == s["rss"]["status"] == "unavailable"
