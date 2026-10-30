"""#897: the full suite runs under verification.full_suite_timeout_seconds, `--pr`
keeps check_timeout_seconds, and a timed-out check names the setting to raise."""
from __future__ import annotations

import pytest

from office import db, gates

RUN = {"id": "r1"}
REV = {"id": "R1", "commit_sha": "0" * 40, "dispatch_id": "D1"}
GATE = {"id": "G1", "task_id": "T1"}
CFG = {"check_timeout_seconds": 1800, "full_suite_timeout_seconds": 7200}
FULL = ["scripts/validate.sh", "./scripts/validate.sh", "bash scripts/validate.sh", "scripts/validate.sh --full"]


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    conn = db.connect(tmp_path / "runs.db")
    conn.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, status, created_at) "
                 "VALUES('G1', 'r1', 'T1', 'T1', 'R1', 'checks', 'k', 'running', '2026-01-01T00:00:00Z')")
    return conn


def _run(con, tmp_path, monkeypatch, command, *, cfg=CFG, code=0, out="", loaded=None):
    """Run one check through _run_commands; return (outcome, timeouts each _run_check call received)."""
    started = []

    def fake_run_check(cmd, cwd, timeout, env):
        started.append(timeout)
        return code, out

    monkeypatch.setattr(gates, "_run_check", fake_run_check)
    monkeypatch.setattr(gates, "host_overloaded", lambda: loaded)
    outcome = gates._run_commands(con, RUN, [command], tmp_path, REV, GATE, cfg, check_tree=False)
    return outcome, started


@pytest.mark.parametrize("command", FULL)
def test_full_suite_check_gets_the_full_suite_timeout(con, tmp_path, monkeypatch, command):
    _, started = _run(con, tmp_path, monkeypatch, command)
    assert started == [7200]


def test_pr_gate_keeps_check_timeout(con, tmp_path, monkeypatch):
    _, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh --pr")
    assert started == [1800]


def test_full_suite_timeout_is_the_configured_setting(con, tmp_path, monkeypatch):
    _, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh",
                      cfg={"check_timeout_seconds": 1800, "full_suite_timeout_seconds": 10800})
    assert started == [10800]


def test_full_suite_never_gets_less_than_the_check_timeout(con, tmp_path, monkeypatch):
    cfg = {"check_timeout_seconds": 9000, "full_suite_timeout_seconds": 7200}
    _, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", cfg=cfg)
    assert started == [9000]
    assert gates.check_timeout(cfg, "scripts/validate.sh") == (9000, "check_timeout_seconds")


def test_check_timeout_defaults_when_unset():
    assert gates.check_timeout({}, "scripts/validate.sh") == (7200, "full_suite_timeout_seconds")
    assert gates.check_timeout({}, "pytest -q") == (1800, "check_timeout_seconds")


@pytest.mark.parametrize("command, limit, setting", [
    ("scripts/validate.sh", 7200, "full_suite_timeout_seconds"),
    ("scripts/validate.sh --pr", 1800, "check_timeout_seconds"),
])
def test_timeout_names_the_limit_and_the_setting_to_raise(con, tmp_path, monkeypatch, command, limit, setting):
    outcome, started = _run(con, tmp_path, monkeypatch, command, code=124, out="partial output\n")
    assert started == [limit]
    assert outcome["verdict"] == "CHANGES_REQUIRED", outcome
    summary = outcome["parsed"].findings[0]["summary"]
    assert summary.startswith(f"check timed out after {limit}s; raise verification.{setting}"), summary


def test_loaded_timeout_is_unavailable_and_names_the_setting_once(con, tmp_path, monkeypatch):
    outcome, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", code=124,
                            loaded="host load 90 exceeds 16 (8 CPUs)")
    assert outcome["verdict"] == "UNAVAILABLE", outcome
    assert "timed out after 7200s; raise verification.full_suite_timeout_seconds" in outcome["summary"], outcome
    assert "office resume" in outcome["summary"], outcome
    assert started == [7200]  # no automatic re-run of the full suite
