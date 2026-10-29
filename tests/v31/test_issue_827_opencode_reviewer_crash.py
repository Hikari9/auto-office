"""A completed or crashed opencode pane without its reply is a route failure.

Native Herdr check on macOS: not run (fake Herdr state only).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run
from test_herdr_agent_launch import _fake, _live_dispatch


@pytest.mark.approved
def test_crashed_opencode_reviewer_pane_without_reply_is_terminal(env, monkeypatch):
    from office import db, dispatch

    # The scripted harness exits like a crashed `opencode mini`; it never writes reply.txt.
    opencode = env.bin / "opencode"
    opencode.write_text(f"#!{sys.executable}\nraise SystemExit(17)\n")
    opencode.chmod(0o755)
    crash = subprocess.run([str(opencode)], capture_output=True, text=True)
    assert crash.returncode == 17

    state_file = _fake(env, monkeypatch, gets=["done"])
    _, d = _live_dispatch(env, monkeypatch)
    agent = dispatch.herdr_agent_name(d["id"])
    state = json.loads(state_file.read_text())
    state.update({"pane_agents": {"w1:p827": agent}, "pane_kinds": {"w1:p827": "opencode"},
                  "content": "opencode mini exited unexpectedly"})
    state_file.write_text(json.dumps(state))
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='opencode', harness='opencode', launcher='herdr', "
                        "pane_id='w1:p827' WHERE id=?", (d["id"],))
    finally:
        con.close()

    reply = env.tmp / "dispatches" / d["id"] / "reply.txt"
    from office import gates
    result = gates._reviewer_pane_terminal_without_reply(
        {**d, "adapter_id": "opencode", "launcher": "herdr"}, reply)

    assert result == "done"
    assert not reply.exists()


@pytest.mark.approved
def test_opencode_reviewer_profile_does_not_treat_a_working_pane_as_terminal(env, monkeypatch):
    from office import adapters, db, dispatch, gates
    state_file = _fake(env, monkeypatch, gets=["working"])
    _, d = _live_dispatch(env, monkeypatch)
    agent = dispatch.herdr_agent_name(d["id"])
    state = json.loads(state_file.read_text())
    state.update({"pane_agents": {"w1:p827": agent}, "pane_kinds": {"w1:p827": "opencode"},
                  "content": "Working (1s • esc to interrupt)"})
    state_file.write_text(json.dumps(state))
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='opencode', harness='opencode', launcher='herdr', "
                        "pane_id='w1:p827' WHERE id=?", (d["id"],))
    finally:
        con.close()

    profile = adapters.profile(adapters.load_all()["opencode"], "reviewer")
    assert adapters.terminal_statuses_without_reply(adapters.load_all()["opencode"], "reviewer") == {"done", "idle"}
    assert profile["interactive"]["wall_cap"] is True
    assert gates._reviewer_pane_terminal_without_reply(
        {**d, "adapter_id": "opencode", "launcher": "herdr"}, env.tmp / "missing-reply.txt") is None


@pytest.mark.approved
@pytest.mark.parametrize("pane_status", ["done", "idle"])
@pytest.mark.parametrize("sample", [
    {"busy": True, "text_busy": False},
    {"busy": False, "text_busy": True},
])
def test_opencode_terminal_status_is_ignored_while_pane_is_busy(env, monkeypatch, pane_status, sample):
    from office import db, dispatch, gates

    state_file = _fake(env, monkeypatch, gets=[pane_status])
    _, d = _live_dispatch(env, monkeypatch)
    agent = dispatch.herdr_agent_name(d["id"])
    state = json.loads(state_file.read_text())
    state.update({"pane_agents": {"w1:p827": agent}, "pane_kinds": {"w1:p827": "opencode"},
                  "content": "opencode reviewer still working"})
    state_file.write_text(json.dumps(state))
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='opencode', harness='opencode', launcher='herdr', "
                        "pane_id='w1:p827' WHERE id=?", (d["id"],))
    finally:
        con.close()

    monkeypatch.setattr(dispatch, "_herdr_agent_sample", lambda _agent: {"status": pane_status, **sample})
    assert gates._reviewer_pane_terminal_without_reply(
        {**d, "adapter_id": "opencode", "launcher": "herdr"}, env.tmp / "missing-reply.txt") is None


@pytest.mark.approved
@pytest.mark.parametrize("sample", [None, {"status": []}, {"status": {}}])
def test_opencode_terminal_status_is_ignored_when_herdr_sample_is_missing_or_malformed(env, monkeypatch, sample):
    from office import db, dispatch, gates

    state_file = _fake(env, monkeypatch, gets=["gone"])
    _, d = _live_dispatch(env, monkeypatch)
    agent = dispatch.herdr_agent_name(d["id"])
    state = json.loads(state_file.read_text())
    state.update({"pane_agents": {"w1:p827": agent}, "pane_kinds": {"w1:p827": "opencode"},
                  "content": "opencode reviewer exited"})
    state_file.write_text(json.dumps(state))
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='opencode', harness='opencode', launcher='herdr', "
                        "pane_id='w1:p827' WHERE id=?", (d["id"],))
    finally:
        con.close()

    monkeypatch.setattr(dispatch, "_herdr_agent_sample", lambda _agent: sample)
    assert gates._reviewer_pane_terminal_without_reply(
        {**d, "adapter_id": "opencode", "launcher": "herdr"}, env.tmp / "missing-reply.txt") is None


@pytest.mark.approved
@pytest.mark.parametrize("pane_status", ["done", "idle"])
def test_crashed_opencode_review_records_unavailable_and_attempts_fallback_without_round(env, monkeypatch,
                                                                                         pane_status):
    from office import candidates, db, dispatch, gates, state

    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    from office.util import now_iso
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, created_at) "
                "VALUES('G827', ?, 'T1', 'T1', 'code_review', 'issue-827', 'running', ?)",
                (run["id"], now_iso()))
    gate = dict(con.execute("SELECT * FROM gates WHERE id='G827'").fetchone())
    round_before = gate["round"]
    calls = []
    routes = [
        {"harness": "opencode", "adapter_id": "opencode", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "2.0.29"},
        {"harness": "claude", "adapter_id": "claude", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "1.0.0"},
    ]

    def route_role(_con, _config, _run, _role, *, exclude=None, **_kwargs):
        for candidate in routes:
            from office import routing
            if routing.candidate_id(candidate) not in (exclude or []):
                calls.append(candidate["harness"])
                return {"status": "selected", "candidate": candidate,
                        "selected": routing.candidate_id(candidate), "selection_disclosure": {}}
        return {"status": "no_qualifying_route", "skipped": []}

    state_file = _fake(env, monkeypatch)
    opencode = env.bin / "opencode"
    opencode.write_text(f"#!{sys.executable}\nraise SystemExit(17)\n")
    opencode.chmod(0o755)
    crash = subprocess.run([str(opencode)], capture_output=True, text=True)
    assert crash.returncode == 17

    def launch(_run, reviewer, _kind, ddir, *, output, **_kwargs):
        pane = reviewer["adapter_id"] == "opencode"
        if pane:
            agent = dispatch.herdr_agent_name(reviewer["id"])
            fake = json.loads(state_file.read_text())
            fake.update({"get": [pane_status], "pane_agents": {"w1:p827": agent},
                         "pane_kinds": {"w1:p827": "opencode"}, "content": "opencode mini exited unexpectedly"})
            state_file.write_text(json.dumps(fake))
        con.execute("UPDATE dispatches SET launcher=?, pane_id=?, status='exited', terminal_classification=?, ended_at=? "
                    "WHERE id=?", ("herdr" if pane else "process", "w1:p827" if pane else None,
                                   "success" if pane else "nonzero", "2026-10-29T00:00:00+00:00", reviewer["id"]))

    monkeypatch.setattr(candidates, "route_role", route_role)
    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(gates, "_reprompt_until_valid", lambda _con, _run, _d, _ddir, _output, parsed, **_kw:
                        ("", parsed, "reviewer ended without a reply"))
    try:
        result = gates.run_reviewer(con, run, gate, "code_reviewer", "review", cwd=Path(env.repo))
        assert result.get("verdict") == "UNAVAILABLE" or result.get("status") == "UNAVAILABLE", result
        assert f"Herdr pane status {pane_status}" in result["summary"], result
        assert calls == ["opencode", "claude"], calls
        assert con.execute("SELECT round FROM gates WHERE id=?", (gate["id"],)).fetchone()[0] == round_before
    finally:
        con.close()


@pytest.mark.approved
def test_opencode_wall_cap_timeout_records_unavailable_and_attempts_fallback_without_round(env, monkeypatch):
    from office import candidates, db, dispatch, gates, state

    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    from office.util import now_iso, parse_iso
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, created_at) "
                "VALUES('G827', ?, 'T1', 'T1', 'code_review', 'issue-827-timeout', 'running', ?)",
                (run["id"], now_iso()))
    gate = dict(con.execute("SELECT * FROM gates WHERE id='G827'").fetchone())
    round_before = gate["round"]
    calls = []
    reply_files = []
    routes = [
        {"harness": "opencode", "adapter_id": "opencode", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "2.0.29"},
        {"harness": "claude", "adapter_id": "claude", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "1.0.0"},
    ]

    def route_role(_con, _config, _run, _role, *, exclude=None, **_kwargs):
        for candidate in routes:
            from office import routing
            if routing.candidate_id(candidate) not in (exclude or []):
                calls.append(candidate["harness"])
                return {"status": "selected", "candidate": candidate,
                        "selected": routing.candidate_id(candidate), "selection_disclosure": {}}
        return {"status": "no_qualifying_route", "skipped": []}

    state_file = _fake(env, monkeypatch, gets=["working"], reads=["Working (1s • esc to interrupt)"])
    opencode = env.bin / "opencode"
    opencode.write_text(f"#!{sys.executable}\nraise SystemExit(17)\n")
    opencode.chmod(0o755)
    assert subprocess.run([str(opencode)], capture_output=True, text=True).returncode == 17
    monkeypatch.setenv("OFFICE_WORKER_MAX_MINUTES", "0.001")
    monkeypatch.setattr(candidates, "route_role", route_role)

    def launch(_run, reviewer, _kind, ddir, *, output, **_kwargs):
        reply_files.append(output)
        pane = reviewer["adapter_id"] == "opencode"
        launcher = "herdr" if pane else "process"
        pane_id = "w1:p827" if pane else None
        agent = dispatch.herdr_agent_name(reviewer["id"]) if pane else None
        with db.transaction(con):
            con.execute("UPDATE dispatches SET launcher=?, pane_id=?, status='running', launched_at=? WHERE id=?",
                        (launcher, pane_id, now_iso(), reviewer["id"]))
        if pane:
            fake = json.loads(state_file.read_text())
            fake.update({"get": ["working"], "pane_agents": {pane_id: agent}, "pane_kinds": {pane_id: "opencode"},
                         "content": "Working (1s • esc to interrupt)"})
            state_file.write_text(json.dumps(fake))
            con_row = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (reviewer["id"],)).fetchone())
            assert dispatch._pane_wall_cap(con_row, {"kind": "reviewer"}) == pytest.approx(0.06)
            launched = parse_iso(con_row["launched_at"]).timestamp()
            monkeypatch.setattr(dispatch.time, "time", lambda: launched + 1)
            monkeypatch.setattr(dispatch, "reclaim_pane", lambda *_args, **_kwargs: None)
            exit_code, classification = dispatch.watch_herdr_agent(
                reviewer["id"], {"kind": "reviewer", "output": str(output), "herdr_agent": agent})
            assert exit_code is None
            assert classification == "timeout"
        else:
            classification = "nonzero"
            exit_code = 17
        with db.transaction(con):
            con.execute("UPDATE dispatches SET status='exited', terminal_classification=?, exit_code=?, ended_at=? "
                        "WHERE id=?", (classification, exit_code, now_iso(), reviewer["id"]))

    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(gates, "_reprompt_until_valid", lambda _con, _run, _d, _ddir, _output, parsed, **_kw:
                        ("", parsed, "reviewer ended without a reply"))
    try:
        result = gates.run_reviewer(con, run, gate, "code_reviewer", "review", cwd=Path(env.repo))
        assert result.get("verdict") == "UNAVAILABLE" or result.get("status") == "UNAVAILABLE", result
        assert "timeout" in result["summary"].lower(), result
        assert calls == ["opencode", "claude"], calls
        assert con.execute("SELECT round FROM gates WHERE id=?", (gate["id"],)).fetchone()[0] == round_before
        assert reply_files and all(not path.exists() for path in reply_files)
    finally:
        con.close()
