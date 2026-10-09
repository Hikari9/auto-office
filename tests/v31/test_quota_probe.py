"""Quota probe failure causes, cache precedence, run events, and preview snapshots."""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from office import candidates, db, plan_view, state


@pytest.fixture(autouse=True)
def isolated_quota_state(tmp_path, monkeypatch):
    candidates._QUOTA_CACHE.clear()
    monkeypatch.setattr(candidates.paths, "state_home", lambda: tmp_path)
    monkeypatch.delenv("OFFICE_QUOTA_FIXTURE", raising=False)
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "on")


def _adapter(command=None, harness="codex"):
    return {"id": harness, "quota_probe": {"command": command} if command is not None else {}}


@pytest.mark.parametrize(("adapter", "env", "proc", "expected"), [
    (_adapter(), {}, None, "no command configured"),
    (_adapter(["probe"]), {"OFFICE_QUOTA_PROBE": "off"}, None, "probe disabled"),
    (_adapter(["probe"]), {"OFFICE_QUOTA_FIXTURE": '{"codex": null}'}, None, "fixture returned null"),
    (_adapter(["probe"]), {}, SimpleNamespace(returncode=7, stdout="secret probe output"), "command exited with code 7"),
    (_adapter(["probe"]), {}, subprocess.TimeoutExpired(["probe"], 20), "command timed out after 20 seconds"),
    (_adapter(["probe"]), {}, SimpleNamespace(returncode=0, stdout="secret probe output"), "command returned invalid JSON"),
    (_adapter(["probe"]), {}, SimpleNamespace(returncode=0, stdout='{"other": 20}'),
     "command response missing tightest_remaining_percent"),
])
def test_unknown_probe_paths_have_safe_causes(adapter, env, proc, expected, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if proc is not None:
        def fake_run(*args, **kwargs):
            if isinstance(proc, BaseException):
                raise proc
            return proc
        monkeypatch.setattr(candidates.subprocess, "run", fake_run)

    result = candidates.probe_quota(adapter)

    assert result["status"] == "unknown"
    assert result["cause"] == expected
    assert "secret probe output" not in result["cause"]


def test_process_local_unknown_does_not_shadow_fresh_shared_ok(tmp_path):
    candidates._QUOTA_CACHE["codex"] = (time.time(), {
        "status": "unknown", "tightest_remaining_percent": None, "cause": "old failure"})
    shared = {"status": "ok", "tightest_remaining_percent": 37.0}
    candidates._shared_cache_put("codex", shared)

    assert candidates.probe_quota(_adapter(["must-not-run"])) == shared


def test_route_role_emits_one_unknown_event_per_harness_only_when_probing(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "runs.db")
    con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R1','Change','open','{}')")
    candidate = {"adapter_id": "codex", "quota": {"status": "unknown", "cause": "probe disabled"}}
    monkeypatch.setattr(candidates, "build_candidates", lambda *a, **kw: ([candidate, candidate], []))
    monkeypatch.setattr("office.benchmarks.apply", lambda run, cands: {})
    monkeypatch.setattr("office.routing.route", lambda request: {"status": "no_candidate"})
    run = {"id": "R1"}
    try:
        candidates.route_role(con, {}, run, "code_reviewer", probe=False)
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

        candidates.route_role(con, {}, run, "code_reviewer", probe=True)
        rows = con.execute("SELECT kind, summary, payload_json FROM events").fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "quota-probe-unknown"
        assert "codex" in rows[0]["summary"] and "probe disabled" in rows[0]["summary"]
        assert json.loads(rows[0]["payload_json"]) == {"harness": "codex", "cause": "probe disabled"}
    finally:
        con.close()


def test_plan_preview_shares_one_quota_snapshot_across_tasks_and_roles(monkeypatch):
    con = object()
    monkeypatch.setattr(state, "pinned_config", lambda run: {})
    monkeypatch.setattr(state, "current_requirements", lambda con, run_id: {"frozen": {"end_state": "ask"}})
    monkeypatch.setattr(plan_view, "_dispatched_routes", lambda con, run_id, tasks: {})
    monkeypatch.setattr("office.prs.settings", lambda con, run: {})
    monkeypatch.setattr(candidates.adapters, "load_all", lambda: {"codex": {"id": "codex"}})
    monkeypatch.setattr(candidates, "build_candidates", lambda *a, **kw: ([{"adapter_id": "codex"}], []))
    calls = []

    def fake_probe(adapter):
        calls.append(adapter["id"])
        return {"status": "ok", "tightest_remaining_percent": 40 - 10 * len(calls)}

    monkeypatch.setattr(candidates, "probe_quota", fake_probe)
    routed = []

    def fake_route_role(con, config, run, role, **kwargs):
        routed.append((role, kwargs["quota_snapshot"]))
        return {"status": "no_candidate", "rejected": []}

    monkeypatch.setattr(candidates, "route_role", fake_route_role)
    tasks = [{"id": f"T{i}", "title": f"Task {i}", "depends": []} for i in range(1, 4)]

    plan_view.preview(con, {"id": "R1", "gates": {"code_review": True}}, tasks)

    assert calls == ["codex"]
    assert len(routed) == 6
    assert all(snapshot == {"codex": {"status": "ok", "tightest_remaining_percent": 30}}
               for _, snapshot in routed)


def _timeout_run(calls):
    def fake_run(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired(["probe"], 20)
    return fake_run


def test_timeout_on_quiet_host_is_unknown_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(candidates.subprocess, "run", _timeout_run(calls))
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    result = candidates.probe_quota(_adapter(["probe"]))
    assert result["status"] == "unknown" and len(calls) == 1


def test_timeout_under_load_retries_once_then_unavailable(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(candidates.subprocess, "run", _timeout_run(calls))
    monkeypatch.setattr(candidates.time, "sleep", sleeps.append)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: "host load 40 exceeds 16 (8 CPUs)")
    result = candidates.probe_quota(_adapter(["probe"]))
    assert len(calls) == 2 and sleeps == [candidates.PROBE_RETRY_BACKOFF_S]
    assert result["status"] == "unavailable" and result["tightest_remaining_percent"] is None
    assert "host load 40" in result["cause"]


def test_retry_after_loaded_timeout_can_succeed(monkeypatch):
    outcomes = [subprocess.TimeoutExpired(["probe"], 20),
                SimpleNamespace(returncode=0, stdout='{"tightest_remaining_percent": 55}')]

    def fake_run(*a, **k):
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out
    monkeypatch.setattr(candidates.subprocess, "run", fake_run)
    monkeypatch.setattr(candidates.time, "sleep", lambda s: None)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: "loaded")
    assert candidates.probe_quota(_adapter(["probe"])) == {"status": "ok", "tightest_remaining_percent": 55.0}


def test_successful_probe_is_cached_across_processes_and_failure_never_becomes_ok(monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout='{"tightest_remaining_percent": 80}')
    monkeypatch.setattr(candidates.subprocess, "run", fake_run)
    first = candidates.probe_quota(_adapter(["probe"]))
    candidates._QUOTA_CACHE.clear()  # a new office process
    assert candidates.probe_quota(_adapter(["probe"])) == first and len(calls) == 1

    # A failed probe is remembered briefly, as a failure; the next process does not re-run it.
    candidates._QUOTA_CACHE.clear()
    candidates._shared_cache_path().unlink()
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    monkeypatch.setattr(candidates.subprocess, "run", _timeout_run(calls))
    bad = candidates.probe_quota(_adapter(["probe"]))
    candidates._QUOTA_CACHE.clear()
    n = len(calls)
    again = candidates.probe_quota(_adapter(["probe"]))
    assert bad["status"] == again["status"] == "unknown" and len(calls) == n


def test_cache_ttl_env_and_expiry(monkeypatch):
    candidates._shared_cache_put("codex", {"status": "ok", "tightest_remaining_percent": 9.0})
    assert candidates._shared_cache_get("codex") is not None
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    assert candidates._shared_cache_get("codex") is None


def test_shared_cache_put_keeps_other_harnesses():
    candidates._shared_cache_put("codex", {"status": "ok", "tightest_remaining_percent": 1.0})
    candidates._shared_cache_put("claude", {"status": "ok", "tightest_remaining_percent": 2.0})
    assert candidates._shared_cache_get("codex") and candidates._shared_cache_get("claude")


def test_route_payload_records_unknown_quota_and_status_shows_it():
    from office import dispatch
    cand = {"adapter_id": "claude", "harness": "claude", "quota": {"status": "unknown", "cause": "command timed out after 20 seconds"}}
    payload = dispatch._route_payload({"candidate": cand})
    assert payload["quota_unknown"] == {"harness": "claude", "status": "unknown",
                                        "cause": "command timed out after 20 seconds"}
    ok = {"adapter_id": "claude", "quota": {"status": "ok", "tightest_remaining_percent": 50.0}}
    assert "quota_unknown" not in dispatch._route_payload({"candidate": ok})

    from office import guide
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE dispatches(id TEXT, route_json TEXT)")
    con.execute("INSERT INTO dispatches VALUES('D1', ?)", (json.dumps(payload),))
    note = guide._quota_unknown_note(con, {"id": "T1", "current_dispatch_id": "D1"})
    assert note == "T1 routed on unknown quota: claude: command timed out after 20 seconds"
    assert guide._quota_unknown_note(con, {"id": "T2", "current_dispatch_id": None}) is None
