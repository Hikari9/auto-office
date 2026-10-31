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
    (_adapter(["probe"]), {}, subprocess.TimeoutExpired(["probe"], 20), "command timed out twice after 20 seconds"),
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
        if isinstance(proc, subprocess.TimeoutExpired):
            monkeypatch.setattr(candidates.time, "sleep", lambda seconds: None)

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


def test_timeout_on_quiet_host_retries_once_before_unknown(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(candidates.subprocess, "run", _timeout_run(calls))
    monkeypatch.setattr(candidates.time, "sleep", sleeps.append)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    result = candidates.probe_quota(_adapter(["probe"]))
    assert result["status"] == "unknown" and len(calls) == 2
    assert sleeps == [candidates.PROBE_RETRY_BACKOFF_S]
    assert result["failure_kind"] == "timeout"
    assert result["cause"] == "command timed out twice after 20 seconds"


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


def test_quiet_timeout_then_success_on_retry(monkeypatch):
    calls, sleeps = [], []
    answers = [subprocess.TimeoutExpired(["probe"], 20),
               SimpleNamespace(returncode=0, stdout='{"tightest_remaining_percent": 62}')]

    def fake_run(*args, **kwargs):
        calls.append(1)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(candidates.subprocess, "run", fake_run)
    monkeypatch.setattr(candidates.time, "sleep", sleeps.append)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    result = candidates.probe_quota(_adapter(["probe"]))
    assert result == {"status": "ok", "tightest_remaining_percent": 62.0}
    assert len(calls) == 2 and sleeps == [candidates.PROBE_RETRY_BACKOFF_S]


def test_last_success_survives_timeout_across_processes_then_expires(monkeypatch):
    now = [1000.0]
    probe_fails = [False]
    calls = []
    monkeypatch.setattr(candidates.time, "time", lambda: now[0])
    monkeypatch.setattr(candidates.time, "sleep", lambda _: None)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)

    def fake_run(*args, **kwargs):
        calls.append(1)
        if probe_fails[0]:
            raise subprocess.TimeoutExpired(["probe"], 20)
        return SimpleNamespace(returncode=0, stdout='{"tightest_remaining_percent": 42}')

    monkeypatch.setattr(candidates.subprocess, "run", fake_run)
    success = candidates.probe_quota(_adapter(["probe"], "claude"))
    assert success == {"status": "ok", "tightest_remaining_percent": 42.0}
    now[0] += 301  # The normal 5-minute cache expires; the last-good 10-minute window has not.
    probe_fails[0] = True
    candidates._QUOTA_CACHE.clear()  # Simulate another office process.
    fallback = candidates.probe_quota(_adapter(["probe"], "claude"))
    assert len(calls) == 3  # initial success, then two timed-out attempts
    assert fallback["status"] == "ok" and fallback["tightest_remaining_percent"] == 42.0
    assert fallback["source"] == "last-known"
    assert fallback["last_success_age_seconds"] == 301
    assert "timed out twice" in fallback["probe_failure"]

    from office import dispatch, guide
    import sqlite3
    route = dispatch._route_payload({"candidate": {"adapter_id": "claude", "quota": fallback}})
    assert route["quota_unknown"]["status"] == "last-known"
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE dispatches(id TEXT, route_json TEXT)")
    con.execute("INSERT INTO dispatches VALUES('D1', ?)", (json.dumps(route),))
    note = guide._quota_unknown_note(con, {"id": "T1", "current_dispatch_id": "D1"})
    assert "T1 routed on last-known quota: claude:" in note
    assert "42.0% from 301s ago" in note

    now[0] += 300  # A saved reading must not be treated as eternally available.
    candidates._QUOTA_CACHE.clear()
    expired = candidates.probe_quota(_adapter(["probe"], "claude"))
    assert expired["status"] == "unknown" and expired.get("source") is None
    assert len(calls) == 5  # two fresh attempts after fallback expired


def test_last_good_not_used_for_auth_errors_or_when_cache_disabled(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(candidates.time, "time", lambda: now[0])
    monkeypatch.setattr(candidates.time, "sleep", lambda _: None)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    candidates._shared_cache_put("claude", {"status": "ok", "tightest_remaining_percent": 37.0})
    now[0] += 301
    candidates._QUOTA_CACHE.clear()

    monkeypatch.setattr(candidates.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=2, stdout="private token"))
    failed_auth = candidates.probe_quota(_adapter(["probe"], "claude"))
    assert failed_auth["status"] == "unknown"
    assert failed_auth.get("source") is None
    assert failed_auth["cause"] == "command exited with code 2"

    now[0] += 61
    candidates._QUOTA_CACHE.clear()
    monkeypatch.setenv("OFFICE_QUOTA_LAST_GOOD_TTL", "0")
    monkeypatch.setattr(candidates.subprocess, "run", _timeout_run([]))
    denied_fallback = candidates.probe_quota(_adapter(["probe"], "claude"))
    assert denied_fallback["status"] == "unknown" and denied_fallback.get("source") is None


def test_last_good_fallback_preserves_agy_family_limits(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(candidates.time, "time", lambda: now[0])
    family = {"gemini": {"status": "ok", "tightest_remaining_percent": 0.0},
              "claude": {"status": "ok", "tightest_remaining_percent": 80.0}}
    candidates._shared_cache_put("agy", {
        "status": "ok", "tightest_remaining_percent": 0.0, "families": family})
    now[0] += 301
    candidates._shared_cache_put("agy", {
        "status": "unknown", "tightest_remaining_percent": None,
        "cause": "command timed out twice after 20 seconds", "failure_kind": "timeout"})
    fallback = candidates._shared_cache_get("agy")
    assert fallback["source"] == "last-known" and fallback["families"] == family
    claude = candidates.quota_for_candidate({"adapter_id": "agy", "model_id": "claude-sonnet-5-5"}, fallback)
    assert claude["source"] == "last-known" and claude["tightest_remaining_percent"] == 80.0
    gemini = candidates.quota_for_candidate({"adapter_id": "agy", "model_id": "gemini-3.8-flash"}, fallback)
    assert gemini["tightest_remaining_percent"] == 0.0


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
    for cause in ("no command configured", "probe disabled"):
        unprobed = {"adapter_id": "claude", "quota": {"status": "unknown", "cause": cause}}
        assert dispatch._route_payload({"candidate": unprobed})["quota_unknown"]["cause"] == cause
    not_sampled = {"adapter_id": "claude"}
    assert dispatch._route_payload({"candidate": not_sampled})["quota_unknown"]["cause"] == "quota not probed"
    unknown_family = {"adapter_id": "agy", "model_id": "claude-sonnet-5-5", "quota": {
        "status": "ok", "tightest_remaining_percent": 60.0,
        "families": {"claude": {"status": "unknown", "tightest_remaining_percent": None,
                                "cause": "family quota unavailable"}}}}
    assert dispatch._route_payload({"candidate": unknown_family})["quota_unknown"]["cause"] == "family quota unavailable"
    unknown_family["quota"].update(source="last-known", last_success_age_seconds=301,
                                    probe_failure="command timed out twice")
    unknown_notice = dispatch._route_payload({"candidate": unknown_family})["quota_unknown"]
    assert unknown_notice["status"] == "unknown" and unknown_notice["cause"] == "family quota unavailable"


_FAMILIES = {
    "gemini": {"status": "ok", "tightest_remaining_percent": 0, "reset_time": "g"},
    "claude": {"status": "ok", "tightest_remaining_percent": 80, "reset_time": "c"},
}


def _probe_stdout(monkeypatch, payload):
    candidates._QUOTA_CACHE.clear()
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    monkeypatch.setattr(candidates.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    return candidates.probe_quota(_adapter(["probe"], harness="agy"))


def test_probe_keeps_per_family_result(monkeypatch):
    result = _probe_stdout(monkeypatch, {"tightest_remaining_percent": 0,
                                         "families": {**_FAMILIES, "x": {"status": "unknown"}}})
    assert result["families"]["claude"] == {"status": "ok", "tightest_remaining_percent": 80.0, "reset_time": "c"}
    assert result["families"]["x"]["status"] == "unknown" and result["families"]["x"]["cause"]


def test_probe_without_families_stays_harness_wide(monkeypatch):
    assert _probe_stdout(monkeypatch, {"tightest_remaining_percent": 42}) == {
        "status": "ok", "tightest_remaining_percent": 42.0}


def _agy(model, quota):
    return {"adapter_id": "agy", "harness": "agy", "model_id": model, "quota": quota}


def test_quota_for_candidate_is_per_family_for_agy_only():
    quota = {"status": "ok", "tightest_remaining_percent": 50.0, "families": candidates._normalize_probe_result(
        {"tightest_remaining_percent": 50, "families": _FAMILIES}, None)["families"]}
    assert candidates.quota_for_candidate(_agy("gemini-3.8-flash", quota), quota)["tightest_remaining_percent"] == 0
    assert candidates.quota_for_candidate(_agy("claude-opus-5-5", quota), quota)["tightest_remaining_percent"] == 80
    assert candidates.quota_for_candidate(_agy("gpt-oss-120b", quota), quota)["tightest_remaining_percent"] == 80
    # a family the probe did not report, and a probe with no families, stay harness-wide
    assert candidates.quota_for_candidate(_agy("mystery-1", quota), quota) is quota
    plain = {"status": "ok", "tightest_remaining_percent": 42.0}
    assert candidates.quota_for_candidate(_agy("gemini-3.8-flash", plain), plain) is plain
    other = {"adapter_id": "codex", "harness": "codex", "model_id": "gemini-x", "quota": quota}
    assert candidates.quota_for_candidate(other, quota) is quota


def _routable(harness, model, quota):
    return {
        "harness": harness, "adapter_id": harness, "harness_version": "2.0", "model_id": model,
        "invocation_model_id": model, "effort": "high", "capabilities": ["builder"],
        "benchmark_indexes": {"Artificial Analysis Intelligence Index v4.3.2": 45},
        "price_fields": {"output_per_mtok": 2.0, "input_per_mtok": 0.5},
        "speed_fields": {"output_tok_per_s": 100.0, "ttft_ms": 1000},
        "cost": {"money_estimate": 2.0}, "quota": quota,
    }


def _route(cands):
    from office import routing
    return routing.route({
        "role": "worker", "playbook": "Change", "candidates": cands,
        "policy": {"cost_policy": "balanced", "required_capabilities": ["builder"], "quota_reserve_percent": 5},
        "evidence": {"routes": {}, "pooling": {"prior_strength": 8}},
        "routing_seed": "family-quota", "adaptive_config": {"exploration": {"rate": 0}},
    })


def _agy_family_quota():
    return {"status": "ok", "tightest_remaining_percent": 50.0, "families": candidates._normalize_probe_result(
        {"tightest_remaining_percent": 50, "families": _FAMILIES}, None)["families"]}


def test_zero_gemini_family_is_rejected_even_when_other_candidates_are_unknown():
    unknown = {"status": "unknown", "tightest_remaining_percent": None}
    cands = [_routable("agy", "gemini-3.8-flash", _agy_family_quota()), _routable("claude", "opus", unknown)]
    result = _route(cands)  # the agy candidate keeps the harness-wide quota: resolved in-router
    assert result["status"] == "selected"
    assert result["selected"].startswith("claude@")
    assert any(r["stage"] == 6 and "gemini-3.8-flash" in r["candidate"] for r in result["rejected"])


def test_zero_gemini_family_still_leaves_agy_claude_eligible():
    quota = _agy_family_quota()
    result = _route([_routable("agy", "gemini-3.8-flash", quota), _routable("agy", "claude-sonnet-5-5", quota)])
    assert result["selected"].startswith("agy@") and "claude-sonnet-5-5" in result["selected"]
    assert any(r["stage"] == 6 and "gemini-3.8-flash" in r["candidate"] for r in result["rejected"])
    rows = {r["route"]: r for r in result["routing"]["candidates"]}
    claude_row = next(r for k, r in rows.items() if "claude-sonnet-5-5" in k)
    assert claude_row["quota"]["state"] == "80% left"


def test_zero_claude_family_rejects_agy_claude_and_leaves_gemini_eligible():
    quota = _agy_family_quota()
    fam = quota["families"]
    fam["gemini"], fam["claude"] = fam["claude"], fam["gemini"]  # claude 0%, gemini 80%
    result = _route([_routable("agy", "gemini-3.8-flash", quota), _routable("agy", "claude-sonnet-5-5", quota)])
    assert result["selected"].startswith("agy@") and "gemini-3.8-flash" in result["selected"]
    assert any(r["stage"] == 6 and "claude-sonnet-5-5" in r["candidate"] for r in result["rejected"])
    rows = {r["route"]: r for r in result["routing"]["candidates"]}
    assert next(r for k, r in rows.items() if "gemini-3.8-flash" in k)["quota"]["state"] == "80% left"


def test_unknown_top_level_keeps_family_quota():
    result = candidates._normalize_probe_result(
        {"tightest_remaining_percent": None, "cause": "gemini family quota not reported",
         "families": {"gemini": {"status": "unknown", "tightest_remaining_percent": None},
                      "claude": {"status": "ok", "tightest_remaining_percent": 80}}}, lambda c: {"status": "unknown", "tightest_remaining_percent": None, "cause": c})
    assert result["status"] == "unknown" and result["cause"] == "gemini family quota not reported"
    assert result["families"]["claude"]["tightest_remaining_percent"] == 80.0
