"""`office revoke` stops the live dispatch: SIGTERM to its supervisor and agent
process groups, its herdr pane closed, the end recorded as `signal`."""
from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from conftest import approved_run
from test_herdr_agent_launch import BUSY, _calls, launch_in_herdr


def _dispatch(env, task="T1"):
    from office import state
    con = env.con()
    try:
        did = con.execute("SELECT id FROM dispatches WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task,)).fetchone()[0]
        return state.get_dispatch(con, did)
    finally:
        con.close()


def _until(pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("condition not reached")


def _lease_revoked(env) -> bool:
    con = env.con()
    try:
        return bool(con.execute("SELECT 1 FROM leases WHERE task_id='T1' AND revoke_reason='test stop'").fetchone())
    finally:
        con.close()


def _reap_when_done(pid: int) -> None:
    """The supervisor is a child of this process, so a finished one lingers as a
    zombie that still answers kill 0, and `office revoke` would wait out its
    whole grace period for it. Reap it the moment it exits."""
    threading.Thread(target=os.waitpid, args=(pid, 0), daemon=True).start()


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


@pytest.mark.approved
def test_revoke_stops_a_headless_dispatch(env, monkeypatch):
    approved_run(env, executor=[{"sleep": 60}])
    env.office("dispatch", "T1", env={"OFFICE_LAUNCHER": "auto"}, check=0)
    from office import paths
    d = _until(lambda: (lambda d: d if d["status"] == "running" else None)(_dispatch(env)))
    pgid_file = paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "agent.pgid"
    agent = int(_until(lambda: pgid_file.is_file() and pgid_file.read_text().strip()))
    sup = d["pid"]
    _reap_when_done(sup)
    code, out = env.office("revoke", "T1", "--reason", "test stop")
    assert code == 0, out
    assert "SIGTERM" in out
    d = _until(lambda: (lambda d: d if d["ended_at"] else None)(_dispatch(env)))
    assert d["terminal_classification"] == "signal" and d["signal"] == signal.SIGTERM
    _until(lambda: _dead(agent) and _dead(sup))
    assert _lease_revoked(env)


@pytest.mark.approved
def test_revoke_stops_a_herdr_agent_dispatch(env, monkeypatch):
    state_file, run, _, _, res = launch_in_herdr(env, monkeypatch, gets=["working"], reads=[BUSY], adapter="claude",
                                                 model="fake-model", effort="high", real_watcher=True,
                                                 settings={"OFFICE_HERDR_POLL": "0.2"})
    assert res["launcher"] == "herdr", res
    _reap_when_done(res["watcher_pid"])
    _until(lambda: (lambda d: d if d["status"] == "running" and d["pid"] == res["watcher_pid"] else None)(_dispatch(env)))
    code, out = env.office("revoke", "T1", "--reason", "test stop")
    assert code == 0, out
    d = _until(lambda: (lambda d: d if d["ended_at"] else None)(_dispatch(env)))
    assert d["terminal_classification"] == "signal" and d["signal"] == signal.SIGTERM
    assert ["pane", "close", res["pane"]] in _calls(state_file), [c for c in _calls(state_file) if c[0] == "pane"]
    _until(lambda: _dead(res["watcher_pid"]))
    assert _lease_revoked(env)
