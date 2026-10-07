"""`office wait` ends only for what the orchestrator has not been shown (#296, #329), and an
authority entry left unauthorized after its action left the plan can be dropped on the record.

Exit 0: a new actionable event, a task or phase change, or a pending item first seen. An unchanged
pending item already shown, or informational events alone, keep it blocking until the timeout (124)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
WAIT = ("wait", "--timeout", "1", "--poll", "0.2")
ENTRY = {"id": "X1", "action": "deploy preview", "preconditions": ["tests pass"], "needs_authorization": True}
PLAN_ACTION = PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nactions:\n- deploy preview | preconditions: tests pass\n")


def _go(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("status", check=0)  # consume the dispatch events


def _wait(env):
    return env.office(*WAIT, env=EXTERNAL)


def _emit(env, kind, summary):
    from office import state
    con = env.con()
    run = dict(con.execute("SELECT id FROM runs").fetchone())
    with con:
        state.emit(con, state.get_run(con, run["id"]), kind, summary, audience="orchestrator")
    con.close()


def _set_envelope(env, envelope):
    from office import state
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    with con:
        state.update_run(con, run_id, envelope=envelope)
    con.close()


def _envelope(env):
    con = env.con()
    try:
        return json.loads(con.execute("SELECT envelope_json FROM runs").fetchone()[0])
    finally:
        con.close()


# ---- #296 / #329: a pending item ends the wait once

@pytest.mark.approved
def test_a_pending_item_ends_wait_once_and_the_repeat_blocks(env):
    _go(env)
    env.office("revoke", "T1", check=0)  # T1 is blocked: the orchestrator has not seen that
    code, out = _wait(env)
    assert code == 0 and "blocker:" in out, out
    for _ in range(2):  # unchanged and already reported: no immediate exit on repeated calls
        code, out = _wait(env)
        assert code == 124 and "nothing new" in out, out


@pytest.mark.approved
def test_status_counts_as_showing_the_pending_item(env):
    _go(env)
    env.office("revoke", "T1", check=0)
    code, out = env.office("status", check=0)
    assert "blocker:" in out
    code, out = _wait(env)
    assert code == 124, out


@pytest.mark.approved
def test_a_different_pending_item_ends_wait_again(env):
    _go(env)
    env.office("revoke", "T1", check=0)
    assert _wait(env)[0] == 0
    assert _wait(env)[0] == 124
    _set_envelope(env, [ENTRY])  # a new authority entry now also waits on the orchestrator
    code, out = _wait(env)
    assert code == 0 and "X1" in out, out
    assert _wait(env)[0] == 124


@pytest.mark.approved
def test_a_pending_item_that_cleared_and_returned_is_new_again(env):
    _go(env)
    _set_envelope(env, [ENTRY])
    assert _wait(env)[0] == 0
    assert _wait(env)[0] == 124
    _set_envelope(env, [])
    env.office("status", check=0)  # nothing pending is shown
    _set_envelope(env, [ENTRY])
    assert _wait(env)[0] == 0


# ---- informational events never end it alone; actionable ones do

@pytest.mark.approved
def test_informational_events_alone_keep_wait_blocking(env):
    _go(env)
    _emit(env, "dispatch", "something happened that needs nothing")
    _emit(env, "setup.done", "worktree setup ok")
    code, out = _wait(env)
    assert code == 124 and "nothing new" in out, out
    assert "something happened" in out  # shown when it returns, not a reason to return


@pytest.mark.approved
def test_an_actionable_event_ends_wait(env):
    _go(env)
    _emit(env, "gate.attention", "T1 reviewer left no readable reply")
    code, out = _wait(env)
    assert code == 0 and "reviewer left no readable reply" in out, out
    assert _wait(env)[0] == 124  # shown and consumed


@pytest.mark.approved
def test_an_informational_event_does_not_hide_a_later_actionable_one(env):
    _go(env)
    _emit(env, "dispatch", "fyi")
    _emit(env, "job.failed", "outbox job failed: boom")
    code, out = _wait(env)
    assert code == 0 and "boom" in out, out


# ---- an unauthorized envelope entry whose action left the plan

@pytest.mark.approved
def test_a_stale_envelope_entry_is_pending_until_declined_then_it_is_not(env):
    _go(env)
    _set_envelope(env, [ENTRY])
    code, out = env.office("status", check=0)
    assert "X1 (deploy preview) needs authorization" in out
    assert 'office decline X1 --reason' in out  # the plan no longer names the action
    code, out = env.office("decline", "X1", "--reason", "the preview deploy was dropped from the plan", check=0)
    assert "X1 (deploy preview) dropped" in out
    assert _envelope(env) == []
    con = env.con()
    row = dict(con.execute("SELECT kind, target, authorized_by, quote FROM authorizations WHERE kind='envelope-decline'").fetchone())
    assert row == {"kind": "envelope-decline", "target": "X1", "authorized_by": "orchestrator",
                   "quote": "the preview deploy was dropped from the plan"}
    code, out = env.office("status", check=0)
    assert "X1" not in out
    assert _wait(env)[0] == 124  # nothing pending any more


@pytest.mark.approved
def test_declining_needs_a_reason_and_a_known_unauthorized_entry(env):
    _go(env)
    _set_envelope(env, [ENTRY, {**ENTRY, "id": "X2", "action": "ship it", "needs_authorization": False}])
    code, out = env.office("decline", "X1")
    assert code != 0 and "reason" in out and _envelope(env)[0]["id"] == "X1", out
    code, out = env.office("decline", "X9", "--reason", "gone")
    assert code != 0 and "no authority entry X9" in out, out
    code, out = env.office("decline", "X2", "--reason", "gone")
    assert code != 0 and "is authorized" in out and len(_envelope(env)) == 2, out


def test_an_entry_the_plan_still_names_cannot_be_declined(env):
    approved_run(env, plan=PLAN_ACTION, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    _set_envelope(env, [ENTRY])
    code, out = env.office("decline", "X1", "--reason", "changed my mind")
    assert code != 0 and "still named by the plan" in out and _envelope(env) == [ENTRY], out
    code, out = env.office("status", check=0)
    assert "office decline" not in out  # the hint is only for an entry the plan dropped


@pytest.mark.approved
def test_a_worker_cannot_decline(env):
    _go(env)
    _set_envelope(env, [ENTRY])
    code, out = env.office("decline", "X1", "--reason", "x", env={**EXTERNAL, "OFFICE_DISPATCH_ID": "D1"})
    assert code != 0 and "worker cannot" in out and _envelope(env) == [ENTRY], out


@pytest.mark.approved
def test_new_amendment_entries_never_reuse_a_dropped_id(env):
    from office import amend, state
    _go(env)
    _set_envelope(env, [{**ENTRY, "needs_authorization": False}, {**ENTRY, "id": "X2", "action": "second"}])
    env.office("decline", "X2", "--reason", "gone", check=0)
    _set_envelope(env, [{**ENTRY, "id": "X1", "needs_authorization": False}, {**ENTRY, "id": "X3", "action": "third"}])
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    parsed = SimpleNamespace(requirements={"named_actions": [{"action": "fourth", "preconditions": []}]})
    with con:
        flagged = amend._envelope_changes(con, run, parsed)
    assert flagged == ["X4"], flagged


# ---- what the orchestrator was actually shown

@pytest.mark.approved
def test_events_shown_while_an_older_one_is_held_do_not_end_wait_again(env):
    """Piggyback shows urgent events ahead of an older informational one and remembers each by seq."""
    _go(env)
    _emit(env, "dispatch", "fyi, older")
    for n in range(4):
        _emit(env, "gate.attention", f"urgent {n}")
    code, out = env.office("inspect", "run", check=0)
    assert all(f"urgent {n}" in out for n in range(4)), out
    code, out = _wait(env)
    assert code == 124, out


@pytest.mark.approved
def test_an_actionable_event_behind_a_backlog_of_informational_ones_still_ends_wait(env):
    _go(env)
    for n in range(210):
        _emit(env, "dispatch", f"fyi {n}")
    _emit(env, "task.paused", "T1 needs you")
    code, out = _wait(env)
    assert code == 0 and "T1 needs you" in out, out  # urgent first, not the 6 oldest


@pytest.mark.approved
def test_the_session_start_hook_does_not_count_as_showing_the_pending_item(env):
    """The hook cuts status to a byte budget: a pending item it may have cut off is still new to `wait`."""
    import subprocess
    import sys
    _go(env)
    _set_envelope(env, [ENTRY])
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    con.execute("INSERT OR REPLACE INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) "
                "VALUES('claude','s1',?,'2026-01-01','test')", (run_id,))
    con.commit()
    sessions = env.repo / ".office" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "claude-s1.json").write_text(json.dumps({"run_id": run_id}))
    payload = json.dumps({"session_id": "s1", "cwd": str(env.repo), "source": "startup"})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "SessionStart", "--harness", "claude", "--office-managed"],
                          input=payload, capture_output=True, text=True, cwd=env.repo)
    assert proc.returncode == 0 and "X1" in proc.stdout, (proc.stdout, proc.stderr)
    code, out = _wait(env)
    assert code == 0 and "X1" in out, out


# ---- entry ids and the plan key

@pytest.mark.approved
def test_declining_the_highest_entry_never_frees_its_id(env):
    from office import amend, state
    _go(env)
    _set_envelope(env, [{**ENTRY, "needs_authorization": False}, {**ENTRY, "id": "X2", "action": "second"}])
    env.office("decline", "X2", "--reason", "gone", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    parsed = SimpleNamespace(requirements={"named_actions": [{"action": "third", "preconditions": []}]})
    with con:
        assert amend._envelope_changes(con, run, parsed) == ["X3"]


def test_an_entry_whose_preconditions_the_plan_changed_can_be_declined(env):
    """amend keys entries on (action, preconditions): the plan naming the action under other
    preconditions is a different entry, so the old one has left the plan."""
    approved_run(env, plan=PLAN_ACTION, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    _set_envelope(env, [{**ENTRY, "preconditions": ["old precondition"]}])
    code, out = env.office("decline", "X1", "--reason", "preconditions changed", check=0)
    assert "dropped" in out and _envelope(env) == [], out


# ---- events: the cursor never skips what was not shown

@pytest.mark.approved
def test_status_shows_urgent_events_first_and_never_skips_the_informational_ones_between(env):
    """Urgent events beyond the 200-event window show too; the cursor must not jump over the
    informational events between the window and them."""
    _go(env)
    _emit(env, "dispatch", "fyi first")
    for n in range(199):  # the whole window is urgent: shown first, remembered by seq
        _emit(env, "gate.attention", f"urgent {n}")
    for n in range(100):
        _emit(env, "dispatch", f"gap {n}")
    _emit(env, "task.paused", "far urgent")
    code, out = env.office("status", check=0)
    assert out.splitlines()[1:7] and "· urgent 0" in out and "· gap 0" not in out, out
    shown = out
    for _ in range(80):
        shown += env.office("status", check=0)[1]
    missing = [n for n in range(100) if f"· gap {n}\n" not in shown]
    assert not missing and "· far urgent\n" in shown and "· fyi first\n" in shown, missing[:5]


@pytest.mark.approved
def test_status_without_record_shows_events_but_leaves_them_unread(env):
    from office import guide, state
    _go(env)
    _emit(env, "gate.attention", "urgent one")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    res = guide.status(con, run, record=False)
    assert "· urgent one" in res.lines
    assert _wait(env)[0] == 0  # the orchestrator was not shown it: still news
