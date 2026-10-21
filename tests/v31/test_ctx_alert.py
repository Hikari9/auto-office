"""Orchestrator context-high parsing, episode, and notice behavior."""
import json
import sqlite3

from conftest import approved_run
from office import ctx_alert
from office import db, discovery, dispatch, root_wake, state


def _assistant(usage, **extra):
    return json.dumps({"type": "assistant", "message": {"usage": usage}, **extra})


def _event_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_bindings(harness, session_id, run_id, ended_at)")
    con.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id, kind, audience, task_id, "
                "dispatch_id, summary, payload_json, office_version, created_at)")
    return con


def test_context_tokens_matches_rico_mode_tail_self_test_cases():
    tail = "\n".join([
        '{"partial',
        _assistant({"input_tokens": 5, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 1000}),
        _assistant({"input_tokens": 9, "cache_read_input_tokens": 999999}, isSidechain=True),
        json.dumps({"type": "user", "message": {"usage": {"input_tokens": 900000}}}),
    ])
    assert ctx_alert.context_tokens(tail) == 101005
    assert ctx_alert.context_tokens('{"type":"user"}') is None
    assert ctx_alert.context_tokens(_assistant({})) == 0


def test_tick_fires_once_rearms_below_and_ignores_unknown():
    high, notice = ctx_alert.tick(False, 199000, 200000)
    assert (high, notice) == (False, None)
    high, notice = ctx_alert.tick(high, 231000, 200000)
    assert (high, notice) == (True,
        "ctx-high: orchestrator 231k > 200k; run /compactable at the next boundary; compact on COMPACT NOW"
    )
    assert ctx_alert.tick(high, 250000, 200000) == (True, None)
    assert ctx_alert.tick(high, None, 200000) == (True, None)
    high, notice = ctx_alert.tick(high, 40000, 200000)
    assert (high, notice) == (False, None)
    assert ctx_alert.tick(high, 210000, 200000) == (True,
        "ctx-high: orchestrator 210k > 200k; run /compactable at the next boundary; compact on COMPACT NOW"
    )


def test_notice_uses_bound_transcript_low_threshold_and_is_rate_limited(tmp_path, monkeypatch):
    con = _event_db()
    con.execute("INSERT INTO session_bindings VALUES('claude', 'root-session', 'run-1', NULL)")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(_assistant({"input_tokens": 400, "cache_read_input_tokens": 700}) + "\n")
    state_dir = tmp_path / "runs"
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [("claude", "root-session")])
    monkeypatch.setattr(ctx_alert, "_transcript_path", lambda harness, session: transcript)
    monkeypatch.setattr(ctx_alert.paths, "run_dir", lambda run_id: state_dir / run_id)
    run = {"id": "run-1", "policy": {"orchestrator_context": {"threshold_tokens": 1000}}}

    assert ctx_alert.notice(con, run, now=1000) == (
        "ctx-high: orchestrator 1k > 1k; run /compactable at the next boundary; compact on COMPACT NOW"
    )
    event = con.execute("SELECT kind, summary FROM events WHERE run_id='run-1'").fetchone()
    assert tuple(event) == ("ctx.high", "ctx-high: orchestrator 1k > 1k; run /compactable at the next boundary; compact on COMPACT NOW")
    assert ctx_alert.notice(con, run, now=1059) is None
    assert ctx_alert.notice(con, run, now=1060) is None  # same episode

    transcript.write_text(_assistant({"input_tokens": 200}) + "\n")
    assert ctx_alert.notice(con, run, now=1120) is None  # drops below and rearms
    transcript.write_text(_assistant({"input_tokens": 1200}) + "\n")
    assert ctx_alert.notice(con, run, now=1180) == (
        "ctx-high: orchestrator 1k > 1k; run /compactable at the next boundary; compact on COMPACT NOW"
    )


def test_notice_silently_skips_unbound_or_disabled_sessions(monkeypatch):
    con = _event_db()
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [])
    assert ctx_alert.notice(con, {"id": "run-1", "policy": {}}) is None
    assert ctx_alert.notice(con, {"id": "run-1", "policy": {"orchestrator_context": {"enabled": False}}}) is None


def test_notice_threshold_zero_disables_without_resolving_or_measuring(monkeypatch):
    con = _event_db()
    run = {"id": "run-1", "policy": {"orchestrator_context": {"threshold_tokens": 0}}}
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys",
                        lambda: (_ for _ in ()).throw(AssertionError("session lookup should be skipped")))
    monkeypatch.setattr(ctx_alert, "_transcript_path",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("transcript lookup should be skipped")))
    monkeypatch.setattr(ctx_alert, "context_tokens",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("transcript should not be measured")))

    assert ctx_alert.notice(con, run, now=1000) is None
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_notice_resolves_unique_claude_session_from_bound_herdr_root(tmp_path, monkeypatch):
    con = _event_db()
    con.executemany("INSERT INTO session_bindings VALUES(?, ?, 'run-1', NULL)", [
        ("herdr", "root-pane"), ("claude", "root-session"),
    ])
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(_assistant({"input_tokens": 500}) + "\n")
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [("herdr", "root-pane")])
    monkeypatch.setattr(ctx_alert, "_transcript_path", lambda _harness, _session: transcript)
    monkeypatch.setattr(ctx_alert.paths, "run_dir", lambda run_id: tmp_path / run_id)
    run = {"id": "run-1", "policy": {"orchestrator_context": {"threshold_tokens": 100}}}

    assert ctx_alert.notice(con, run, now=1000) == (
        "ctx-high: orchestrator 1k > 0k; run /compactable at the next boundary; compact on COMPACT NOW"
    )


def test_unresolved_transcript_is_silent_and_rate_limited(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_bindings(harness, session_id, run_id, ended_at)")
    con.execute("INSERT INTO session_bindings VALUES('claude', 'root-session', 'run-1', NULL)")
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [("claude", "root-session")])
    monkeypatch.setattr(ctx_alert, "_transcript_path", lambda *_args: None)
    monkeypatch.setattr(ctx_alert.paths, "run_dir", lambda run_id: tmp_path / run_id)
    run = {"id": "run-1", "policy": {}}

    assert ctx_alert.notice(con, run, now=1000) is None
    assert ctx_alert.notice(con, run, now=1010) is None


def test_ctx_high_event_wakes_wait_and_root_wake(env, monkeypatch):
    approved_run(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0])
    with db.transaction(con):
        con.execute("UPDATE tasks SET status='accepted' WHERE run_id=?", (run["id"],))
    pane = "fake-workspace:p-context"
    monkeypatch.setenv("HERDR_PANE_ID", pane)
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda _pane: ("root-claude", "idle"))
    with db.transaction(con):
        discovery.bind(con, run, [("herdr", pane)], "resume")
    assert "enabled" in root_wake.attach(con, run)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt",
                        lambda _name, text, **_kw: sent.append(text) or "landed")
    notice = "ctx-high: orchestrator 300k > 272k; run /compactable at the next boundary; compact on COMPACT NOW"
    with db.transaction(con):
        state.emit(con, run, "ctx.high", notice)

    code, output = env.office("wait", "--timeout", "5", "--poll", "0.1")
    assert code == 0 and notice in output

    assert root_wake.poll(con, run) == "delivered"
    assert len(sent) == 1 and notice in sent[0]
