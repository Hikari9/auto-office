"""Per-run controls follow the runtime that would serve the run."""
from __future__ import annotations

import pytest

from office.web.capabilities import RUN_KINDS, Resolver, for_run


def run(version, liveness="live", phase="executing"):
    return {"office_version": version, "liveness": liveness, "phase": phase}


PROBED = []


def resolver(registered=None, probe=True, current="3.3.3"):
    def probe_fn(argv):
        PROBED.append(argv)
        return probe
    return Resolver(registry=lambda line: registered if registered and registered["office_version"].startswith(line)
                    else None, probe=probe_fn, current=current)


def test_current_line_gets_every_control():
    caps = for_run(run("3.3.1"), resolver())
    assert all(caps[k]["allowed"] for k in RUN_KINDS)
    assert caps["runtime"] == {"version": "3.3.3", "read_only": False}


@pytest.mark.parametrize("version,line", [("3.0.4", "3.0"), ("3.1.9", "3.1"), ("3.2.2", "3.2"), (None, None)])
def test_older_lines_are_read_only_with_a_reason(version, line):
    caps = for_run(run(version), resolver())
    assert not any(caps[k]["allowed"] for k in RUN_KINDS)
    assert caps["runtime"]["read_only"] is True
    reason = caps["pause"]["reason"]
    assert (line in reason) if line else "legacy" in reason


def test_newer_registered_patch_is_probed_for_the_new_commands():
    newer = {"office_version": "3.3.9", "argv": ["/x/python", "-m", "office"]}
    PROBED.clear()
    ok = for_run(run("3.3.1"), resolver(newer, probe=True))
    assert PROBED and all(argv == tuple(newer["argv"]) for argv in PROBED)
    assert ok["pause"]["allowed"] and ok["runtime"]["version"] == "3.3.9"
    missing = for_run(run("3.3.1"), resolver(newer, probe=False))
    assert not missing["pause"]["allowed"] and "office queue" in missing["pause"]["reason"]


def test_other_process_line_without_registration_is_read_only():
    caps = for_run(run("3.3.1"), resolver(current="3.4.0"))
    assert not caps["pause"]["allowed"] and "no 3.3.x runtime" in caps["pause"]["reason"]


def test_terminal_run_and_missing_launcher():
    assert not for_run(run("3.3.3", "terminal", "closed"), resolver())["pause"]["allowed"]
    caps = for_run(run("3.3.3"), resolver(), launcher_reason="Herdr is not available")
    assert not caps["resume_run"]["allowed"] and caps["resume_run"]["reason"] == "Herdr is not available"
    assert caps["pause"]["allowed"]
    assert not for_run(None, resolver())["pause"]["allowed"]
