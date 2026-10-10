"""3.6.1 regression: a Herdr pane must never receive another dispatch's brief.

The launch boundary is shared by parallel dispatch and `rerun --resume`.
Tests deliberately make Herdr report another task's cwd, agent, or session.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from office import dispatch, prompting
from office.state import Refused


def _run(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda run_id: tmp_path / run_id)
    return {"id": "r361"}


def _d(task="T1", serial="Da111"):
    return {"id": serial, "task_id": task, "session_id": "session-A", "worktree": "/tmp/office/T1",
            "launcher": "herdr", "pane_id": "w1:p1", "role": "executor"}


@pytest.mark.parametrize("field,actual", [
    ("cwd", "/tmp/office/T2"),
    ("foreground_cwd", "/tmp/office/T2"),
])
def test_other_worktree_refused_even_when_reserved_for_this_dispatch(monkeypatch, tmp_path, field, actual):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {field: actual}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert "pane w1:p1" in mismatch and "T2" in mismatch


def test_subdirectory_of_correct_worktree_is_permitted(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"foreground_cwd": "/tmp/office/T1/src"}})
    assert dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1")) is None


def test_sibling_with_same_prefix_is_not_this_worktree(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1-other"}})
    assert dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))


def test_other_agent_or_resumed_session_cannot_receive_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": {"name": "office-other", "session_id": "session-B"}}})
    assert "runs office-other" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")
    assert "session does not match" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-other")


def test_reserved_for_another_dispatch_refused_before_any_pane_commands(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    dest = tmp_path / run["id"]
    dest.mkdir()
    (dest / "herdr-reservations.json").write_text(json.dumps({"w1:p1": "Db222"}))
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: pytest.fail("setup reached wrong pane"))
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    notes = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notes.append(a[-1]))
    result = dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {},
                                          ([], "claude"), "w1:p1", Path("/tmp/office/T1"), dest)
    assert result is None
    assert any("pane-mismatch" in n for n in notes)


def test_wrong_cwd_after_setup_prevents_agent_start(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"foreground_cwd": "/tmp/office/T2"}})
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: True)
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch_form", lambda *a, **kw: pytest.fail("wrong agent started"))
    notes = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notes.append(a[-1]))
    assert dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {}, ([], "claude"),
                                       "w1:p1", Path("/tmp/office/T1"), tmp_path) is None
    assert "agent and brief were not started" in notes[-1]


def test_manual_prompt_refuses_foreign_pane(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(prompting, "_resolve", lambda *a: _d())
    monkeypatch.setattr(prompting.gates, "_agent_alive", lambda *a: True)
    monkeypatch.setattr(dispatch, "_pane_identity_mismatch", lambda *a, **kw: "wrong worktree")
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("prompt crossed task"))
    with pytest.raises(Refused, match="wrong worktree"):
        prompting.prompt(None, run, "T1", "fix this")


def test_parallel_pane_picks_keep_unique_reservations(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    def pick(_run, _cwd):
        file = tmp_path / run["id"] / "herdr-reservations.json"
        held = json.loads(file.read_text()) if file.exists() else {}
        return "w1:p1" if "w1:p1" not in held else "w1:p2"
    monkeypatch.setattr(dispatch, "_herdr_pick_pane", pick)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda did: dispatch._herdr_pane(run, Path("/tmp/office") / did,
                                                               dispatch_id=did), ["Da111", "Db222"]))
    assert {a, b} == {"w1:p1", "w1:p2"}
    assert set(json.loads((tmp_path / run["id"] / "herdr-reservations.json").read_text()).values()) == {"Da111", "Db222"}


def _mock_started_herdr(monkeypatch, tmp_path, observed):
    """Exercise the actual post-start delivery boundary with no harness process."""
    import subprocess
    calls = {"get": 0}
    def pane_get(_):
        calls["get"] += 1
        # Before `agent start` this is a clean shell. After it, Herdr
        # reports the actual agent identity (correct or cross-assigned).
        return {"pane": {"cwd": "/tmp/office/T1"}} if calls["get"] <= 2 else {"pane": observed}
    monkeypatch.setattr(dispatch, "_herdr_json", pane_get)
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: True)
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch_form", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_pane_agent_group", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_pane_ledger", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "pane_label", lambda *a, **kw: "T1 executor")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "{}", ""))
    class FakeWatcher:
        pid = 1234
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **kw: FakeWatcher())


def test_post_start_cross_task_agent_never_gets_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    _mock_started_herdr(monkeypatch, tmp_path, {
        "foreground_cwd": "/tmp/office/T1", "agent": "office-the-other-task"})
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **kw: pytest.fail("brief sent to wrong agent"))
    notices = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notices.append(a[-1]))
    spec = {"kind": "worker", "prompt_file": str(tmp_path / "brief.md"), "images": []}
    out = dispatch._herdr_agent_start(run, _d(), spec, {}, ([], "claude"), "w1:p1",
                                      Path("/tmp/office/T1"), tmp_path)
    assert out["launcher"] == "herdr" and out["prompt_landed"] is False
    assert "identity_failure" in spec and "brief NOT sent" in notices[-1]
    assert spec["pane_evidence"]["reported_agent"] == "office-the-other-task"


def test_post_start_matching_agent_receives_one_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    d = _d()
    agent = dispatch.herdr_agent_name(d["id"])
    _mock_started_herdr(monkeypatch, tmp_path, {"foreground_cwd": "/tmp/office/T1",
                                                "agent": {"name": agent, "session_id": "session-A"}})
    calls = []
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **kw: calls.append(a) or True)
    spec = {"kind": "worker", "prompt_file": str(tmp_path / "brief.md"), "images": []}
    out = dispatch._herdr_agent_start(run, d, spec, {}, ([], "claude"), "w1:p1",
                                      Path("/tmp/office/T1"), tmp_path)
    assert out["prompt_landed"] is True and len(calls) == 1
    assert spec["pane_evidence"]["reported_session"] == "session-A"


def test_rapid_resume_pane_claims_are_fenced_per_dispatch(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    def pick(_run, _cwd):
        f = tmp_path / run["id"] / "herdr-reservations.json"
        held = json.loads(f.read_text()) if f.exists() else {}
        return "w1:p3" if "w1:p3" not in held else "w1:p4"
    monkeypatch.setattr(dispatch, "_herdr_pick_pane", pick)
    resumed = [_d("T3", "Dc333"), _d("T1", "Da111")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        panes = list(pool.map(lambda d: dispatch._herdr_pane(run, Path(d["worktree"]), dispatch_id=d["id"]), resumed))
    assert panes[0] != panes[1]
    held = json.loads((tmp_path / run["id"] / "herdr-reservations.json").read_text())
    assert held[panes[0]] == "Dc333" and held[panes[1]] == "Da111"


@pytest.mark.parametrize("broken", ["{not-json", "[]", '{"w1:p1": 23}'])
def test_corrupt_reservation_ledger_fails_closed(monkeypatch, tmp_path, broken):
    run = _run(monkeypatch, tmp_path)
    root = tmp_path / run["id"]
    root.mkdir()
    (root / "herdr-reservations.json").write_text(broken)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert "reservation evidence" in mismatch


def test_existing_agent_prevents_shell_setup_even_if_cwd_is_correct(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": "office-somebody-else"}})
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: pytest.fail("wrong pane shell modified"))
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a, **kw: None)
    assert dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {},
                                       ([], "claude"), "w1:p1", Path("/tmp/office/T1"), tmp_path) is None


def test_manual_prompt_fences_reassignment_until_delivery_exits(monkeypatch, tmp_path):
    from threading import Event
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(prompting, "_resolve", lambda *a: _d())
    monkeypatch.setattr(prompting.gates, "_agent_alive", lambda *a: True)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    entered, proceed, changed = Event(), Event(), Event()

    def send(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=3)
        raise RuntimeError("finished sending")
    monkeypatch.setattr(dispatch, "submit_prompt", send)

    def move_reservation():
        assert entered.wait(timeout=3)
        with dispatch._pane_lock(run):
            changed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(prompting.prompt, None, run, "T1", "fix this")
        other = pool.submit(move_reservation)
        assert entered.wait(timeout=3)
        assert not changed.wait(timeout=0.05)  # assignment cannot interleave with send
        proceed.set()
        with pytest.raises(RuntimeError, match="finished sending"):
            future.result(timeout=3)
        other.result(timeout=3)
    assert changed.is_set()


def test_inspect_optional_pane_column_in_old_schema():
    import sqlite3
    from office import inspect_cmd
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE dispatches (id TEXT, session_id TEXT, resumed_from TEXT, harness TEXT)")
        assert "NULL AS pane_id" in inspect_cmd._optional_cols(con)
        con.execute("ALTER TABLE dispatches ADD COLUMN pane_id TEXT")
        assert "NULL AS pane_id" not in inspect_cmd._optional_cols(con)
    finally:
        con.close()


def test_empty_reservation_owner_is_not_authority(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    p = tmp_path / run["id"] / "herdr-reservations.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"w1:p1": " "}))
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    assert "malformed" in dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))


def test_unidentified_agent_is_not_a_safe_shell(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": {"session_id": "session-B"}}})
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), check_cwd=False)
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")


def test_reported_control_chars_are_not_rendered(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    control = chr(27) + "[31m"
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/other" + control}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1" + control))
    assert control not in mismatch
    held = tmp_path / run["id"] / "herdr-reservations.json"
    held.parent.mkdir(exist_ok=True)
    held.write_text(json.dumps({"w1:p1": "other" + control}))
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert control not in mismatch


def test_empty_agent_object_is_unverified(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1", "agent": {}}})
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), check_cwd=False)
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")
