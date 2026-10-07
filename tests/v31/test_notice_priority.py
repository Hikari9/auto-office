"""Orchestrator news: failures and blockers lead, and one combined quota notice per routing call."""
from __future__ import annotations

import json

import pytest

from office import candidates, db, guide, state
from office.result import Result


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "on")
    c = db.connect(tmp_path / "runs.db")
    c.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R1','Change','open','{}')")
    yield c
    c.close()


RUN = {"id": "R1"}


def _emit(con, kind, summary):
    with db.transaction(con):
        return state.emit(con, RUN, kind, summary)


def _news(con):
    res = Result()
    guide.piggyback(con, RUN, res)
    return [n.removeprefix("· ") for n in res.notices]


def test_failures_and_blockers_lead_and_the_cap_stays_four(con):
    for i in range(3):
        _emit(con, "quota-probe-unknown", f"quota {i}")
    _emit(con, "dispatch", "dispatched T1")
    _emit(con, "setup.failed", "worktree setup failed")
    _emit(con, "task.blocked", "T1 blocked")
    assert _news(con) == ["worktree setup failed", "T1 blocked", "quota 0", "quota 1"]


def test_informational_notices_keep_their_order_when_nothing_is_urgent(con):
    for i in range(6):
        _emit(con, "dispatch", f"note {i}")
    assert _news(con) == ["note 0", "note 1", "note 2", "note 3"]
    assert _news(con) == ["note 4", "note 5"]
    assert _news(con) == []


def test_an_older_informational_notice_is_not_lost_behind_newer_urgent_ones(con):
    _emit(con, "dispatch", "dispatched T1")
    for i in range(6):
        _emit(con, "integration.failed", f"failure {i}")
    assert _news(con) == [f"failure {i}" for i in range(4)]
    assert _news(con) == ["failure 4", "failure 5", "dispatched T1"]
    assert _news(con) == []


def test_an_event_already_shown_is_not_shown_again_while_an_older_one_waits(con):
    _emit(con, "dispatch", "old 0")
    _emit(con, "dispatch", "old 1")
    for i in range(4):
        _emit(con, "task.blocked", f"blocked {i}")
    assert _news(con) == [f"blocked {i}" for i in range(4)]
    _emit(con, "task.blocked", "blocked 4")
    assert _news(con) == ["blocked 4", "old 0", "old 1"]
    assert _news(con) == []
    assert con.execute("SELECT COUNT(*) FROM cursors WHERE consumer LIKE 'orchestrator:seen:%'").fetchone()[0] == 0


def test_a_pause_leads_and_an_unblock_is_only_news(con):
    for i in range(4):
        _emit(con, "dispatch", f"note {i}")
    _emit(con, "task.unblocked", "T1 unblocked")
    _emit(con, "task.paused", "T1 paused")
    assert _news(con) == ["T1 paused", "note 0", "note 1", "note 2"]
    assert _news(con) == ["note 3", "T1 unblocked"]


def test_a_notice_that_was_not_shown_stays_unread_when_it_is_newer(con):
    _emit(con, "setup.failed", "failed")
    for i in range(5):
        _emit(con, "dispatch", f"note {i}")
    assert _news(con) == ["failed", "note 0", "note 1", "note 2"]
    assert _news(con) == ["note 3", "note 4"]


def _route(con, monkeypatch, harnesses, **kwargs):
    cands = [{"adapter_id": h, "quota": {"status": "unknown", "cause": f"{h} failed"}} for h in harnesses]
    monkeypatch.setattr(candidates, "build_candidates", lambda *a, **kw: (cands, []))
    monkeypatch.setattr("office.benchmarks.apply", lambda run, c: {})
    monkeypatch.setattr("office.routing.route", lambda request: {"status": "no_candidate"})
    candidates.route_role(con, {}, RUN, "code_reviewer", probe=True, **kwargs)
    return [dict(r) for r in con.execute("SELECT kind, summary, payload_json FROM events ORDER BY seq")]


def test_one_combined_quota_event_per_routing_call(con, monkeypatch):
    rows = _route(con, monkeypatch, ["claude", "codex", "agy", "codex"])
    assert [r["kind"] for r in rows] == ["quota-probe-unknown"]
    assert all(h in rows[0]["summary"] for h in ("claude failed", "codex failed", "agy failed"))
    assert json.loads(rows[0]["payload_json"]) == {"harnesses": {"claude": "claude failed", "codex": "codex failed",
                                                                 "agy": "agy failed"}}


def test_a_harness_already_reported_is_not_reported_again(con, monkeypatch):
    seen = {"claude"}
    rows = _route(con, monkeypatch, ["claude", "codex"], quota_event_seen=seen)
    assert len(rows) == 1 and "codex" in rows[0]["summary"] and "claude" not in rows[0]["summary"]
    assert seen == {"claude", "codex"}
    assert len(_route(con, monkeypatch, ["claude", "codex"], quota_event_seen=seen)) == 1  # nothing new


def test_a_probe_switched_off_emits_nothing(con, monkeypatch):
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
    assert _route(con, monkeypatch, ["claude", "codex", "agy"]) == []
