"""#860: quota stalls are provider availability, never hidden policy denies."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from office import dispatch, routing
from test_wait import _as_herdr, _go, _herdr, _wait


def test_only_known_opencode_free_routes_share_a_quota_pool():
    a = routing.shared_quota_pool_key("opencode", "glm-5.3-flash-free")
    b = routing.shared_quota_pool_key("opencode", "deepseek-v4.1-flash-free")
    assert a and a == b
    assert a in routing._demotion_keys({"harness": "opencode", "invocation_model_id": "glm-5.3-flash-free",
                                       "effort": "max"})
    assert a in routing._demotion_keys({"harness": "opencode", "invocation_model_id": "deepseek-v4.1-flash-free",
                                       "effort": "low"})
    assert routing.shared_quota_pool_key("opencode", "paid-model") is None
    assert routing.shared_quota_pool_key("agy", "glm-5.3-flash-free") is None
    assert routing.shared_quota_pool_key("codex", "gpt-6.1-sol") is None


def test_real_usage_limit_screen_records_one_cooldown_episode_and_audit_event(env):
    _go(env)
    session_env = _herdr(env, status="idle", pane="Free usage limit reached\n")
    d = _as_herdr(env)
    con = env.con()
    con.execute("UPDATE dispatches SET harness='opencode', adapter_id='opencode', "
                "model='glm-5.3-flash-free', invocation_model_id='glm-5.3-flash-free', effort='max' "
                "WHERE id=?", (d["id"],))
    con.commit()
    code, out = _wait(env, session_env, timeout="3")
    assert code == 3 and "usage_limit" in out, (code, out)
    route = routing.demotion_key("opencode", "glm-5.3-flash-free", "max")
    pool = routing.shared_quota_pool_key("opencode", "glm-5.3-flash-free")
    rows = {row["route"]: row["expires_at"] for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert route in rows and pool in rows and rows[route] == rows[pool]
    assert datetime.fromisoformat(rows[pool]) > datetime.now(timezone.utc)
    [event] = con.execute("SELECT payload_json FROM events WHERE kind='route.failed' AND dispatch_id=?",
                          (d["id"],)).fetchall()
    payload = json.loads(event[0])
    assert payload["reason"] == "usage_limit" and payload["quota_pool"] == pool
    # Repeated status reads cannot mint another failure or perpetually extend TTL.
    env.office("status", env=session_env)
    again = {row["route"]: row["expires_at"] for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert again == rows
    assert con.execute("SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?",
                       (d["id"],)).fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM events WHERE kind='route.policy_changed'").fetchone()


def test_dispatch_reroute_a_live_worker_must_refuse_without_false_success(env):
    _go(env)
    con = env.con()
    count = con.execute("SELECT COUNT(*) FROM events WHERE kind='dispatch'").fetchone()[0]
    code, out = env.office("dispatch", "T1", "--reroute")
    assert code == 4 and "worker-live" in out and "office revoke T1" in out, (code, out)
    assert "dispatched T1" not in out
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='dispatch'").fetchone()[0] == count
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 1


def test_expired_shared_quota_pool_key_releases_the_entire_provider(env):
    _go(env)
    con = env.con()
    key = routing.shared_quota_pool_key("opencode", "glm-5.3-flash-free")
    con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                (key, "2999-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    con.commit()
    assert key in routing._demotion_keys({"harness": "opencode", "model_id": "deepseek-v4.1-flash-free",
                                          "effort": "high"})
    assert key not in routing._demotion_keys({"harness": "agy", "model_id": "gemini-3.8-flash",
                                              "effort": "medium"})
    con.execute("UPDATE route_cooldowns SET expires_at='2000-01-01T00:00:00+00:00' WHERE route=?", (key,))
    con.commit()
    now = datetime.now(timezone.utc).isoformat()
    assert not con.execute("SELECT 1 FROM route_cooldowns WHERE route=? AND expires_at>?",
                           (key, now)).fetchone()
