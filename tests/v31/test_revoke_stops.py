"""`office revoke` stops the live dispatch: SIGTERM to its supervisor and agent
process groups, its herdr pane closed, the end recorded as `signal`."""
from __future__ import annotations

import os
import signal
import time

from conftest import start_inline
from test_herdr_agent_launch import _calls, _fake, _live_dispatch


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


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def test_revoke_stops_a_headless_dispatch(env, monkeypatch):
    env.trust()
    env.script(executor=[{"sleep": 60}])
    start_inline(env)
    env.office("approve", "plan", "--quote", "yes, go ahead", check=0)
    env.office("dispatch", "T1", env={"OFFICE_LAUNCHER": "auto"}, check=0)
    from office import paths
    d = _until(lambda: (lambda d: d if d["status"] == "running" else None)(_dispatch(env)))
    pgid_file = paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "agent.pgid"
    agent = int(_until(lambda: pgid_file.is_file() and pgid_file.read_text().strip()))
    sup = d["pid"]
    code, out = env.office("revoke", "T1", "--reason", "test stop")
    assert code == 0, out
    assert "SIGTERM" in out
    d = _until(lambda: (lambda d: d if d["ended_at"] else None)(_dispatch(env)))
    assert d["terminal_classification"] == "signal" and d["signal"] == signal.SIGTERM
    _until(lambda: _dead(agent) and _dead(sup))
    assert _lease_revoked(env)


def test_revoke_stops_a_herdr_agent_dispatch(env, monkeypatch):
    state_file = _fake(env, monkeypatch, gets=["working"])
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    monkeypatch.setenv("OFFICE_HERDR_POLL", "0.2")
    from office import db, dispatch, paths
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='claude', model='fake-model', effort='high' WHERE id=?", (d["id"],))
    finally:
        con.close()
    d = _dispatch(env)
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("ROLE executor\n")
    res = dispatch.launch(run, d, "worker", ddir, cwd=env.repo)
    assert res["launcher"] == "herdr", res
    d = _until(lambda: (lambda d: d if d["status"] == "running" and d["pid"] == res["watcher_pid"] else None)(_dispatch(env)))
    code, out = env.office("revoke", "T1", "--reason", "test stop")
    assert code == 0, out
    d = _until(lambda: (lambda d: d if d["ended_at"] else None)(_dispatch(env)))
    assert d["terminal_classification"] == "signal" and d["signal"] == signal.SIGTERM
    assert ["pane", "close", res["pane"]] in _calls(state_file)
    _until(lambda: _dead(res["watcher_pid"]) or os.waitpid(res["watcher_pid"], os.WNOHANG)[0])
    assert _lease_revoked(env)
