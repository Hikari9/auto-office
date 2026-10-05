"""office rerun --resume|--fresh and office dismiss (R5-R8, issue #200).

Findings never start an executor on their own; after a worker ends, the
orchestrator picks: resume that session, or start fresh with the findings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

FAKE_HERDR = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_AGENT", "gone")
if args[:2] == ["agent", "get"]:
    if mode == "alive":
        print(json.dumps({{"result": {{"agent": {{"name": args[2], "agent_status": "working"}}}}}})); sys.exit(0)
    if mode == "gone":
        sys.stderr.write(json.dumps({{"error": {{"code": "agent_not_found"}}}})); sys.exit(1)
    sys.stderr.write("server unavailable"); sys.exit(1)
print(json.dumps({{"result": {{}}}}))
'''


def _setup(env, monkeypatch, *, session="sess-123", launcher="herdr"):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}] * 3)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    for k, v in EXTERNAL.items():
        monkeypatch.setenv(k, v)
    con = env.con()
    have = {r[1] for r in con.execute("PRAGMA table_info(dispatches)")}
    for col in ("session_id", "resumed_from"):
        if col not in have:  # added by the pane-lifecycle change; tolerate either order
            con.execute(f"ALTER TABLE dispatches ADD COLUMN {col} TEXT")
    did = con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0]
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=started_at, "
                "launcher=?, pane_id='w1:p5', session_id=?, adapter_id='claude', model='m' WHERE id=?",
                (launcher, session, did))
    con.commit()
    return did


def _run(con):
    from office import state
    return state.get_run(con, con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0])


RESUMABLE = {"id": "claude", "invocation": {"executable": "claude"}, "effort_mapping": {"high": "high"},
             "office_profiles": {"worker": {"herdr_kind": "claude", "interactive": {
                 "argv": ["--model", "{model}"], "resume_argv": ["--resume", "{session_id}"]}}}}
NO_RESUME = {**RESUMABLE, "office_profiles": {"worker": {"herdr_kind": "claude", "interactive": {"argv": ["--model", "{model}"]}}}}


def test_resume_argv_appends_the_session_or_says_none():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import adapters
    args, kind = adapters.resume_argv(RESUMABLE, "worker", session_id="S1", model="m", effort="high", cwd=Path("."))
    assert args == ["--model", "m", "--resume", "S1"] and kind == "claude"
    assert adapters.resume_argv(NO_RESUME, "worker", session_id="S1", model="m", effort="high", cwd=Path(".")) is None


@pytest.mark.approved
def test_rerun_needs_exactly_one_mode(env, monkeypatch):
    _setup(env, monkeypatch)
    code, out = env.office("rerun", "T1", env=EXTERNAL)
    assert code == 2 and "--resume" in out and "--fresh" in out, out
    code, out = env.office("rerun", "T1", "--resume", "--fresh", env=EXTERNAL)
    assert code == 2, out


@pytest.mark.approved
def test_fresh_starts_a_new_executor_on_the_same_worktree(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and "fresh session" in out, out
    con = env.con()
    rows = con.execute("SELECT id, worktree FROM dispatches WHERE task_id='T1' AND role='executor' ORDER BY started_at").fetchall()
    assert len(rows) == 2 and rows[0]["worktree"] == rows[1]["worktree"] and rows[1]["id"] != parent


@pytest.mark.approved
def test_resume_refuses_without_a_session_id(env, monkeypatch):
    _setup(env, monkeypatch, session=None)
    code, out = env.office("rerun", "T1", "--resume", env=EXTERNAL)
    assert code == 4 and "no stored harness session id" in out and "office rerun T1 --fresh" in out, out


@pytest.mark.approved
@pytest.mark.parametrize("mode,reason", [("alive", "still running"), ("down", "herdr unreachable")])
def test_resume_refuses_a_live_or_unknown_parent(env, monkeypatch, mode, reason):
    _setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", mode)
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": RESUMABLE})
    con = env.con()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert reason in err.value.message and err.value.next_step == "office rerun T1 --fresh"


@pytest.mark.approved
def test_resume_refuses_an_adapter_without_a_resume_form(env, monkeypatch):
    _setup(env, monkeypatch)
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": NO_RESUME})
    con = env.con()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert "declares no resume form" in err.value.message


@pytest.mark.approved
def test_resume_launches_a_linked_dispatch_carrying_the_resume_argv(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": RESUMABLE})
    con = env.con()
    res = rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert "resuming " + parent in res.lines[0], res.lines
    child = con.execute("SELECT id, resumed_from FROM dispatches WHERE task_id='T1' AND role='executor' "
                        "ORDER BY started_at DESC LIMIT 1").fetchone()
    assert child["resumed_from"] == parent
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert resume["parent"] == parent and resume["session_id"] == "sess-123" and resume["argv"][-2:] == ["--resume", "sess-123"]
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert f"resumed from {parent}" in out, out


@pytest.mark.approved
@pytest.mark.parametrize("adapter,form", [("claude", ["--resume"]), ("codex", ["resume"])])
def test_resume_argv_comes_from_the_recorded_id_after_the_pane_closed(env, monkeypatch, adapter, form):
    sid = "6f1c2a9e-3b7d-4c55-9a41-0d2e8b7a1f33"
    parent = _setup(env, monkeypatch, session=sid)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    con = env.con()
    con.execute("UPDATE dispatches SET adapter_id=?, harness=?, effort='high', pane_closed_at='2026-10-05T00:00:00Z' WHERE id=?",
                (adapter, adapter, parent))
    con.commit()
    from office import rerun
    rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)  # the seed adapter, not a stand-in
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert resume["session_id"] == sid and resume["argv"][-2:] == [*form, sid], resume
    assert "--session-id" not in resume["argv"]


@pytest.mark.approved
def test_inspect_task_names_the_harness_that_gave_no_session_id(env, monkeypatch):
    parent = _setup(env, monkeypatch, session=None)
    con = env.con()
    con.execute("UPDATE dispatches SET harness='agy' WHERE id=?", (parent,))
    con.commit()
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert "session unavailable: agy exposes none" in out, out
    con.execute("UPDATE dispatches SET session_id='sess-7' WHERE id=?", (parent,))
    con.commit()
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert "| session sess-7" in out and "unavailable" not in out, out


@pytest.mark.approved
def test_dismiss_closes_ended_panes_and_refuses_live_ones(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    from office import dispatch, rerun
    calls = []
    monkeypatch.setattr(dispatch, "reclaim_pane", lambda run, did, explicit=False: calls.append((did, explicit)) or "closed",
                        raising=False)
    con = env.con()
    res = rerun.dismiss(con, _run(con), "T1")
    assert calls == [(parent, True)] and "closed" in res.lines[0]
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE id=?", (parent,))
    con.commit()
    with pytest.raises(rerun.Refused):
        rerun.dismiss(con, _run(con), parent)
    assert rerun.dismiss(con, _run(con), None, all_=True).lines[-1].startswith("left running")
