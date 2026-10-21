"""Orchestrator context-high parsing, episode, and notice behavior."""
import json
import sqlite3

from office import ctx_alert


def _assistant(usage, **extra):
    return json.dumps({"type": "assistant", "message": {"usage": usage}, **extra})


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
        "ctx-high: orchestrator 231k > 200k; run /compactable at a boundary"
    )
    assert ctx_alert.tick(high, 250000, 200000) == (True, None)
    assert ctx_alert.tick(high, None, 200000) == (True, None)
    high, notice = ctx_alert.tick(high, 40000, 200000)
    assert (high, notice) == (False, None)
    assert ctx_alert.tick(high, 210000, 200000) == (True,
        "ctx-high: orchestrator 210k > 200k; run /compactable at a boundary"
    )


def test_notice_uses_bound_transcript_low_threshold_and_is_rate_limited(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_bindings(harness, session_id, run_id, ended_at)")
    con.execute("INSERT INTO session_bindings VALUES('claude', 'root-session', 'run-1', NULL)")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(_assistant({"input_tokens": 400, "cache_read_input_tokens": 700}) + "\n")
    state_dir = tmp_path / "runs"
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [("claude", "root-session")])
    monkeypatch.setattr(ctx_alert, "_transcript_path", lambda harness, session: transcript)
    monkeypatch.setattr(ctx_alert.paths, "run_dir", lambda run_id: state_dir / run_id)
    run = {"id": "run-1", "policy": {"orchestrator_context": {"threshold_tokens": 1000}}}

    assert ctx_alert.notice(con, run, now=1000) == (
        "ctx-high: orchestrator 1k > 1k; run /compactable at a boundary"
    )
    assert ctx_alert.notice(con, run, now=1059) is None
    assert ctx_alert.notice(con, run, now=1060) is None  # same episode

    transcript.write_text(_assistant({"input_tokens": 200}) + "\n")
    assert ctx_alert.notice(con, run, now=1120) is None  # drops below and rearms
    transcript.write_text(_assistant({"input_tokens": 1200}) + "\n")
    assert ctx_alert.notice(con, run, now=1180) == (
        "ctx-high: orchestrator 1k > 1k; run /compactable at a boundary"
    )


def test_notice_silently_skips_unbound_or_disabled_sessions(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_bindings(harness, session_id, run_id, ended_at)")
    monkeypatch.setattr(ctx_alert.discovery, "fast_session_keys", lambda: [])
    assert ctx_alert.notice(con, {"id": "run-1", "policy": {}}) is None
    assert ctx_alert.notice(con, {"id": "run-1", "policy": {"orchestrator_context": {"enabled": False}}}) is None


def test_notice_resolves_unique_claude_session_from_bound_herdr_root(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_bindings(harness, session_id, run_id, ended_at)")
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
        "ctx-high: orchestrator 1k > 0k; run /compactable at a boundary"
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
