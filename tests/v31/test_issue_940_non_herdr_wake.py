"""#940: a verified root in plain Claude Code wakes on native Stop asyncRewake.

All tests use a fake process identity and isolated Office DB. No real Claude,
Herdr, model API or user settings are touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import approved_run
from office import db, discovery, hooks, install, root_wake, state

KEY = "claude:4242:mon-oct-31"
SESSION = "native-claude-session-A"


def _run(con):
    return state.get_run(con, con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0])


def _setup(env, monkeypatch):
    approved_run(env)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    monkeypatch.setattr(discovery, "process_key", lambda: KEY)
    monkeypatch.setattr(root_wake, "_native_hook_installed", lambda: True)
    monkeypatch.setattr(root_wake, "_watchdog_loop", lambda _rid: None)
    con = env.con()
    run = _run(con)
    with db.transaction(con):
        discovery.bind(con, run, [("proc", KEY)], "resume")
    assert "Claude native Stop/asyncRewake" in root_wake.attach(con, run)
    return con, run


def _event(con, run, text="review ready"):
    with db.transaction(con):
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason=text)
        state.emit(con, run, "task.blocked", text, audience="orchestrator")


def _intents(con, run):
    return [dict(r) for r in con.execute("SELECT * FROM root_wake_intents WHERE run_id=? ORDER BY event_seq",
                                         (run["id"],))]


def test_native_stop_hook_is_installed_with_async_rewake_and_long_timeout():
    assert install.EVENTS["claude"]["turn.stop"] == "Stop"
    assert "turn.stop" in install.install_items("claude")
    hook = install._entry("claude", "turn.stop", "/bin/office")
    assert hook["hooks"] == [{"type": "command", "command": "/bin/office hook turn.stop --harness claude --office-managed",
                               "timeout": 86400, "asyncRewake": True}]
    assert "matcher" not in hook
    assert "asyncRewake" not in install._entry("claude", "prompt.submit", "/bin/office")["hooks"][0]


def test_plain_shell_and_missing_stop_hook_fail_closed(env, monkeypatch):
    approved_run(env)
    con = env.con()
    run = _run(con)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(discovery, "process_key", lambda: None)
    with pytest.raises(state.Refused, match="no verified Claude process"):
        root_wake.attach(con, run)
    monkeypatch.setattr(discovery, "process_key", lambda: KEY)
    with db.transaction(con):
        discovery.bind(con, run, [("proc", KEY)], "resume")
    monkeypatch.setattr(root_wake, "_native_hook_installed", lambda: False)
    assert "not armed" in root_wake.arm_on_resume(con, run)
    assert not root_wake.armed(con, run)
    assert "hook missing" in root_wake.lines(con, run)[0]
    monkeypatch.setattr(root_wake, "_native_hook_installed", lambda: True)
    with db.transaction(con):
        con.execute("UPDATE session_bindings SET ended_at=? WHERE harness='proc' AND session_id=?",
                    ("2026-01-01", KEY))
    with pytest.raises(state.Refused, match="not bound"):
        root_wake.attach(con, run)


def test_native_wake_emits_exit_two_only_for_real_action_and_observes_after_context(env, monkeypatch, capsys):
    con, run = _setup(env, monkeypatch)
    with db.transaction(con):
        state.emit(con, run, "launch.session", "informational", audience="orchestrator")
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.04,
                                      poll_seconds=0.01) == 0
    assert _intents(con, run) == []
    _event(con, run)
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.1,
                                      poll_seconds=0.01) == 2
    err = capsys.readouterr().err
    assert f"office --run {run['id']} context" in err
    assert "Do not infer success" in err
    intents = _intents(con, run)
    assert len(intents) == 1 and intents[0]["state"] == "delivered"
    assert intents[0]["delivered_at"] and not intents[0]["observed_at"]
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.03,
                                      poll_seconds=0.01) == 0
    assert capsys.readouterr().err == ""  # never duplicate the previous wake
    root_wake.observe(con, run)
    assert _intents(con, run)[0]["state"] == "observed"
    assert root_wake.metrics(con, run)["transport"] == "claude-asyncRewake"


def test_native_stop_ignores_workers_missing_session_and_wrong_session(env, monkeypatch):
    con, run = _setup(env, monkeypatch)
    assert root_wake.native_stop_hook({"cwd": str(env.repo)}) == 0
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "worker-a")
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}) == 0
    monkeypatch.delenv("OFFICE_DISPATCH_ID")
    _event(con, run)
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.1,
                                      poll_seconds=0.01) == 2
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": "different-session"}) == 0
    assert len(_intents(con, run)) == 1


def test_wake_off_and_binding_move_cannot_wake_stale_root(env, monkeypatch, capsys):
    con, run = _setup(env, monkeypatch)
    _event(con, run)
    assert "disabled" in root_wake.attach(con, run, off=True)
    assert not root_wake.armed(con, run)
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}) == 0
    assert capsys.readouterr().err == ""
    assert "off (--wake-off)" in root_wake.lines(con, run)[0]
    assert "off" in root_wake.arm_on_resume(con, run)
    assert "Claude native" in root_wake.arm_on_resume(con, run, explicit=True)
    with db.transaction(con):
        con.execute("UPDATE session_bindings SET ended_at='2026-01-01' WHERE harness='proc' AND session_id=?", (KEY,))
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}) == 0
    assert capsys.readouterr().err == ""


def test_new_user_prompt_invalidates_idle_watcher_and_keeps_queued_receipt(env, monkeypatch):
    con, run = _setup(env, monkeypatch)
    root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.01,
                               poll_seconds=0.01)
    before = root_wake._binding(con, run["id"])["generation"]
    root_wake.native_prompt_started({"session_id": SESSION})
    assert root_wake._binding(con, run["id"])["generation"] == before + 1
    _event(con, run)
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}, timeout=0.1,
                                      poll_seconds=0.01) == 2


def test_wrong_proc_binding_does_not_pick_a_sole_active_run(env, monkeypatch):
    con, run = _setup(env, monkeypatch)
    _event(con, run)
    monkeypatch.setattr(discovery, "process_key", lambda: "claude:someone-else:other")
    assert root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION}) == 0
    assert not _intents(con, run)


def test_hooks_stop_dispatches_without_a_native_session_file(env, monkeypatch, capsys):
    con, run = _setup(env, monkeypatch)
    _event(con, run)
    assert not (env.repo / ".office" / "sessions" / f"claude-{SESSION}.json").exists()
    monkeypatch.setattr(hooks, "_read_stdin", lambda: {"cwd": str(env.repo), "session_id": SESSION})
    monkeypatch.setattr(root_wake, "native_stop_hook",
                        lambda payload: 2 if payload["session_id"] == SESSION else 0)
    assert hooks.main(["turn.stop", "--harness", "claude", "--office-managed"]) == 2


def test_native_status_shows_transport_and_listener_state(env, monkeypatch):
    con, run = _setup(env, monkeypatch)
    text = root_wake.lines(con, run)[0]
    assert "wake: armed (Claude native Stop/asyncRewake" in text
    assert "arms on next Claude Stop" in text
    assert root_wake.armed(con, run)


def test_worker_completion_after_root_is_idle_wakes_without_a_user_nudge(env, monkeypatch, capsys):
    """Native async Stop-hook process is already waiting when Office emits news."""
    import threading
    import time

    con, run = _setup(env, monkeypatch)
    outcome = []

    def listener():
        outcome.append(root_wake.native_stop_hook({"cwd": str(env.repo), "session_id": SESSION},
                                                   timeout=3, poll_seconds=0.01))

    thread = threading.Thread(target=listener)
    thread.start()
    try:
        for _ in range(100):
            if root_wake.metrics(con, run)["listener_active"]:
                break
            time.sleep(0.01)
        assert root_wake.metrics(con, run)["listener_active"]
        _event(con, run, "executor completed with a question")
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert outcome == [2]
        assert "office --run" in capsys.readouterr().err
        assert _intents(con, run)[0]["state"] == "delivered"
    finally:
        thread.join(timeout=1)


def test_disable_during_idle_listener_quiets_it_before_next_event(env, monkeypatch, capsys):
    import threading
    import time

    con, run = _setup(env, monkeypatch)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(root_wake.native_stop_hook(
        {"cwd": str(env.repo), "session_id": SESSION}, timeout=3, poll_seconds=0.01)))
    thread.start()
    try:
        for _ in range(100):
            if root_wake.metrics(con, run)["listener_active"]:
                break
            time.sleep(0.01)
        assert root_wake.metrics(con, run)["listener_active"]
        assert "disabled" in root_wake.attach(con, run, off=True)
        _event(con, run, "event after off")
        thread.join(timeout=3)
        assert outcome == [0]
        assert capsys.readouterr().err == ""
        assert not _intents(con, run)
    finally:
        thread.join(timeout=1)


def test_proc_rebind_waits_until_native_wake_has_finished_its_final_check(env, monkeypatch):
    """No new run can capture this Claude process between final check and wake."""
    import threading
    import time

    con, run = _setup(env, monkeypatch)
    _event(con, run)
    entered, release = threading.Event(), threading.Event()
    original = root_wake._wake_message

    def paused_message(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(root_wake, "_wake_message", paused_message)
    delivery, moves = [], []
    wake_thread = threading.Thread(target=lambda: delivery.append(root_wake.native_stop_hook(
        {"cwd": str(env.repo), "session_id": SESSION}, timeout=3, poll_seconds=0.01)))

    def move_binding():
        other = env.con()
        try:
            with root_wake.rebind_fence(other, [("proc", KEY)]), db.transaction(other):
                other.execute("UPDATE session_bindings SET ended_at='2026-01-01' "
                              "WHERE harness='proc' AND session_id=?", (KEY,))
                moves.append("moved")
        finally:
            other.close()

    wake_thread.start()
    move_thread = None
    try:
        assert entered.wait(3)
        move_thread = threading.Thread(target=move_binding)
        move_thread.start()
        time.sleep(0.05)
        assert moves == []  # rebind cannot pass the in-flight wake fence
        release.set()
        wake_thread.join(3)
        move_thread.join(3)
        assert not wake_thread.is_alive() and not move_thread.is_alive()
        assert delivery == [2] and moves == ["moved"]
    finally:
        release.set()
        wake_thread.join(1)
        if move_thread is not None:
            move_thread.join(1)
