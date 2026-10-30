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


def test_only_a_code_review_with_a_task_gets_the_relaunch_next():
    assert guide._stuck_reply_next({"id": "D1", "task_id": "T1", "kind": "code_review"}).startswith("office revoke D1")
    assert guide._stuck_reply_next({"id": "D1", "task_id": "T1", "kind": "plan_review"}) is None
    assert guide._stuck_reply_next({"id": "D1", "task_id": None, "kind": "code_review"}) is None


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
