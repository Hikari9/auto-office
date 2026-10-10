"""A brief tells the executor to source `<dispatch dir>/agent.env` before `office submit`. Every launch path
that hands it that brief writes the file before the agent process starts, and with the content the Herdr
path writes for the same run and dispatch (#494 live trial: a headless executor could not submit)."""
from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest

from conftest import Env, _activate, approved_run

from test_herdr_agent_launch import launch_in_herdr

# Runs first in every fake harness binary, before the harness does anything: reads agent.env as the agent
# process finds it at its start and records what it saw. The fake then runs as it always does.
PRELUDE = """\
import json, os
from pathlib import Path
from office import paths
_ddir = paths.run_dir(os.environ["OFFICE_RUN_ID"]) / "dispatches" / os.environ["OFFICE_DISPATCH_ID"]
_env = _ddir / "agent.env"
_seen = {"exists": _env.is_file(), "text": _env.read_text() if _env.is_file() else None,
         "mode": _env.stat().st_mode & 0o777 if _env.is_file() else None}
Path(os.environ["FAKE_ENV_RECORD"]).write_text(json.dumps(_seen))
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An isolated Env built directly (not the `env` fixture), so these tests stay in the default unit tier."""
    return _activate(Env(tmp_path, monkeypatch), monkeypatch)


def _recording_harnesses(env, monkeypatch):
    """Every fake harness binary records what agent.env looked like when the agent started. Returns the record path."""
    record = env.tmp / "agent-env-seen.json"
    monkeypatch.setenv("FAKE_ENV_RECORD", str(record))
    for path in list(env.fakes):
        body = path.read_text().replace("import os, runpy\n", "import os, runpy\n" + PRELUDE, 1)
        path.write_text(body)
        env.fakes[path] = body
    return record


def _approved_executor(env, monkeypatch):
    record = _recording_harnesses(env, monkeypatch)
    # A `sleep` action keeps the fake a real child process: an in-process fake would skip the prelude.
    approved_run(env, executor=[{"sleep": 0.01}])
    return record


def _expected(env, overrides):
    """What the Herdr path writes for this run and dispatch: `write_agent_env` under the launching environment."""
    from office import dispatch, paths, state
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
        d = state.get_dispatch(con, con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0])
    finally:
        con.close()
    out = env.tmp / "expected"
    out.mkdir(exist_ok=True)
    with mock.patch.dict(os.environ, overrides):
        file = dispatch.write_agent_env(run, d, out, worker=True)
    return (paths.run_dir(run["id"]) / "dispatches" / d["id"]), file.read_text()


def _wait_for(path, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and path.read_text():
            return json.loads(path.read_text())
        time.sleep(0.1)
    raise AssertionError(f"the fake harness never started: no {path}")


def _check_env_text(text, env):
    lines = text.splitlines()
    assert lines == sorted(lines) and all(ln.startswith("export ") for ln in lines), lines
    for key in ("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE"):
        assert any(ln.startswith(f"export {key}=") for ln in lines), (key, lines)
    assert "export OFFICE_ROLE=executor" in lines, lines


@pytest.mark.parametrize("launcher", ["sync", "process"])
def test_a_headless_executor_finds_agent_env_when_it_starts(world, monkeypatch, launcher):
    env = world
    record = _approved_executor(env, monkeypatch)
    overrides = {"OFFICE_LAUNCHER": launcher}
    code, out = env.office("dispatch", "T1", env=overrides)
    assert code == 0, out
    seen = _wait_for(record)
    ddir, expected = _expected(env, overrides)
    assert seen["exists"], f"{launcher}: the agent started with no {ddir / 'agent.env'}"
    assert seen["text"] == expected
    assert seen["mode"] == 0o600
    _check_env_text(seen["text"], env)


def test_an_external_executor_finds_agent_env_before_anyone_starts_it(world, monkeypatch):
    env = world
    _approved_executor(env, monkeypatch)
    overrides = {"OFFICE_WORKER_LAUNCHER": "external"}
    code, out = env.office("dispatch", "T1", env=overrides)
    assert code == 0, out
    ddir, expected = _expected(env, overrides)
    assert (ddir / "agent.env").read_text() == expected
    _check_env_text(expected, env)


@pytest.mark.approved
def test_the_herdr_path_writes_the_same_file(env, monkeypatch):
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, adapter="claude", model="m", effort="high")
    from office import dispatch
    out = env.tmp / "expected"
    out.mkdir()
    assert (ddir / "agent.env").read_text() == dispatch.write_agent_env(run, d, out, worker=True).read_text()
    _check_env_text((ddir / "agent.env").read_text(), env)
