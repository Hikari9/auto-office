"""#897: the full suite runs under verification.full_suite_timeout_seconds, `--pr`
keeps check_timeout_seconds, and a timed-out check names the setting to raise."""
from __future__ import annotations

import subprocess

import pytest

from office import db, gates, paths, verification_coverage

RUN = {"id": "r1"}
REV = {"id": "R1", "commit_sha": "0" * 40, "dispatch_id": "D1"}
GATE = {"id": "G1", "task_id": "T1"}
CFG = {"check_timeout_seconds": 1800, "full_suite_timeout_seconds": 7200}
FULL = ["scripts/validate.sh", "./scripts/validate.sh", "bash scripts/validate.sh", "scripts/validate.sh --full",
        "./scripts/validate.sh --full"]


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


# AMENDMENT A2: only the canonical argv is full suite. Other shell forms keep check_timeout_seconds.
@pytest.mark.parametrize("command", ["scripts/validate.sh --pr", "scripts/validate.sh --pr > out.log 2>&1",
                                     "scripts/validate.sh --full > out.log 2>&1", "scripts/validate.sh --full 2>&1",
                                     "scripts/validate.sh --full | tail -5", "scripts/validate.sh --full &",
                                     "scripts/validate.sh --full # note", "bash -e scripts/validate.sh",
                                     "bash -o pipefail scripts/validate.sh", "cd x && scripts/validate.sh --full",
                                     "scripts/validate.sh --pr; scripts/validate.sh --full", "(scripts/validate.sh --full)",
                                     "echo scripts/validate.sh", "pytest -q tests/x.py", "scripts/validate.sh --pr --full",
                                     "cd x && scripts/validate.sh --pr", "echo 'scripts/validate.sh --full'",
                                     "pytest -q tests/x.py ; echo done"])
def test_non_full_suite_commands_keep_the_check_timeout(command):
    assert not verification_coverage.is_full_suite(command)
    assert gates.check_timeout(CFG, command) == (1800, "check_timeout_seconds")


def _commit(repo, name, text):
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", name], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.parametrize("command, limit", [("scripts/validate.sh", 7200), ("scripts/validate.sh --pr", 1800)])
def test_preexisting_rerun_on_base_uses_the_same_timeout(con, tmp_path, monkeypatch, command, limit):
    # The base re-run of a failing check must get the limit the check got on the task revision.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    base = _commit(repo, "calc.py", "x = 1\n")
    head = _commit(repo, "calc.py", "x = 2\n")
    log = tmp_path / "check-1.log"
    log.write_text("boom\n")
    checkout = tmp_path / "base"
    checkout.mkdir()
    (paths.run_dir("r1") / "evidence" / "T1" / "R1").mkdir(parents=True)  # _run_commands creates this first
    started = []

    monkeypatch.setattr(gates.state, "pinned_config", lambda run: {"verification": CFG})
    monkeypatch.setattr(gates, "detached_checkout", lambda *a, **k: checkout)
    monkeypatch.setattr(gates, "remove_checkout", lambda *a, **k: None)
    monkeypatch.setattr(gates, "_run_check", lambda cmd, cwd, timeout, env: (started.append(timeout) or (1, "boom\n")))
    outcome = {"results": [{"command": command, "exit": 1, "log": str(log), "cwd": str(repo)}]}
    rev = {"id": "R1", "commit_sha": head, "base_commit": base, "dispatch_id": "D1"}
    run = {"id": "r1", "repo_root": str(repo)}
    gates.preexisting_failures(con, run, rev, GATE, outcome)
    assert started == [limit]
