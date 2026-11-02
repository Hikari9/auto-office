"""Wake coalescing and progress suppression on top of #723 (at-least-once delivery is kept).

A newer actionable wake waits while an earlier one is delivered but unobserved, and goes out once, with the latest
event id, when the root observes or the window ends. Gate progress (queued/running) never re-wakes a root whose
next action is unchanged. All local: no paid models or real sessions.
"""
from __future__ import annotations

import re

from conftest import approved_run
from office import db, dispatch, root_wake, state
from office.util import now_iso

from test_root_wake_723 import events, setup_root


def block(con, run, reason, kind="task.blocked"):
    with db.transaction(con):
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason=reason)
        return state.emit(con, run, kind, f"T1 blocked: {reason}")


def through(text):
    return int(re.search(r"through event (\d+)", text).group(1))


def sender(monkeypatch):
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, text, **kw: sent.append(text) or "landed")
    return sent


def test_newer_wake_is_held_while_the_first_is_unobserved_then_one_wake_carries_the_latest_event(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    sent = sender(monkeypatch)
    block(con, run, "first decision")
    assert root_wake.poll(con, run) == "delivered" and len(sent) == 1
    block(con, run, "second decision")
    block(con, run, "third decision")
    assert root_wake.poll(con, run) == "no-action" and len(sent) == 1  # held, not sent
    held = [x for x in events(con, run) if x["state"] == "pending"]
    assert held, "held receipts stay pending so nothing is lost"
    root_wake.observe(con, run)  # the root read fresh state
    assert root_wake.poll(con, run) == "delivered" and len(sent) == 2
    latest = con.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run["id"],)).fetchone()[0]
    assert through(sent[1]) == max(x["event_seq"] for x in held) <= latest
    assert not [x for x in events(con, run) if x["state"] in ("pending", "retry")]
    assert root_wake.poll(con, run) == "no-action" and len(sent) == 2


def test_an_unobserved_wake_does_not_lose_a_later_actionable_event_when_the_window_ends(env, monkeypatch):
    """The safety net: the root never observed, so the held wake is delivered after the window. No lost wake."""
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    sent = sender(monkeypatch)
    block(con, run, "first decision")
    assert root_wake.poll(con, run) == "delivered"
    block(con, run, "second decision")
    assert root_wake.poll(con, run) == "no-action" and len(sent) == 1
    with db.transaction(con):  # the window passes with the root still silent
        con.execute("UPDATE root_wake_intents SET delivered_at='2000-01-01T00:00:00+00:00' "
                    "WHERE run_id=? AND state='delivered'", (run["id"],))
    assert root_wake.poll(con, run) == "delivered" and len(sent) == 2
    latest = con.execute("SELECT MAX(seq) FROM events WHERE run_id=? AND kind='task.blocked'", (run["id"],)).fetchone()[0]
    assert through(sent[1]) >= latest


def test_window_is_configurable_and_zero_disables_holding(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setenv("OFFICE_WAKE_COALESCE_SECONDS", "0")
    sent = sender(monkeypatch)
    block(con, run, "first decision")
    assert root_wake.poll(con, run) == "delivered"
    block(con, run, "second decision")
    assert root_wake.poll(con, run) == "delivered" and len(sent) == 2


def gate(con, run, gid, status, verdict=None):
    with db.transaction(con):
        con.execute("INSERT OR REPLACE INTO gates(id, run_id, subject, task_id, kind, input_key, status, verdict, "
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (gid, run["id"], "task", "T1", "review", "k", status, verdict, now_iso()))


def test_gate_progress_does_not_change_the_wake_signature_but_a_result_does(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    block(con, run, "needs a decision")
    base = root_wake._authoritative_action_signature(con, run)
    gate(con, run, "g1", "queued")
    assert root_wake._authoritative_action_signature(con, run) == base
    gate(con, run, "g1", "running")
    assert root_wake._authoritative_action_signature(con, run) == base
    gate(con, run, "g1", "done", "APPROVED")
    assert root_wake._authoritative_action_signature(con, run) != base


def test_progress_only_transitions_settle_without_a_prompt(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    sent = sender(monkeypatch)
    block(con, run, "needs a decision")
    assert root_wake.poll(con, run) == "delivered"
    root_wake.observe(con, run)
    gate(con, run, "g1", "queued")
    gate(con, run, "g1", "running")
    with db.transaction(con):
        state.emit(con, run, "gate.attention", "review running")
    assert root_wake.poll(con, run) == "no-action" and len(sent) == 1
    assert events(con, run)[-1]["state"] == "settled"


def test_a_confirmed_stall_is_never_held_behind_an_unobserved_wake(env, monkeypatch):
    from office import guide, questions
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    sent = sender(monkeypatch)
    block(con, run, "first decision")
    assert root_wake.poll(con, run) == "delivered"
    monkeypatch.setattr(questions, "scan", lambda *a, **kw: ([], [], {}))
    monkeypatch.setattr(guide, "stalls", lambda *a, **kw: ["T1 reviewer D-test no output or progress for 900s"])
    assert root_wake.watchdog_tick(con, run) == "alerted"
    assert root_wake.poll(con, run) == "delivered" and len(sent) == 2
