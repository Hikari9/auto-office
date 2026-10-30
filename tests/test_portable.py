"""`office.portable`: the OS differences Office hides (#915). Runs on every platform."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from office import portable, util

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
TREE = [sys.executable, "-c",
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"]


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not portable.pid_exists(pid):
            return True
        time.sleep(0.05)
    return False


def test_lock_excludes_a_second_handle_until_released(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a+") as first, open(path, "a+") as second:
        portable.lock_exclusive(first, blocking=False)
        with pytest.raises(BlockingIOError):
            portable.lock_exclusive(second, blocking=False)
        portable.unlock(first)
        portable.lock_exclusive(second, blocking=False)
        portable.unlock(second)


def test_lock_dies_with_its_handle(tmp_path):
    path = tmp_path / "x.lock"
    first = open(path, "a+")
    portable.lock_exclusive(first, blocking=False)
    first.close()
    with open(path, "a+") as second:
        portable.lock_exclusive(second, blocking=False)


def test_lock_accepts_a_raw_descriptor_and_leaves_contents_readable(tmp_path):
    import os
    path = tmp_path / "x.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"payload")
        portable.lock_exclusive(fd, blocking=False)
        assert path.read_bytes() == b"payload"  # a Windows lock must not cover real file bytes
        portable.unlock(fd)
    finally:
        os.close(fd)


def test_pid_exists_for_self_and_a_finished_process():
    assert portable.pid_exists(portable.os.getpid()) is True
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert _wait_gone(proc.pid)
    assert portable.pid_exists(0) is False and portable.pid_exists(None) is False


def test_probe_signal_zero_never_kills():
    proc = subprocess.Popen(SLEEPER)
    try:
        portable.kill(proc.pid, 0)  # on Windows os.kill(pid, 0) would terminate it
        time.sleep(0.2)
        assert proc.poll() is None
        assert util.pid_state(proc.pid) == util.ALIVE
    finally:
        proc.kill()
        proc.wait()


def test_kill_reports_a_missing_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert _wait_gone(proc.pid)
    with pytest.raises(ProcessLookupError):
        portable.kill(proc.pid, 0)
    assert util.pid_state(proc.pid) == util.DEAD


def test_process_start_is_stable_and_identifies_the_process():
    proc = subprocess.Popen(SLEEPER)
    try:
        first = util.process_start(proc.pid)
        assert first and util.process_start(proc.pid) == first
        assert util.process_is(proc.pid, first)
        assert not util.process_is(proc.pid, first + "x")
    finally:
        proc.kill()
        proc.wait()


def test_killpg_stops_the_whole_group_and_never_the_caller():
    proc = subprocess.Popen(TREE, stdout=subprocess.PIPE, text=True, **portable.session_kwargs())
    try:
        child = int(proc.stdout.readline())
        assert portable.getpgid(proc.pid) == proc.pid
        portable.killpg(proc.pid, portable.SIGKILL)
        proc.wait(timeout=10)
        assert _wait_gone(child), "a grandchild outlived its group"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert portable.pid_exists(portable.os.getpid()) is True


def test_killpg_term_asks_a_group_to_stop():
    proc = subprocess.Popen(SLEEPER, **portable.session_kwargs())
    try:
        portable.killpg(proc.pid, portable.signal.SIGTERM)
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_killpg_on_a_gone_group_raises_lookup_error():
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **portable.session_kwargs())
    proc.wait()
    assert _wait_gone(proc.pid)
    with pytest.raises(OSError):
        portable.killpg(proc.pid, portable.SIGKILL)


def test_open_flags_are_integers():
    assert all(isinstance(f, int) for f in (portable.O_BINARY, portable.O_NOFOLLOW, portable.O_NONBLOCK))
    if portable.IS_WINDOWS:
        assert portable.O_BINARY


def test_host_helpers():
    assert portable.node_name()
    averages = portable.loadavg()
    assert averages is None or len(averages) == 3
