"""#860: quota stalls are provider availability, never hidden policy denies."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from office import dispatch, route_policy, routing
from test_adaptive_dispatch import PLANNED, _approve as _approve_slate, _quota
from test_wait import MANILA_LIMIT, _as_herdr, _go, _herdr, _limit_row, _store_episode, _wait


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
    session_env = _herdr(env, status="idle", pane="sk-test-secret\nFree usage limit reached\n")
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
    remaining = datetime.fromisoformat(rows[pool]) - datetime.now(timezone.utc)
    ttl = timedelta(minutes=route_policy.ROUTE_FAILURE_TTL_MINUTES)
    assert ttl - timedelta(seconds=3) < remaining <= ttl, remaining
    [event] = con.execute("SELECT payload_json FROM events WHERE kind='route.failed' AND dispatch_id=?",
                          (d["id"],)).fetchall()
    payload = json.loads(event[0])
    assert payload["reason"] == "usage_limit" and payload["quota_pool"] == pool
    assert "sk-test-secret" not in json.dumps(payload)
    # Repeated status reads cannot mint another failure or perpetually extend TTL.
    env.office("status", env=session_env)
    again = {row["route"]: row["expires_at"] for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert again == rows
    assert con.execute("SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?",
                       (d["id"],)).fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM events WHERE kind='route.policy_changed'").fetchone()


def test_a_later_limit_episode_after_its_reset_records_a_fresh_cooldown_and_event(env):
    _go(env)
    e = _herdr(env, status="idle", pane=MANILA_LIMIT)
    d = _as_herdr(env)
    con = env.con()
    failed = "SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?"
    cooldowns = "SELECT route, updated_at FROM route_cooldowns"
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out, out
    assert con.execute(failed, (d["id"],)).fetchone()[0] == 1
    first = dict(con.execute(cooldowns).fetchall())
    assert first, first
    # Same screen and label, stored reset passed, a different reset parsed: a new episode.
    _store_episode(env, d, **{**_limit_row(env, d), "resets_at": "2026-01-01T13:30:00+00:00"})
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "now past" in out, out
    assert con.execute(failed, (d["id"],)).fetchone()[0] == 2
    stored = _limit_row(env, d)["resets_at"]
    assert stored > "2026-01-02", stored
    second = dict(con.execute(cooldowns).fetchall())
    assert second.keys() == first.keys() and all(second[r] > first[r] for r in first), (first, second)
    newest = con.execute("SELECT payload_json FROM events WHERE kind='route.failed' AND dispatch_id=? "
                         "ORDER BY seq DESC LIMIT 1", (d["id"],)).fetchone()[0]
    assert json.loads(newest)["demoted_until"] == stored, newest
    # Repeated polls of the new episode add no event, move no reset, and leave the cooldowns alone.
    _wait(env, e, timeout="3")
    assert con.execute(failed, (d["id"],)).fetchone()[0] == 2
    assert _limit_row(env, d)["resets_at"] == stored
    assert dict(con.execute(cooldowns).fetchall()) == second


def test_a_resolved_reset_refreshes_the_current_cooldown_without_a_second_event(env, monkeypatch):
    _go(env)
    e = _herdr(env, status="idle", pane="limit screen")
    d = _as_herdr(env)
    con = env.con()
    con.execute("UPDATE dispatches SET harness='opencode', adapter_id='opencode', "
                "model='glm-5.3-flash-free', invocation_model_id='glm-5.3-flash-free', effort='max' "
                "WHERE id=?", (d["id"],))
    con.commit()
    limit = {"resets_at": None, "label": "same reset", "tz": None, "local": None,
             "fingerprint": "same-screen"}
    monkeypatch.setattr(dispatch, "_usage_limit", lambda _text: limit)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out, out
    before = {row["route"]: row["expires_at"]
              for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert before and con.execute("SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?",
                                 (d["id"],)).fetchone()[0] == 1

    reset_at = datetime.now(timezone.utc) + timedelta(hours=1)
    limit = {**limit, "resets_at": reset_at}
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out, out
    after = {row["route"]: row["expires_at"]
             for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert after.keys() == before.keys() and set(after.values()) == {reset_at.isoformat()}, (before, after)
    assert con.execute("SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?",
                       (d["id"],)).fetchone()[0] == 1

    _wait(env, e, timeout="3")
    repeated = {row["route"]: row["expires_at"]
                for row in con.execute("SELECT route, expires_at FROM route_cooldowns")}
    assert repeated == after
    assert con.execute("SELECT count(*) FROM events WHERE kind='route.failed' AND dispatch_id=?",
                       (d["id"],)).fetchone()[0] == 1


def test_dispatch_reroute_a_live_worker_must_refuse_without_false_success(env):
    _go(env)
    con = env.con()
    count = con.execute("SELECT COUNT(*) FROM events WHERE kind='dispatch'").fetchone()[0]
    code, out = env.office("dispatch", "T1", "--reroute")
    assert code == 4 and "worker-live" in out and "office revoke T1" in out, (code, out)
    assert "dispatched T1" not in out
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='dispatch'").fetchone()[0] == count
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 1


def test_first_orchestrator_rerun_after_usage_limit_records_route_fallback(env):
    _approve_slate(env, plan=PLANNED)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    con = env.con()
    parent = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, stall_kind='usage_limit' WHERE id=?",
                (parent["id"],))
    con.commit()

    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env, claude=1))
    assert code == 0, out
    latest = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())
    assert latest["triple"] != parent["triple"], (parent["triple"], latest["triple"], out)
    rows = con.execute("SELECT payload_json FROM events WHERE kind='route.fallback' AND dispatch_id=?",
                       (latest["id"],)).fetchall()
    assert len(rows) == 1
    event = json.loads(rows[0][0])
    assert event["before"] == parent["triple"] and event["after"] == latest["triple"], event
    assert event["source"] == "usage_limit" and event["actor"] == "orchestrator", event


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


def test_actual_router_skips_same_pool_fallback_then_recovers_after_ttl():
    """Exercise the real router, not only the pool-key helper."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE route_cooldowns(route TEXT PRIMARY KEY, expires_at TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE dispatches(id TEXT, money_estimate REAL, money_actual REAL, triple TEXT)")
    key = routing.shared_quota_pool_key("opencode", "glm-5.3-flash-free")

    def candidate(harness, model, effort):
        return {"harness": harness, "harness_version": "1.0.0", "model_id": model,
                "invocation_model_id": model, "effort": effort, "capabilities": [],
                "route_status": "available", "quota": {"status": "ok", "tightest_remaining_percent": 90},
                "benchmark_indexes": {}, "cost_per_million_input_tokens": 0,
                "cost_per_million_output_tokens": 0}

    def choose(*, independent=True):
        rows = [candidate("opencode", "glm-5.3-flash-free", "max"),
                candidate("opencode", "deepseek-v4.1-flash-free", "max")]
        if independent:
            rows.append(candidate("agy", "gemini-3.8-flash", "medium"))
        return routing.route({
            "role": "code_reviewer", "candidates": rows, "allow_free_models": True,
            "preferred_seed": [{"model_id": "glm-5.3-flash-free"},
                               {"model_id": "deepseek-v4.1-flash-free"},
                               {"model_id": "gemini-3.8-flash"}],
            "policy": {"required_capabilities": [], "cost_policy": "balanced"}
        }, connection=con)

    assert choose()["selected"] == "opencode@1/glm-5.3-flash-free@max"
    con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                (key, "2999-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    during = choose()
    assert during["selected"] == "agy@1/gemini-3.8-flash@medium", during
    same_pool = [r for r in during["rejected"] if r.get("category") == "route-demoted"]
    assert len(same_pool) == 2 and all(r["candidate"].startswith("opencode@") for r in same_pool)
    exhausted = choose(independent=False)
    assert exhausted["status"] == "no_qualifying_candidate" and exhausted["selected"] is None
    con.execute("UPDATE route_cooldowns SET expires_at='2000-01-01T00:00:00+00:00' WHERE route=?", (key,))
    after = choose()
    assert after["selected"] == "opencode@1/glm-5.3-flash-free@max", after
