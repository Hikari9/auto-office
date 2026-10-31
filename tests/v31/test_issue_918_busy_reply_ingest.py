"""Issue #918: a live reviewer's written reply is ingested once it settles, even while its pane reads busy.
A reply still changing is not ingested. While a reply waits to settle, status and wait say so in one line; past the
hard bound, `next:` names the relaunch on the same revision."""
from __future__ import annotations

import os
import time

import pytest

from office import dispatch as dispatch_mod
from office import guide, paths
from test_herdr_agent_launch import _fake, _live_dispatch, _spec

BUSY = {"status": "working", "content_hash": "h", "busy": True, "text_busy": True}


class Stop(Exception):
    """Ends a watch that would otherwise poll forever."""


def _pane_samples(monkeypatch, sample_for_poll, limit=500):
    calls = []

    def sample(name):
        calls.append(name)
        if len(calls) > limit:
            raise Stop()
        return sample_for_poll(len(calls))

    monkeypatch.setattr(dispatch_mod, "_herdr_agent_sample", sample)
    return calls


@pytest.mark.approved
def test_a_reply_that_settles_is_ingested_while_the_pane_reads_busy(env, monkeypatch):
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: APPROVED\nNEXT: merge it")  # complete under the convergence contract the runs pin
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0.2")
    stamps = []
    _pane_samples(monkeypatch, lambda n: stamps.append(time.monotonic()) or dict(BUSY))
    assert dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01) == (0, "success")
    # Ingested only after the reply held still for the settle window; one or two polls would finish in ~0.02 s.
    assert stamps[-1] - stamps[0] >= 0.15, stamps


@pytest.mark.approved
def test_an_incomplete_reply_is_not_ingested_while_the_pane_reads_busy(env, monkeypatch):
    """A6: the busy-settle ingest needs a complete verdict; a byte-identical draft without one waits."""
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text("Still checking the diff; no verdict yet.")
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0.05")
    _pane_samples(monkeypatch, lambda n: dict(BUSY), limit=40)
    with pytest.raises(Stop):  # still polling after the settle window: never returned success
        dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01)


@pytest.mark.approved
@pytest.mark.parametrize("reply, ingested", [
    # Complete only under convergence-v1: a RECHECK verdict, with its NEXT and a blocking FINDING.
    ("VERDICT: RECHECK\nNEXT: fix F1\nFINDING F1 | high | blocking | a.py:1 | bad | fix it", True),
    # Complete under v3.1 but not under convergence-v1, which needs a NEXT line: the contract must be consulted.
    ("VERDICT: APPROVED\n", False),
])
def test_the_run_contract_decides_whether_a_busy_reply_is_complete(env, monkeypatch, reply, ingested):
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text(reply)
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0.05")
    _pane_samples(monkeypatch, lambda n: dict(BUSY), limit=40)
    if ingested:
        assert dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01) == (0, "success")
    else:
        with pytest.raises(Stop):
            dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01)


@pytest.mark.approved
def test_a_same_size_rewrite_of_a_complete_reply_is_not_ingested_while_busy(env, monkeypatch):
    """Two complete, same-length verdicts alternate: only the byte digest can tell the rewrites apart."""
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    drafts = ["VERDICT: APPROVED\nNEXT: merge it", "VERDICT: APPROVED\nNEXT: merge IT"]
    out.write_text(drafts[0])
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0")

    def flip(n):
        out.write_text(drafts[n % 2])  # same size every poll, bytes differ
        return dict(BUSY)

    _pane_samples(monkeypatch, flip, limit=40)
    with pytest.raises(Stop):
        dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01)


class _Clock:
    """`time` as the watch sees it. `wall` and `mono` are offsets added to the real clocks. A laptop sleep moves only
    `wall`, matching macOS, where the monotonic clock stops during sleep. A slow poll moves both."""

    def __init__(self):
        self.wall = 0.0
        self.mono = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return time.time() + self.wall

    def monotonic(self):
        return time.monotonic() + self.mono


@pytest.mark.approved
@pytest.mark.parametrize("jump", [120.0, -3600.0], ids=["sleep", "clock-stepped-back"])
def test_a_suspend_or_clock_step_restarts_the_settle_window(env, monkeypatch, jump):
    """Quiet time counts only across polls the watch saw: after a sleep or a backward step the window starts over."""
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: APPROVED\nNEXT: merge it")
    clock = _Clock()
    monkeypatch.setattr(dispatch_mod, "time", clock)
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0.2")

    def jump_after_first_poll(n):
        if n == 1:
            clock.wall += jump
        return dict(BUSY)

    calls = _pane_samples(monkeypatch, jump_after_first_poll, limit=2000)
    assert dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01) == (0, "success")
    # The first poll after the jump only restarts the window: an ingest there would be the bug.
    assert len(calls) >= 3, calls


@pytest.mark.approved
def test_a_slow_pane_read_does_not_restart_the_settle_window(env, monkeypatch):
    """A pane read that takes 20 s moves both clocks: the gap between polls is that read's own time, not a jump, so
    the quiet window keeps counting. The window is longer than one poll's 20 s, so a window restarted every poll
    never fills; the reply ingests on the second poll, once the reads have added up to the window."""
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: APPROVED\nNEXT: merge it")
    clock = _Clock()
    monkeypatch.setattr(dispatch_mod, "time", clock)
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "30")

    def slow_read(n):
        clock.wall += 20.0
        clock.mono += 20.0
        return dict(BUSY)

    calls = _pane_samples(monkeypatch, slow_read, limit=50)
    assert dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01) == (0, "success")
    assert len(calls) == 2, calls


@pytest.mark.approved
@pytest.mark.parametrize("raw, expected", [("nan", 600.0), ("inf", 600.0), ("abc", 600.0), ("-2", 0.0), ("0.5", 0.5)],
                         ids=["nan", "inf", "junk", "negative", "fraction"])
def test_the_hard_bound_env_is_read_as_seconds_and_junk_falls_back(monkeypatch, raw, expected):
    monkeypatch.setenv("OFFICE_REPLY_HARD_BOUND_S", raw)
    assert dispatch_mod.reply_hard_bound() == expected


@pytest.mark.approved
def test_a_reply_still_changing_is_not_ingested_while_busy(env, monkeypatch):
    _fake(env, monkeypatch)
    _, d = _live_dispatch(env, monkeypatch)
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: 0")
    monkeypatch.setenv("OFFICE_REPLY_BUSY_SETTLE_S", "0")  # no settle window: only byte-identity can hold it back

    def rewrite_then_gone(n):
        if n > 5:
            return None  # the agent exits; the watch ends on what it saw
        out.write_text(f"VERDICT: {n}")  # same size every poll: only the bytes differ
        return dict(BUSY)

    calls = _pane_samples(monkeypatch, rewrite_then_gone)
    assert dispatch_mod.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0.01) == (None, "nonzero")
    assert len(calls) == 6


MANUAL = "office revoke D1, then office resume; the reply is at /runs/r1/dispatches/D1/reply.txt"


@pytest.mark.parametrize("reply, convergence, expected", [
    ({"id": "D1", "task_id": "T1", "kind": "code_review", "scope": None},
     True, "office revoke D1, then office review T1 to relaunch the review on the same revision"),
    ({"id": "D1", "task_id": None, "kind": "plan_review", "scope": "plan"},
     True, "office revoke D1, then office review plan to re-run the plan review"),
    ({"id": "D1", "task_id": None, "kind": "convergence_review", "scope": "L-T1"},
     True, "office revoke L-T1:convergence, then office review L-T1:convergence to re-run the review on the same revision"),
    ({"id": "D1", "task_id": None, "kind": "visual", "scope": "L-T1"},
     True, "office revoke L-T1:visual, then office review L-T1:visual to re-run the review on the same revision"),
    # No same-revision re-run for a v3.1 plan review or a task visual review: the manual step and the reply file.
    ({"id": "D1", "task_id": None, "kind": "plan_review", "scope": "plan"}, False, MANUAL),
    ({"id": "D1", "task_id": "T1", "kind": "visual", "scope": None}, True, MANUAL),
    ({"id": "D1", "task_id": "T1", "kind": "visual", "scope": None}, False, MANUAL),
    # A code review with no task has nothing to relaunch.
    ({"id": "D1", "task_id": None, "kind": "code_review", "scope": None}, True, None),
])
def test_past_the_bound_each_reviewer_kind_names_its_next_step_and_never_an_invented_command(reply, convergence, expected):
    reply = {**reply, "path": "/runs/r1/dispatches/D1/reply.txt"}
    assert guide._stuck_reply_next(reply, convergence=convergence) == expected


@pytest.mark.approved
def test_status_and_wait_say_a_written_reply_settles_then_past_the_bound_name_the_relaunch(env, monkeypatch):
    _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, task_id, subject, kind, input_key, status, created_at) "
                "VALUES('G918', ?, 'T1', 'task', 'code_review', 'task:T1:r1', 'running', ?)",
                (run["id"], d["started_at"]))
    con.execute("INSERT INTO dispatches(id, run_id, task_id, role, started_at, kind, status, launcher, pane_id, pid, gate_id) "
                "VALUES('D918', ?, 'T1', 'code_reviewer', ?, 'reviewer', 'running', 'herdr', 'w1:p1', ?, 'G918')",
                (run["id"], d["started_at"], os.getpid()))  # a live supervisor: nothing settles it but the watch
    con.commit()
    reply = paths.run_dir(run["id"]) / "dispatches" / "D918" / "reply.txt"
    reply.parent.mkdir(parents=True, exist_ok=True)
    reply.write_text("VERDICT: APPROVED")
    settling = "D918 reply is written; Office ingests it when it settles, no action needed"

    res = guide.status(con, run)
    assert settling in res.lines and not res.next.startswith("office revoke"), res.lines
    assert settling in guide.wait(con, run, timeout=0, poll=0).lines

    os.utime(reply, (1, 1))  # written long ago and still not ingested
    res = guide.status(con, run)
    assert res.next == ("office revoke D918, then office review T1 to relaunch the review on the same revision"), res.next
    assert not any("reply is written" in ln for ln in res.lines), res.lines


@pytest.mark.approved
@pytest.mark.parametrize("gate, role, subject, task_id, scope, expected", [
    ("plan_review", "plan_reviewer", "plan", None, None,
     "office revoke D918, then office review plan to re-run the plan review"),
    ("visual", "visual_reviewer", "lane", None, "L-T1",
     "office revoke L-T1:visual, then office review L-T1:visual to re-run the review on the same revision"),
])
def test_past_the_bound_a_plan_or_lane_reply_gets_its_relaunch_next(env, monkeypatch, gate, role, subject, task_id,
                                                                    scope, expected):
    _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, task_id, subject, kind, scope, input_key, status, created_at) "
                "VALUES('G918', ?, ?, ?, ?, ?, 'k918', 'running', ?)", (run["id"], task_id, subject, gate, scope, d["started_at"]))
    con.execute("INSERT INTO dispatches(id, run_id, task_id, role, started_at, kind, status, launcher, pane_id, pid, gate_id) "
                "VALUES('D918', ?, ?, ?, ?, 'reviewer', 'running', 'herdr', 'w1:p1', ?, 'G918')",
                (run["id"], task_id, role, d["started_at"], os.getpid()))  # a live supervisor: nothing settles it but the watch
    con.commit()
    reply = paths.run_dir(run["id"]) / "dispatches" / "D918" / "reply.txt"
    reply.parent.mkdir(parents=True, exist_ok=True)
    reply.write_text("VERDICT: APPROVED\nFINDING F1 | high | partial")
    settling = "D918 reply is written; Office ingests it when it settles, no action needed"
    assert settling in guide.status(con, run).lines

    os.utime(reply, (1, 1))
    res = guide.status(con, run)
    assert res.next == expected, res.next
    assert not any("reply is written" in ln for ln in res.lines), res.lines


@pytest.mark.approved
def test_past_the_bound_a_task_visual_review_names_the_manual_step_and_the_reply_file(env, monkeypatch):
    _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, task_id, subject, kind, input_key, status, created_at) "
                "VALUES('G918', ?, 'T1', 'task', 'visual', 'task:T1:r1', 'running', ?)", (run["id"], d["started_at"]))
    con.execute("INSERT INTO dispatches(id, run_id, task_id, role, started_at, kind, status, launcher, pane_id, pid, gate_id) "
                "VALUES('D918', ?, 'T1', 'visual_reviewer', ?, 'reviewer', 'running', 'herdr', 'w1:p1', ?, 'G918')",
                (run["id"], d["started_at"], os.getpid()))
    con.commit()
    reply = paths.run_dir(run["id"]) / "dispatches" / "D918" / "reply.txt"
    reply.parent.mkdir(parents=True, exist_ok=True)
    reply.write_text("VERDICT: APPROVED")
    os.utime(reply, (1, 1))
    res = guide.status(con, run)
    assert res.next == f"office revoke D918, then office resume; the reply is at {reply}", res.next


@pytest.mark.parametrize("value, expected", [("nan", 0.0), ("abc", 3.0), ("-2", 0.0)])
def test_pane_group_wait_keeps_its_reading_of_bad_values(monkeypatch, value, expected):
    monkeypatch.setenv("OFFICE_PANE_GROUP_WAIT", value)
    assert dispatch_mod._pane_group_wait() == expected
