"""#507: avoid a second server on an occupied port and name its owner."""
from pathlib import Path
from types import SimpleNamespace

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from conftest import approved_run
from office import db, paths, state, visual
from office.util import now_iso


def test_port_conflict_names_pid_command_and_possible_finished_dispatch(monkeypatch, tmp_path):
    class Conn:
        def execute(self, sql, params):
            assert "ended_at IS NOT NULL" in sql
            assert params == ("R1", str(tmp_path))
            return self
        def fetchone(self):
            return (1,)

    class OpenSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(visual.socket, "create_connection", lambda *args, **kwargs: OpenSocket())
    def fake_run(args, **kwargs):
        if args[0] == "ps":
            return SimpleNamespace(returncode=0, stdout="/usr/local/bin/node\n")
        if "-iTCP:8002" in args:
            return SimpleNamespace(returncode=0, stdout="p4567\n")
        return SimpleNamespace(returncode=0, stdout=f"p4567\nn{tmp_path}\n")
    monkeypatch.setattr(visual.subprocess, "run", fake_run)
    monkeypatch.setattr(visual, "_office_leftover", lambda *a: None)
    result = visual._capture_port_conflict("http://127.0.0.1:8002/", Conn(), {"id": "R1"}, tmp_path)
    assert "port 8002" in result and "PID 4567" in result and "(node)" in result
    assert "ended Office executor" in result and "kill 4567" in result


def test_visual_capture_blocks_before_spawning_second_server(monkeypatch, tmp_path):
    monkeypatch.setattr(visual.paths, "run_dir", lambda *_: tmp_path)
    monkeypatch.setattr(visual, "_capture_port_conflict", lambda *args: "port 8002 occupied by PID 123")
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)
    monkeypatch.setattr(visual, "LOCAL_ORIGIN", visual.LOCAL_ORIGIN)
    monkeypatch.setattr(visual.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not start second server")))
    from office import submit
    monkeypatch.setattr(submit, "matches_revision", lambda *args: True)
    run = {"id": "R1", "repo_root": str(tmp_path)}
    task = {"id": "T1", "visual": {"url": "http://127.0.0.1:8002/", "start": "vite --port 8002 --strictPort"}}
    result = visual.capture_all(None, run, task, {"id": "V1", "commit_sha": "abc"}, {"id": "G1", "recaptures": 0}, tmp_path)
    assert result["evidence_status"] == "CAPTURE_BLOCKED"
    assert "PID 123" in result["cause"]


# ------------------------------------------------------------------ real processes

LEADER = """
import subprocess, sys
port, mode = sys.argv[1], sys.argv[2]
kw = {"start_new_session": True} if mode == "setsid" else {}
child = subprocess.Popen([sys.executable, "-m", "http.server", port, "--bind", "127.0.0.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
print(child.pid, flush=True)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bound(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@pytest.fixture
def leftover(tmp_path):
    """An "agent" leads its own process group, starts a server and exits: the server survives."""
    started = []

    def make(mode="group"):
        port = _free_port()
        leader = subprocess.Popen([sys.executable, "-c", LEADER, str(port), mode], stdout=subprocess.PIPE, text=True,
                                  start_new_session=True)
        server = int(leader.stdout.readline())
        leader.wait(timeout=10)
        started.append(server)
        for _ in range(100):
            if _bound(port):
                break
            time.sleep(0.05)
        assert _bound(port)
        return {"port": port, "leader": leader.pid, "server": server}
    yield make
    for pid in started:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def _executor(env, run, worktree, *, pgid, started, ended, did="DX1"):
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("INSERT INTO dispatches(id, run_id, role, started_at, ended_at, worktree, status) "
                        "VALUES(?,?,?,?,?,?,?)", (did, run["id"], "executor", started, ended, str(worktree),
                                                  "ended" if ended else "running"))
    finally:
        con.close()
    ddir = paths.run_dir(run["id"]) / "dispatches" / did
    ddir.mkdir(parents=True, exist_ok=True)
    if pgid:
        (ddir / "agent.pgid").write_text(str(pgid))


def _run(env):
    approved_run(env)
    con = env.con()
    try:
        return con, state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    finally:
        pass


def _window(seconds=60):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return ((now - timedelta(seconds=seconds)).isoformat(timespec="seconds"),
            (now + timedelta(seconds=seconds)).isoformat(timespec="seconds"))


@pytest.mark.approved
def test_leftover_of_an_ended_headless_executor_is_stopped_and_recorded(env, leftover, tmp_path):
    con, run = _run(env)
    lo, hi = _window()
    srv = leftover()
    _executor(env, run, tmp_path, pgid=srv["leader"], started=lo, ended=hi)
    assert visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path) is None
    assert not _bound(srv["port"])
    ev = con.execute("SELECT summary FROM events WHERE kind='visual.reclaimed'").fetchone()
    assert ev and f"process group {srv['leader']}" in ev[0] and "DX1" in ev[0]


@pytest.mark.approved
def test_unrelated_listener_is_reported_never_signalled(env, leftover, tmp_path):
    con, run = _run(env)
    lo, hi = _window()
    srv = leftover()
    _executor(env, run, tmp_path, pgid=999999, started=lo, ended=hi)  # some other group
    cause = visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path)
    assert f"PID {srv['server']}" in cause and "not proven" in cause
    assert _bound(srv["port"]) and _alive(srv["server"])


@pytest.mark.approved
def test_a_listener_started_outside_the_dispatch_window_is_not_claimed(env, leftover, tmp_path):
    # The recorded group id matches, but the dispatch ended before the listener started: a reused id.
    con, run = _run(env)
    srv = leftover()
    _executor(env, run, tmp_path, pgid=srv["leader"], started="2026-01-01T00:00:00+00:00",
              ended="2026-01-01T00:10:00+00:00")
    cause = visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path)
    assert cause and _bound(srv["port"]) and _alive(srv["server"])


@pytest.mark.approved
def test_a_leftover_is_kept_while_a_live_executor_works_in_that_worktree(env, leftover, tmp_path):
    con, run = _run(env)
    lo, hi = _window()
    srv = leftover()
    _executor(env, run, tmp_path, pgid=srv["leader"], started=lo, ended=hi)
    _executor(env, run, tmp_path, pgid=None, started=lo, ended=None, did="DX2")
    cause = visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path)
    assert "not stopped because executor dispatch DX2 is live" in cause
    assert _bound(srv["port"]) and _alive(srv["server"])


@pytest.mark.approved
def test_a_descendant_that_left_the_agent_group_is_reported(env, leftover, tmp_path):
    con, run = _run(env)
    lo, hi = _window()
    srv = leftover("setsid")
    _executor(env, run, tmp_path, pgid=srv["leader"], started=lo, ended=hi)
    cause = visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path)
    assert cause and _bound(srv["port"]) and _alive(srv["server"])


@pytest.mark.approved
def test_missing_lsof_reports_a_generic_blocker(env, leftover, tmp_path, monkeypatch):
    con, run = _run(env)
    srv = leftover()
    real = visual.subprocess.run

    def no_lsof(argv, **kw):
        if argv[0] == "lsof":
            raise FileNotFoundError("lsof")
        return real(argv, **kw)
    monkeypatch.setattr(visual.subprocess, "run", no_lsof)
    cause = visual._capture_port_conflict(f"http://127.0.0.1:{srv['port']}/", con, run, tmp_path)
    assert f"port {srv['port']} is already bound" in cause and _alive(srv["server"])


def test_free_port_is_no_conflict(tmp_path):
    assert visual._capture_port_conflict(f"http://127.0.0.1:{_free_port()}/", None, {"id": "R1"}, tmp_path) is None
