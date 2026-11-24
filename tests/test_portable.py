"""`office.portable`: the OS differences Office hides (#915). Runs on every platform."""
from __future__ import annotations

import contextlib
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


def test_shared_locks_coexist_and_exclude_an_exclusive_one(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a") as first, open(path, "a") as second, open(path, "a") as writer:
        portable.lock_shared(first, blocking=False)
        portable.lock_shared(second, blocking=False)
        with pytest.raises(BlockingIOError):
            portable.lock_exclusive(writer, blocking=False)
        portable.unlock(first)
        portable.unlock(second)
        portable.lock_exclusive(writer, blocking=False)
        with pytest.raises(BlockingIOError):
            portable.lock_shared(first, blocking=False)
        portable.unlock(writer)


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
    proc = portable.popen_group(SLEEPER)  # on Windows only a known group leader gets CTRL_BREAK
    try:
        time.sleep(0.5)  # let the interpreter install its console handler
        portable.killpg(proc.pid, portable.signal.SIGTERM)
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        portable.release_group(proc.pid)


def test_killpg_term_never_breaks_the_callers_console_for_a_non_leader():
    # CTRL_BREAK sent to an id that is not a group leader reaches every process on the console, this one too.
    proc = subprocess.Popen(SLEEPER)
    try:
        with contextlib.suppress(OSError):
            portable.killpg(proc.pid, portable.signal.SIGTERM)
        time.sleep(0.5)
        assert portable.pid_exists(portable.os.getpid())
    finally:
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


@pytest.mark.skipif(not portable.IS_WINDOWS, reason="the Windows process snapshot")
def test_windows_process_table_links_a_tree_and_matches_recorded_starts():
    proc = subprocess.Popen(TREE, stdout=subprocess.PIPE, text=True, **portable.session_kwargs())
    try:
        child = int(proc.stdout.readline())
        rows = {pid: (ppid, start) for pid, ppid, start, _name in portable.windows_process_table()}

        def ancestors(pid):
            while pid in rows and rows[pid][0]:
                pid = rows[pid][0]
                yield pid

        # a venv's python.exe may be a launcher with the real interpreter as its child
        assert portable.os.getpid() in ancestors(proc.pid)
        assert proc.pid in ancestors(child)
        assert rows[child][1] == util.process_start(child)
    finally:
        portable.killpg(proc.pid, portable.SIGKILL)
        proc.wait(timeout=10)


ORPHANER = [sys.executable, "-c",
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"]  # the leader exits at once and leaves its child behind


def test_a_group_outlives_its_leader_until_killpg_ends_every_member():
    proc = portable.popen_group(ORPHANER, stdout=subprocess.PIPE, text=True)
    try:
        orphan = int(proc.stdout.readline())
        proc.wait(timeout=10)
        portable.killpg(proc.pid, 0)  # the leader is gone, its group is not
        portable.killpg(proc.pid, portable.SIGKILL)
        assert _wait_gone(orphan), "an orphan outlived its group"
        if portable.IS_WINDOWS:
            with pytest.raises(ProcessLookupError):
                portable.killpg(proc.pid, 0)
    finally:
        portable.release_group(proc.pid)


@pytest.mark.skipif(not portable.IS_WINDOWS, reason="Windows runs a .cmd shim only by its full path")
def test_resolve_argv_runs_a_cmd_shim_by_its_bare_name(tmp_path):
    (tmp_path / "shimmed.cmd").write_text("@echo shim %1\r\n")
    env = {**portable.os.environ, "PATH": f"{tmp_path}{portable.os.pathsep}{portable.os.environ['PATH']}"}
    argv = portable.resolve_argv(["shimmed", "ok"], env)
    assert argv[0].lower().endswith("shimmed.cmd") and argv[1:] == ["ok"]
    assert subprocess.run(argv, env=env, capture_output=True, text=True).stdout.strip() == "shim ok"


def test_resolve_argv_leaves_paths_and_posix_alone():
    assert portable.resolve_argv(["./x", "a"]) == ["./x", "a"]
    assert portable.resolve_argv([]) == []
    if not portable.IS_WINDOWS:
        assert portable.resolve_argv(["git", "status"]) == ["git", "status"]


def test_rmtree_removes_a_read_only_file(tmp_path):
    tree = tmp_path / "tree"
    (tree / "objects").mkdir(parents=True)
    obj = tree / "objects" / "ab"
    obj.write_text("x")
    obj.chmod(0o444)  # git writes its objects read-only
    portable.rmtree(tree)
    assert not tree.exists()


@pytest.mark.skipif(portable.IS_WINDOWS, reason="POSIX keeps plain rmtree semantics")
def test_rmtree_on_posix_never_touches_a_symlinks_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(0o444)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    portable.rmtree(tree)
    assert not tree.exists() and outside.read_text() == "keep" and oct(outside.stat().st_mode & 0o777) == "0o444"


def test_orphan_filter_drops_children_born_after_their_parent_was_gone():
    seen = {10: "1000"}
    gone: dict[int, int] = {}
    if not portable.IS_WINDOWS:
        assert portable.orphan_filter(gone, seen, {})(10, 11) is True  # POSIX reparents orphans
        return
    now = portable.windows_ticks_now()
    started = {11: str(now - 10_000_000), 12: str(now + 10_000_000)}  # pid 10 is absent: exited (or reused)
    follows = portable.orphan_filter(gone, seen, started)
    assert 10 in gone
    assert follows(10, 11) and not follows(10, 12)  # 12 started after 10 was gone: a reuser's child
    assert follows(99, 12)  # a parent never recorded as gone is followed as before
    started[10] = "1000"
    assert portable.orphan_filter({}, seen, started)(10, 12)  # alive with its recorded start: followed


def test_python3_shim_stands_in_only_for_a_missing_python3(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    real = {"python": str(tmp_path / "py" / "python.exe")}
    monkeypatch.setattr(portable, "_real_exe", lambda name, path: real.get(name))
    shim = portable._python3_shim("ignored")
    assert shim and (tmp_path / "py").as_posix() in (portable.Path(shim) / "python3").read_text()
    assert portable._python3_shim("ignored") == shim  # reused, not rewritten per call
    real["python3"] = "python3"
    assert portable._python3_shim("ignored") is None
    real.clear()
    assert portable._python3_shim("ignored") is None


def test_store_stub_python_does_not_count_as_real(monkeypatch):
    monkeypatch.setattr(portable.shutil, "which",
                        lambda name, path=None: r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python3.exe")
    assert portable._real_exe("python3", "") is None


def test_execve_hands_over_arguments_and_exit_code_and_runs_nothing_after():
    """`os.execve` on Windows splits an argument at its space, loses the exit code and can crash the caller."""
    code = ("import os, sys\n"
            "from office import portable\n"
            "portable.execve([sys.executable, '-c', 'import sys; print(sys.argv[1]); sys.exit(7)', 'a b'], dict(os.environ))\n"
            "print('returned')\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert (proc.returncode, proc.stdout.split()) == (7, ["a", "b"]), (proc.returncode, proc.stdout, proc.stderr)


def test_owns_group_is_a_held_windows_job_only():
    proc = portable.popen_group([sys.executable, "-c", "pass"])
    try:
        proc.wait(timeout=30)
        assert portable.owns_group(proc.pid) is portable.IS_WINDOWS  # still held after the leader exited
        assert portable.owns_group(proc.pid + 1_000_000) is False
    finally:
        portable.release_group(proc.pid)
    assert portable.owns_group(proc.pid) is False


@pytest.mark.skipif(not portable.IS_WINDOWS, reason="Windows suspended job launch")
@pytest.mark.parametrize("stage", ["create", "configure", "assign", "resume"])
def test_job_setup_failure_never_runs_suspended_child(tmp_path, monkeypatch, stage):
    marker = tmp_path / "ran"
    spawned = []
    real = subprocess.Popen

    def capture(*args, **kwargs):
        proc = real(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(portable.subprocess, "Popen", capture)
    if stage == "create":
        monkeypatch.setattr(portable._k32, "CreateJobObjectW", lambda *a: 0)
    elif stage == "configure":
        monkeypatch.setattr(portable._k32, "SetInformationJobObject", lambda *a: 0)
    elif stage == "assign":
        monkeypatch.setattr(portable._k32, "AssignProcessToJobObject", lambda *a: 0)
    else:
        monkeypatch.setattr(portable._ntdll, "NtResumeProcess", lambda *a: 1)
    before = dict(portable._JOBS)
    with pytest.raises(OSError):
        portable.popen_group([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)])
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert not marker.exists() and portable._JOBS == before


@pytest.mark.skipif(not portable.IS_WINDOWS, reason="Windows job query")
def test_owned_job_query_failure_never_falls_back_to_a_pid(monkeypatch):
    proc = portable.popen_group(SLEEPER)
    try:
        with monkeypatch.context() as m:
            m.setattr(portable._k32, "QueryInformationJobObject", lambda *a: 0)
            m.setattr(portable, "_taskkill", lambda *a, **kw: pytest.fail("numeric PID fallback"))
            with pytest.raises(PermissionError):
                portable.killpg(proc.pid, portable.SIGKILL)
            assert not portable.finish_group(proc.pid)
        assert portable.finish_group(proc.pid)
        proc.wait(timeout=10)
    finally:
        portable.release_group(proc.pid)
