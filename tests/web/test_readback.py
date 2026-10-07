"""Read-back: a web mutation is recorded by Office itself, not only by the web service.

The service runs over the synthetic workspace with the real executor, so each
web command runs the real `office` CLI against that runs.db. The tests then
read the result back the way an operator would: the `commands` receipts, the
`sched_items` rows, the run's `events`, `office queue list --json` and
`office config --show-origin`.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time

import pytest

from office.web import fixtures, server, synthetic
from office.web.executor import Executor, _env, office_argv
from tests.web.test_server import request, token_of

pytestmark = pytest.mark.integration


@pytest.fixture
def real(tmp_path, monkeypatch):
    """(service, port, env): fixture data, real `office` executor, isolated Office homes."""
    env = {"AUTO_OFFICE_RUNS_DB": "", "OFFICE_STATE_HOME": str(tmp_path / "state"),
           "OFFICE_USER_CONFIG": str(tmp_path / "user.yaml"), "OFFICE_DATA_HOME": str(tmp_path / "data")}
    svc = fixtures.build("small", tmp_path / "fx")
    env["AUTO_OFFICE_RUNS_DB"] = str(svc.db_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    svc.executor = Executor(env=env, timeout=120)
    svc.start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield svc, httpd.server_address[1], env
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def office(env, *args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([*office_argv(), *args], capture_output=True, text=True, timeout=120, cwd=cwd,
                          env=_env(env))  # the executor's env: same runtime and PYTHONPATH


def post(port, token, body) -> dict:
    """POST one command as the browser does and wait for its receipt to settle."""
    status, out = request(port, "POST", "/api/commands", body,
                          headers={"Content-Type": "application/json", "X-Office-Token": token,
                                   "Origin": f"http://127.0.0.1:{port}"})
    assert status in (200, 202), out
    deadline = time.time() + 120
    while time.time() < deadline:
        status, receipt = request(port, "GET", f"/api/commands/{body['id']}")
        if status == 200 and receipt.get("status") in ("completed", "failed", "unknown"):
            return receipt
        time.sleep(0.1)
    raise AssertionError(f"{body['id']} never settled")


def rows(svc, sql, args=()):
    con = sqlite3.connect(f"file:{svc.db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def live_run(svc):
    return next(r for r in svc.snapshot()["entities"]["runs"].values()
                if r["liveness"] == "live" and r["office_version"] == synthetic.CURRENT_VERSION
                and not r["controls"]["runtime"]["read_only"])


def queue_entry(env, run_id):
    out = office(env, "queue", "list", "--json")
    assert out.returncode == 0, out.stderr
    return next(e for e in json.loads(out.stdout)["data"]["entries"] if e.get("run_id") == run_id and e["kind"] == "run")


def test_pause_priority_resume_are_read_back_from_office(real):
    svc, port, env = real
    token = token_of(port)
    run = live_run(svc)
    target = {"run_id": run["run_id"]}

    paused = post(port, token, {"id": "cmd-rb-pause-1", "kind": "pause", "target": target,
                                "payload": {"reason": "web read-back"}})
    assert paused["status"] == "completed", paused
    item = rows(svc, "SELECT * FROM sched_items WHERE run_id=? AND kind='run'", (run["run_id"],))
    assert len(item) == 1 and item[0]["paused"] == 1 and item[0]["pause_reason"] == "web read-back"
    entry = queue_entry(env, run["run_id"])
    assert entry["paused"] is True and entry["pause_reason"] == "web read-back"

    urgent = post(port, token, {"id": "cmd-rb-prio-1", "kind": "set_priority", "target": target,
                                "payload": {"level": "urgent"}})
    assert urgent["status"] == "completed", urgent
    assert queue_entry(env, run["run_id"])["priority"] == "urgent"

    resumed = post(port, token, {"id": "cmd-rb-resume-1", "kind": "resume", "target": target})
    assert resumed["status"] == "completed", resumed
    entry = queue_entry(env, run["run_id"])
    assert entry["paused"] is False and entry["priority"] == "urgent"
    assert rows(svc, "SELECT paused FROM sched_items WHERE run_id=? AND kind='run'", (run["run_id"],)) == [{"paused": 0}]

    receipts = rows(svc, "SELECT id, kind, status, origin, target FROM commands WHERE id LIKE 'cmd-rb-%' "
                         "ORDER BY accepted_at")
    assert [(r["id"], r["kind"], r["status"], r["origin"]) for r in receipts] == [
        ("cmd-rb-pause-1", "pause", "completed", "web"),
        ("cmd-rb-prio-1", "set_priority", "completed", "web"),
        ("cmd-rb-resume-1", "resume", "completed", "web")]
    assert all(json.loads(r["target"]) == target for r in receipts)
    kinds = [e["kind"] for e in rows(svc, "SELECT kind FROM events WHERE run_id=? AND kind LIKE 'queue.%' ORDER BY seq",
                                     (run["run_id"],))]
    assert kinds == ["queue.paused", "queue.priority", "queue.resumed"]


def test_a_replayed_command_id_is_not_run_again(real):
    svc, port, env = real
    token = token_of(port)
    body = {"id": "cmd-rb-dup-0001", "kind": "pause", "target": {"run_id": live_run(svc)["run_id"]}}
    assert post(port, token, body)["status"] == "completed"
    assert post(port, token, body)["status"] == "completed"
    assert len(rows(svc, "SELECT 1 FROM events WHERE kind='queue.paused'")) == 1
    assert len(rows(svc, "SELECT 1 FROM commands WHERE id='cmd-rb-dup-0001'")) == 1


def test_settings_write_is_read_back_by_office_config(real):
    svc, port, env = real
    token = token_of(port)
    out = post(port, token, {"id": "cmd-rb-set-0001", "kind": "settings_set",
                             "target": {"tier": "machine", "key": "scheduler.max_active_runs"}, "payload": {"value": 3}})
    assert out["status"] == "completed", out
    shown = office(env, "config", "--list", "--show-origin")
    assert shown.returncode == 0, shown.stderr
    assert "user\tscheduler.max_active_runs=3" in shown.stdout.splitlines()
    assert rows(svc, "SELECT kind, status FROM commands WHERE id='cmd-rb-set-0001'") == [
        {"kind": "settings_set", "status": "completed"}]


def test_approve_plan_never_reaches_office(real):
    svc, port, env = real
    token = token_of(port)
    status, out = request(port, "POST", "/api/commands",
                          {"id": "cmd-rb-plan-0001", "kind": "approve_plan", "target": {"run_id": live_run(svc)["run_id"]},
                           "payload": {"quote": "yes"}},
                          headers={"Content-Type": "application/json", "X-Office-Token": token})
    assert (status, out["reason"]) == (400, "unknown-kind")
    assert rows(svc, "SELECT 1 FROM commands WHERE id='cmd-rb-plan-0001'") == []
    assert rows(svc, "SELECT 1 FROM authorizations WHERE kind='plan'") == []
