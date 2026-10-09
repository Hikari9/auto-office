"""Headless fallback of a Herdr launch: named dialog, visible in status, doctor warning,
office prompt to a headless worker, and a headless `rerun --resume` (issues #449, #445)."""
from __future__ import annotations

import json
import os

import pytest

from test_herdr_agent_launch import _calls, _launch_events, launch_in_herdr

DIALOG = ("Allow external CLAUDE.md file imports?\n External imports:\n  /Users/x/AGENTS.md\n"
          "❯ 1. Yes, allow external imports\n  2. No, disable external imports")


@pytest.mark.approved
def test_external_imports_dialog_is_named_not_answered_and_shown_in_status(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    monkeypatch.setenv("FAKE_HERDR_PANE_READ", DIALOG)
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, adapter="claude", model="m", effort="high",
                                                    owned_cwd=True)
    assert res["launcher"] == "process-fallback"
    events = _launch_events(env, run)
    assert len(events) == 1 and "external CLAUDE.md imports dialog" in events[0], events
    assert not any(c[:2] == ["pane", "send-keys"] for c in _calls(state_file))  # never answered
    from office import guide
    con = env.con()
    try:
        con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE id=?", (d["id"],))
        con.commit()
        status = guide.status(con, run)
        assert any("runs headless (herdr fallback)" in ln and "external CLAUDE.md imports dialog" in ln
                   for ln in status.lines), status.lines
    finally:
        con.close()


@pytest.mark.approved
def test_no_pane_fallback_reason_is_in_status(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch)
    from office import dispatch
    con = env.con()
    try:
        con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE id=?", (d["id"],))
        con.commit()
        lines = dispatch.headless_fallbacks(con, run)
    finally:
        con.close()
    assert len(lines) == 1 and d["id"] in lines[0] and "herdr agent start failed" in lines[0], lines


def test_doctor_warns_on_claude_imports_outside_the_repo(tmp_path):
    from office import doctor
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("x")
    (repo / "docs" / "more.md").write_text("see @../../AGENTS.md\n")
    (repo / "CLAUDE.md").write_text("@docs/more.md\n@inside.md\nmail me @someone and `@../../nope.md`\n")
    (repo / "inside.md").write_text("ok")
    warn = doctor.claude_import_warnings(repo)
    assert len(warn) == 1 and "../../AGENTS.md" in warn[0] and "docs/more.md" in warn[0], warn
    assert "nope.md" not in warn[0] and "someone" not in warn[0]
    (repo / "docs" / "more.md").write_text("fine\n")
    assert doctor.claude_import_warnings(repo) == []
    (repo / "CLAUDE.md").write_text("@../AGENTS.md\n")
    assert "../AGENTS.md" in doctor.claude_import_warnings(repo)[0]
    assert doctor.claude_import_warnings(tmp_path / "none") == []


@pytest.mark.approved
def test_prompt_to_a_headless_worker_is_queued_and_delivered_once(env, monkeypatch):
    from conftest import approved_run
    from office import guide, prompting, state
    approved_run(env)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    did = con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0]
    con.execute("UPDATE dispatches SET launcher='process-fallback', pid=?, status='running', pane_id=NULL WHERE id=?",
                (os.getpid(), did))
    con.commit()
    res = prompting.prompt(con, run, "T1", "the lockfile is stale; run pnpm install first")
    assert res.lines == [f"queued: {did} runs headless; it sees this on its next office command"]
    assert con.execute("SELECT COUNT(*) FROM amendments").fetchone()[0] == 0  # not a plan change
    monkeypatch.setenv("OFFICE_DISPATCH_ID", did)
    out = guide.worker_status(con, run, did)
    assert any("lockfile is stale" in ln for ln in out.lines), out.lines
    again = guide.worker_status(con, run, did)
    assert not any("lockfile is stale" in ln for ln in again.lines)
    # A piggyback on another command carries nothing once delivered, and a second message arrives alone.
    prompting.prompt(con, run, "T1", "second note")
    from office.result import Result
    r = Result()
    guide.piggyback(con, run, r)
    assert any("second note" in ln for ln in r.notices) and not any("lockfile" in ln for ln in r.notices)


@pytest.mark.approved
def test_headless_fallback_of_a_resume_resumes_the_recorded_session(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    resume = {"parent": "D0", "session_id": "sess-123", "argv": ["--resume", "sess-123"], "herdr_kind": "claude",
              "findings": "none"}
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, adapter="claude", model="m", effort="high",
                                                    owned_cwd=True, resume=resume)
    assert res["launcher"] == "process-fallback"
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["headless_resume"] == ["--resume", "sess-123"]
    assert any("resumed headless" in e and "sess-123" in e for e in _launch_events(env, run))
    from office import adapters
    adapter = adapters.load_all()["claude"]
    argv, _ = adapters.build_argv(adapter, "worker", model="m", effort="high", cwd=ddir, resume_args=spec["headless_resume"])
    assert argv[-2:] == ["--resume", "sess-123"] and "--session-id" not in argv


@pytest.mark.approved
def test_headless_fallback_of_a_resume_without_a_headless_form_says_fresh(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    resume = {"parent": "D0", "session_id": "sess-123", "argv": ["resume", "sess-123"], "herdr_kind": "agy",
              "findings": "none"}
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, adapter="agy", resume=resume)
    assert res["launcher"] == "process-fallback"
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec.get("headless_fresh") is True and "headless_resume" not in spec
    assert any("FRESH session" in e and "no headless resume form" in e for e in _launch_events(env, run))
    con = env.con()
    try:  # the parent's id was not kept, so the new session's own id cannot mismatch it
        assert con.execute("SELECT session_id FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0] != "sess-123"
    finally:
        con.close()
