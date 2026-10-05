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
