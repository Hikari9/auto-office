"""The loopback server: binding, request guards, startup lifecycle and the daemon."""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from office import commands, db
from office.web import server
from office.web.service import Service


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    yield s
    s.close()


@pytest.fixture
def http_svc(svc):
    httpd = server.make_server(svc, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield svc, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, *, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    raw = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=raw, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except ValueError:
        return resp.status, data.decode()


def token_of(port):
    status, page = request(port, "GET", "/")
    assert status == 200
    return re.search(r'name="office-token" content="([^"]+)"', page).group(1)


def pause_body(svc, cid="cmd-http-0001"):
    run = next(r for r in svc.snapshot()["entities"]["runs"].values()
               if r["liveness"] == "live" and not r["controls"]["runtime"]["read_only"])
    return {"id": cid, "kind": "pause", "target": {"run_id": run["run_id"]}}


# ------------------------------------------------------------------ binding

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com", "10.0.0.1"])
def test_non_loopback_host_is_refused(host):
    with pytest.raises(server.WebError) as info:
        server.loopback_host(host)
    assert info.value.category == "non-loopback-host"


@pytest.mark.parametrize("host,bind", [("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("::1", "::1"),
                                       ("127.0.0.2", "127.0.0.2")])
def test_loopback_hosts_bind(host, bind):
    assert server.loopback_host(host) == bind


def test_serve_refuses_non_loopback_before_building_anything(monkeypatch):
    monkeypatch.setattr(server, "build_real", lambda: pytest.fail("built a service"))
    with pytest.raises(server.WebError):
        server.serve("0.0.0.0", 0)


# ------------------------------------------------------------------ guards

def test_index_embeds_the_token_and_fixture_marker(http_svc):
    svc, port = http_svc
    status, page = request(port, "GET", "/")
    assert status == 200 and svc.token in page and 'content="small"' in page


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1:1", "localhost.evil:{port}", ""])
def test_wrong_host_header_is_refused(http_svc, host):
    _, port = http_svc
    status, body = request(port, "GET", "/api/snapshot", host=host.format(port=port) or " ")
    assert status == 421 and body["reason"] == "bad-host"


def test_localhost_host_header_is_accepted(http_svc):
    _, port = http_svc
    assert request(port, "GET", "/api/snapshot", host=f"localhost:{port}")[0] == 200


def test_post_needs_json_content_type(http_svc):
    svc, port = http_svc
    for ctype in ("text/plain", None):
        headers = {"X-Office-Token": token_of(port), **({"Content-Type": ctype} if ctype else {})}
        status, body = request(port, "POST", "/api/commands", pause_body(svc), headers=headers)
        assert status == 415 and body["reason"] == "bad-content-type"
    assert_nothing_ran(svc, port)


def assert_nothing_ran(svc, port, cid="cmd-http-0001"):
    assert svc.executor.calls == []
    assert request(port, "GET", f"/api/commands/{cid}")[0] == 404


def test_post_with_wrong_host_is_refused(http_svc):
    svc, port = http_svc
    status, body = request(port, "POST", "/api/commands", pause_body(svc), host="evil.example",
                           headers={"Content-Type": "application/json", "X-Office-Token": token_of(port)})
    assert status == 421 and body["reason"] == "bad-host"
    assert_nothing_ran(svc, port)


def test_post_with_foreign_origin_is_refused(http_svc):
    svc, port = http_svc
    status, body = request(port, "POST", "/api/commands", pause_body(svc),
                           headers={"Content-Type": "application/json", "X-Office-Token": token_of(port),
                                    "Origin": "http://evil.example"})
    assert status == 403 and body["reason"] == "bad-origin"
    assert_nothing_ran(svc, port)


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_post_without_the_token_is_refused(http_svc, token):
    svc, port = http_svc
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Office-Token"] = token
    status, body = request(port, "POST", "/api/commands", pause_body(svc), headers=headers)
    assert status == 403 and body["reason"] == "bad-token"
    assert svc.executor.calls == []


def test_valid_post_executes_once_and_replays(http_svc):
    svc, port = http_svc
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Office-Token": token_of(port),
               "Origin": f"http://127.0.0.1:{port}"}
    status, body = request(port, "POST", "/api/commands", pause_body(svc), headers=headers)
    assert status == 202 and body["receipt"]["status"] in ("running", "completed")
    svc.wait("cmd-http-0001")
    status, body = request(port, "POST", "/api/commands", pause_body(svc), headers=headers)
    assert status == 200 and body["receipt"]["replayed"] is True
    assert len(svc.executor.calls) == 1
    status, receipt = request(port, "GET", "/api/commands/cmd-http-0001")
    assert status == 200 and receipt["status"] == "completed"


def test_unknown_kind_over_http(http_svc):
    svc, port = http_svc
    status, body = request(port, "POST", "/api/commands", {"id": "cmd-http-0002", "kind": "merge"},
                           headers={"Content-Type": "application/json", "X-Office-Token": token_of(port)})
    assert status == 400 and body["reason"] == "unknown-kind"
    assert_nothing_ran(svc, port, "cmd-http-0002")


def test_read_endpoints(http_svc):
    svc, port = http_svc
    run = pause_body(svc)["target"]["run_id"]
    assert request(port, "GET", "/api/settings")[1]["scope"] == "machine"
    assert request(port, "GET", f"/api/runs/{run}/activity?limit=2")[1]["limit"] == 2
    assert request(port, "GET", "/api/nothing")[0] == 404


# ------------------------------------------------------------------ startup lifecycle

def test_startup_migrates_once_creates_host_id_and_recovers_receipts(tmp_path, caplog):
    path = tmp_path / "data" / "runs.db"
    con = db.connect(path)
    commands.record(con, command_id="cmd-crash-001", kind="pause", target="x", payload={}, origin="web")
    commands.transition(con, "cmd-crash-001", "running", pid=2 ** 22 + 7)
    con.close()
    state = tmp_path / "state"
    caplog.set_level(logging.INFO, logger="office.web")
    svc = Service(path, state, host_probe=lambda: {}, config=lambda: {}).start()
    try:
        assert "migrated to schema" in caplog.text and "marked unknown" in caplog.text
        assert (state / "web" / "host-id").is_file() and svc.host_id
        assert svc.recovered == ["cmd-crash-001"]
        assert svc.command("cmd-crash-001")["status"] == "unknown"
    finally:
        svc.close()


def test_reads_after_startup_never_use_the_writer_path(svc, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("db.connect after startup"))
    monkeypatch.setattr(db, "migrate", lambda *a, **k: pytest.fail("db.migrate after startup"))
    svc.poll(force=True)
    run = pause_body(svc)["target"]["run_id"]
    svc.settings_view(run_id=run)
    svc.activity(run, limit=5, before_seq=None)
    assert svc.observer.con.execute("PRAGMA query_only").fetchone()[0] == 1


def test_fixture_mode_uses_a_temp_home(svc, tmp_path):
    assert str(svc.db_path).startswith(str(tmp_path)) and svc.fixture == "small"
    assert svc.snapshot()["scalars"]["fixture"] == "small"
    assert type(svc.launcher).__name__ == "FakeLauncher" and type(svc.executor).__name__ == "FakeExecutor"
    assert svc.github.token == "fixture-token"


# ------------------------------------------------------------------ daemon

def test_a_reused_pid_is_not_taken_for_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "st"))
    server.web_dir().mkdir(parents=True)
    # This test process is alive but is not the server that wrote the file.
    server.pid_file().write_text(json.dumps({"pid": os.getpid(), "started": "Thu Jan  1 00:00:00 1970", "url": "x"}))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None if sig == 0 else pytest.fail("signalled"))
    assert server.status().data == {"running": False}
    assert server.stop().lines == ["office web is not running"] and not server.pid_file().exists()


def test_status_and_stop_without_a_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "st"))
    assert server.status().lines == ["office web is not running"]
    server.web_dir().mkdir(parents=True)
    server.pid_file().write_text(json.dumps({"pid": 2 ** 22 + 9, "url": "x"}))
    assert server.status().data == {"running": False}
    assert server.stop().lines == ["office web is not running"] and not server.pid_file().exists()


@pytest.mark.integration
def test_start_status_stop_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "st"))
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    started = server.start("127.0.0.1", 0, "small")
    try:
        info = started.data
        assert info["pid"] != os.getpid() and info["fixture"] == "small"
        assert "running" in server.status().lines[0]
        status, page = request(info["port"], "GET", "/")
        assert status == 200 and "FIXTURE" in page
        assert "already running" in server.start("127.0.0.1", 0, "small").lines[0]
    finally:
        server.stop()
    assert server.status().data == {"running": False}


@pytest.mark.integration
def test_office_web_cli_serve_and_refusal(tmp_path):
    env = {**os.environ, "OFFICE_STATE_HOME": str(tmp_path / "st"), "OFFICE_USER_CONFIG": str(tmp_path / "u.yaml")}
    bad = subprocess.run([sys.executable, "-m", "office", "web", "serve", "--host", "0.0.0.0"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert bad.returncode == 2 and "non-loopback-host" in bad.stdout
    proc = subprocess.Popen([sys.executable, "-m", "office", "web", "serve", "--port", "0", "--fixture", "small"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pid = tmp_path / "st" / "web" / "web.pid"
        deadline = time.time() + 30
        while not pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert json.loads(pid.read_text())["pid"] == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert not pid.exists()
