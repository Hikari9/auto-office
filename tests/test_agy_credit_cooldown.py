"""An agy AI-credits wall is account-wide: it cools every agy route, and only agy."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office import db as office_db, dispatch, gates, routing

CREDITS_LOG = ("RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota).\n"
               "Your AI credits balance is too low to continue.\n")
CODEX_LOG = "stream error: 429 Too Many Requests\n"
NOW = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.db"
    real_connect = office_db.connect
    monkeypatch.setattr(dispatch.db, "connect", lambda: real_connect(db_path))
    c = real_connect(db_path)
    c.execute("INSERT INTO runs(id, phase, status, policy_json) VALUES ('r', 'executing', 'running', ?)",
              (json.dumps({"quota": {"cooldown_minutes": 15}}),))
    c.commit()
    yield c
    c.close()


def _dispatch(con, tmp_path, did, harness, log_text):
    log = tmp_path / f"{did}.log"
    log.write_text(log_text, encoding="utf-8")
    con.execute("INSERT INTO dispatches(id, run_id, role, kind, status, triple, harness, invocation_model_id, "
                "effort, log_path) VALUES (?, 'r', 'observer', 'observer', 'running', ?, ?, 'm1', 'high', ?)",
                (did, f"{harness}@1/m1@high", harness, str(log)))
    con.commit()


def _routes(con):
    return {r[0]: r[1] for r in con.execute("SELECT route, expires_at FROM route_cooldowns")}


def _finish(did, classification="nonzero", code=1):
    dispatch._finish(did, code, None, classification, 1.0)


def test_signatures_match_credit_lines():
    for line in CREDITS_LOG.splitlines():
        assert gates._quota_signature(line)
    assert gates._quota_signature("Your AI credits balance is too low to continue.")
    quota = next(p for r, p in dispatch._ROUTE_FAILURE_SIGNATURES if r == "quota")
    import re
    assert re.search(quota, "Your AI credits balance is too low to continue.", re.I)
    assert re.search(quota, "RESOURCE_EXHAUSTED", re.I)


def test_agy_credits_wall_writes_harness_key_with_model_expiry(con, tmp_path):
    _dispatch(con, tmp_path, "d1", "agy", CREDITS_LOG)
    run = dict(con.execute("SELECT * FROM runs WHERE id='r'").fetchone())
    d = dict(con.execute("SELECT * FROM dispatches WHERE id='d1'").fetchone())
    d["terminal_classification"] = "nonzero"
    until = dispatch._record_route_cooldown(con, run, d, now=NOW)
    routes = _routes(con)
    expected = (NOW + timedelta(minutes=15)).isoformat()
    assert until == NOW + timedelta(minutes=15)
    assert routes[routing.harness_demotion_key("agy")] == expected
    assert routes[routing.cooldown_key("agy", "m1")] == expected


def test_finish_records_harness_key_event_and_success_clears_it(con, tmp_path):
    _dispatch(con, tmp_path, "d1", "agy", CREDITS_LOG)
    _finish("d1")
    key = routing.harness_demotion_key("agy")
    assert key in _routes(con)
    ev = con.execute("SELECT summary FROM events WHERE kind='route.failed'").fetchall()
    assert any("AI credits exhausted" in r[0] for r in ev)
    _dispatch(con, tmp_path, "d2", "agy", "done\n")
    _finish("d2", "success", 0)
    assert key not in _routes(con)
    assert routing.cooldown_key("agy", "m1") not in _routes(con)


def test_codex_429_and_other_agy_walls_stay_per_model(con, tmp_path):
    _dispatch(con, tmp_path, "c1", "codex", CODEX_LOG)
    _finish("c1")
    _dispatch(con, tmp_path, "c2", "codex", CREDITS_LOG)  # same text on another harness
    _finish("c2")
    _dispatch(con, tmp_path, "a1", "agy", "RESOURCE_EXHAUSTED (code 429): Resource has been exhausted\n")
    _finish("a1")
    routes = _routes(con)
    assert routing.harness_demotion_key("codex") not in routes
    assert routing.harness_demotion_key("agy") not in routes
    assert routing.cooldown_key("codex", "m1") in routes
    assert routing.cooldown_key("agy", "m1") in routes


def test_credits_line_in_narration_above_tail_does_not_cool_agy(con, tmp_path):
    narration = "Your AI credits balance is too low to continue.\n" + "work\n" * 6 + "429 rate limit\n"
    _dispatch(con, tmp_path, "a1", "agy", narration)
    _finish("a1")
    assert routing.harness_demotion_key("agy") not in _routes(con)
