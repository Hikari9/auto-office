"""Landing stays in `office land`: the browser can only start a run with an end state, never merge, land or deploy."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import shlex
import subprocess
import threading

import pytest

from office import cli, db, land
from office.web import server
from office.web.capabilities import RUN_KINDS
from office.web.service import KIND_TARGET, KINDS, Command, CommandRefused

REPO = "synth-org-0/repo-00"
ISSUE = f"https://github.com/{REPO}/issues/3"
FORBIDDEN = ("land", "merge", "deploy", "ship", "release", "push", "finish", "close")
# Every command the browser can send. Adding one is a decision: it must not land, merge or deploy.
ALLOWED_KINDS = {"start_issue", "queue_issue", "resume_run", "attach_run", "pause", "resume", "set_priority", "demote",
                 "set_auto_mode", "change_route", "chat_send", "settings_set", "settings_unset"}


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    yield s
    s.close()


@pytest.fixture
def port(svc):
    httpd = server.make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def http_json(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                 headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw


def post_command(port, kind, **fields):
    _, page = http_json(port, "GET", "/")
    token = re.search(r'name="office-token" content="([^"]+)"', page).group(1)
    body = {"id": f"cmd-land-{kind or 'none'}-0001", "kind": kind, "target": {}, "payload": {}, "expect": {}, **fields}
    return http_json(port, "POST", "/api/commands", body, {"Content-Type": "application/json", "X-Office-Token": token})


def commands_recorded(svc):
    return svc.writer(lambda con: con.execute("SELECT COUNT(*) FROM commands").fetchone()[0])


def office(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    return code, out.getvalue()


@pytest.fixture
def office_home(tmp_path, monkeypatch):
    """An isolated Office (data, state, config) and a one-commit git repository to start a run in."""
    for name in ("data", "state"):
        monkeypatch.setenv(f"OFFICE_{name.upper()}_HOME", str(tmp_path / name))
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "office-user.yaml"))
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@t")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)  # a developer's signing or hooks config must not reach the test
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.chdir(repo)
    return repo


# ------------------------------------------------------------------ the catalog

def test_command_catalog_exposes_no_land_merge_or_deploy():
    names = set(KINDS) | set(KIND_TARGET) | set(RUN_KINDS)
    assert set(KINDS) == ALLOWED_KINDS and set(KIND_TARGET) == ALLOWED_KINDS
    assert not [n for n in names for word in FORBIDDEN if word in n]


def test_projected_run_controls_offer_no_land_merge_or_deploy(svc):
    controls = {k for r in svc.snapshot()["entities"]["runs"].values() for k in r.get("controls", {})}
    assert controls == set(RUN_KINDS) | {"runtime"}


@pytest.mark.parametrize("kind", ["land", "merge", "deploy"])
def test_posting_a_landing_command_is_refused_and_leaves_no_receipt(svc, port, kind):
    before = commands_recorded(svc)
    status, body = post_command(port, kind, target={"run_id": "r-1"}, payload={"end_state": "merge"})
    assert status == 400 and body["reason"] == "unknown-kind"
    assert commands_recorded(svc) == before and svc.launcher.launches == [] and svc.executor.calls == []


def test_start_issue_cannot_carry_a_landing_end_state(svc):
    with pytest.raises(CommandRefused) as info:
        svc.submit(Command.parse({"id": "cmd-land-bad-0001", "kind": "start_issue", "target": {"repo": REPO, "issue": 3},
                                  "payload": {"end_state": "land"}}), wait=True)
    assert info.value.reason == "bad-payload" and svc.launcher.launches == []


# ------------------------------------------------------------------ the end state travels through office start

@pytest.mark.parametrize("end_state", ["merge", "e2e"])
def test_browser_start_records_the_end_state_through_office_start(svc, office_home, end_state):
    out = svc.submit(Command.parse({"id": f"cmd-land-{end_state}-0001", "kind": "start_issue",
                                    "target": {"repo": REPO, "issue": 3}, "payload": {"end_state": end_state}}), wait=True)
    assert out["status"] == "completed"
    [launch] = svc.launcher.launches
    # The agent is told to start the run itself; nothing else is run on its behalf.
    started = re.search(r"`(office start [^`]+)`", launch["prompt"]).group(1)
    assert shlex.split(started) == ["office", "start", "--issue", ISSUE, "--end-state", end_state]
    assert out["result"]["command"].startswith(f"office start --issue {ISSUE} --end-state {end_state} ")
    assert svc.executor.calls == [] and f"authorized end state `{end_state}`" in launch["prompt"]

    code, text = office(*shlex.split(started)[1:], f"Resolve issue {ISSUE}", "--json")
    assert code == 0, text
    con = db.connect()
    try:
        run = dict(con.execute("SELECT * FROM runs").fetchone())
        assert land.end_state(con, run)["mode"] == end_state
        frozen = json.loads(con.execute("SELECT frozen_json FROM requirements WHERE run_id=?", (run["id"],)).fetchone()[0])
        assert frozen["end_state"] == end_state
    finally:
        con.close()


@pytest.mark.parametrize("end_state", ["merge", "e2e"])
def test_land_before_acceptance_is_refused_whatever_the_end_state(svc, office_home, end_state):
    code, text = office("start", "--issue", ISSUE, "--end-state", end_state, f"Resolve issue {ISSUE}", "--json")
    assert code == 0, text
    for args in (("land",), ("land", f"--{end_state}", "--quote", "ship it")):
        code, text = office(*args, "--json")
        error = json.loads(text)["error"]
        assert code != 0 and error["category"] == "not-ready" and "nothing verified to land" in error["message"], text
    con = db.connect()
    try:
        run = dict(con.execute("SELECT * FROM runs").fetchone())
        assert run["phase"] == "planning" and not run["terminal_at"]
        # A refused land records no authorization and no land event: the gates run before anything is written.
        assert con.execute("SELECT COUNT(*) FROM authorizations WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='authority.land'", (run["id"],)).fetchone()[0] == 0
        assert land.end_state(con, run)["mode"] == end_state
    finally:
        con.close()
