"""#897: the full suite runs under verification.full_suite_timeout_seconds, `--pr`
keeps check_timeout_seconds, and a timed-out check names the setting to raise."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from office import db, gates, paths, verification_coverage

RUN = {"id": "r1"}
REV = {"id": "R1", "commit_sha": "0" * 40, "dispatch_id": "D1"}
GATE = {"id": "G1", "task_id": "T1"}
CFG = {"check_timeout_seconds": 1800, "full_suite_timeout_seconds": 7200}
FULL = ["scripts/validate.sh", "./scripts/validate.sh", "scripts/validate.sh --full", "./scripts/validate.sh --full"]
WRAPPED = ["bash scripts/validate.sh", "sh ./scripts/validate.sh --full", "bash scripts/validate.sh --full",
           "env scripts/validate.sh --full", "PYTHONUNBUFFERED=1 scripts/validate.sh"]


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    conn = db.connect(tmp_path / "runs.db")
    conn.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, status, created_at) "
                 "VALUES('G1', 'r1', 'T1', 'T1', 'R1', 'checks', 'k', 'running', '2026-01-01T00:00:00Z')")
    return conn


def _run(con, tmp_path, monkeypatch, command, *, cfg=CFG, code=0, out="", loaded=None, killed=True):
    """Run one check through _run_commands; return (outcome, timeouts each _run_check call received).
    `killed` makes a 124 look like Office's own kill, the output _run_check writes for one."""
    started = []

    def fake_run_check(cmd, cwd, timeout, env):
        started.append(timeout)
        if code == 124 and killed:
            return code, gates._TimedOut(f"timed out after {timeout}s\n{out}")
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
    # A non-default check limit: a hard-coded or full-suite limit for --pr cannot pass this.
    cfg = {"check_timeout_seconds": 3000, "full_suite_timeout_seconds": 7200}
    _, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh --pr", cfg=cfg)
    assert started == [3000]


def test_full_suite_timeout_tie_names_the_full_suite_setting():
    cfg = {"check_timeout_seconds": 7200, "full_suite_timeout_seconds": 7200}
    assert gates.check_timeout(cfg, "scripts/validate.sh") == (7200, "full_suite_timeout_seconds")


def test_exit_124_from_the_process_itself_is_not_named_as_a_timeout(con, tmp_path, monkeypatch):
    outcome, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", code=124, out="tool exited 124\n",
                            killed=False)
    assert started == [7200]
    summary = outcome["parsed"].findings[0]["summary"]
    assert summary.startswith("check failed (exit 124)") and "raise verification" not in summary, summary


def test_exit_124_that_prints_the_kill_prefix_is_not_named_as_a_timeout(con, tmp_path, monkeypatch):
    # The text of a kill is not evidence of one: only _run_check's TimeoutExpired branch marks a kill.
    outcome, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", code=124, killed=False,
                            out="timed out after 7200s\nprinted by the check\n")
    assert started == [7200]
    finding = outcome["parsed"].findings[0]
    assert finding["summary"].startswith("check failed (exit 124)") and "raise verification" not in finding["summary"]
    assert finding["action"] == "make this check pass", finding


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


def test_null_full_suite_setting_falls_back_to_the_default():
    # A YAML key with no value reads as None; it must not raise inside the gate.
    cfg = {"check_timeout_seconds": 1800, "full_suite_timeout_seconds": None}
    assert gates.check_timeout(cfg, "scripts/validate.sh") == (7200, "full_suite_timeout_seconds")


def test_explicit_zero_full_suite_setting_does_not_replace_the_check_timeout():
    cfg = {"check_timeout_seconds": 1800, "full_suite_timeout_seconds": 0}
    assert gates.check_timeout(cfg, "scripts/validate.sh") == (1800, "check_timeout_seconds")


def test_no_src_module_reads_job_timeout_seconds():
    # The job timeout covers the full suite only because nothing enforces it. If a reader is added,
    # it must be at least check_timeout for every check it runs, or it cuts the full suite short.
    readers = [p.name for p in Path(gates.__file__).parent.rglob("*.py")
               if "job_timeout_seconds" in p.read_text(encoding="utf-8")]
    assert readers == [], f"job_timeout_seconds is read by {readers}; make it >= check_timeout for the full suite"


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
    assert outcome["parsed"].findings[0]["action"] == f"raise verification.{setting} or shorten the check"


def test_real_kill_timeout_names_the_setting(con, tmp_path, monkeypatch):
    # No _run_check fake: the timeout text must be the one the real kill writes.
    monkeypatch.setattr(gates, "host_overloaded", lambda: None)
    outcome = gates._run_commands(con, RUN, ["sleep 5"], tmp_path, REV, GATE, {"check_timeout_seconds": 1},
                                  check_tree=False)
    summary = outcome["parsed"].findings[0]["summary"]
    assert summary.startswith("check timed out after 1s; raise verification.check_timeout_seconds"), summary


@pytest.mark.parametrize("command", WRAPPED)
def test_wrapper_timeout_keeps_the_check_limit_and_names_its_setting(con, tmp_path, monkeypatch, command):
    outcome, started = _run(con, tmp_path, monkeypatch, command, code=124, out="partial output\n")
    assert started == [1800]
    summary = outcome["parsed"].findings[0]["summary"]
    assert summary.startswith("check timed out after 1800s; raise verification.check_timeout_seconds"), summary


def test_loaded_exit_124_from_the_process_is_not_named_a_timeout(con, tmp_path, monkeypatch):
    outcome, _ = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", code=124, killed=False,
                      out="tool exited 124\n", loaded="host load 90 exceeds 16 (8 CPUs)")
    assert outcome["verdict"] == "UNAVAILABLE", outcome
    assert "exited 124" in outcome["summary"] and "timed out" not in outcome["summary"], outcome


def test_loaded_timeout_is_unavailable_and_names_the_setting_once(con, tmp_path, monkeypatch):
    outcome, started = _run(con, tmp_path, monkeypatch, "scripts/validate.sh", code=124,
                            loaded="host load 90 exceeds 16 (8 CPUs)")
    assert outcome["verdict"] == "UNAVAILABLE", outcome
    assert "timed out after 7200s; raise verification.full_suite_timeout_seconds" in outcome["summary"], outcome
    assert outcome["summary"].count("raise verification.") == 1, outcome
    assert "office resume" in outcome["summary"], outcome
    assert started == [7200]  # no automatic re-run of the full suite


# AMENDMENT A2: only the canonical argv is full suite. Other shell forms keep check_timeout_seconds.
@pytest.mark.parametrize("command", WRAPPED + ["scripts/validate.sh --pr", "scripts/validate.sh --pr > out.log 2>&1",
                                     "scripts/validate.sh --full > out.log 2>&1", "scripts/validate.sh --full 2>&1",
                                     "scripts/validate.sh --full | tail -5", "scripts/validate.sh --full &",
                                     "scripts/validate.sh --full # note", "bash -e scripts/validate.sh",
                                     "bash -o pipefail scripts/validate.sh", "cd x && scripts/validate.sh --full",
                                     "scripts/validate.sh --pr; scripts/validate.sh --full", "(scripts/validate.sh --full)",
                                     "echo scripts/validate.sh", "pytest -q tests/x.py", "scripts/validate.sh --pr --full",
                                     "cd x && scripts/validate.sh --pr", "echo 'scripts/validate.sh --full'",
                                     "pytest -q tests/x.py ; echo done", "scripts/validate.sh\n--full",
                                     "scripts/validate.sh --full\n", "scripts/validate.sh --full\r"])
def test_non_full_suite_commands_keep_the_check_timeout(command):
    assert not verification_coverage.is_full_suite(command)
    assert gates.check_timeout(CFG, command) == (1800, "check_timeout_seconds")


def test_carriage_return_is_refused_by_both_classifiers():
    # sh does not split on a carriage return, so "--all\r" is not the argv it looks like.
    assert not verification_coverage.is_full_suite("scripts/validate.sh\r")
    assert not verification_coverage._complete_pytest_command("pytest --all\r")


def test_real_check_exit_124_is_a_plain_result_not_a_kill():
    code, out = gates._run_check("sh -c 'echo timed out after 60s; exit 124'", Path("."), 60, {"PATH": "/usr/bin:/bin"})
    assert code == 124 and not isinstance(out, gates._TimedOut), out


def test_real_kill_returns_a_timed_out_result():
    code, out = gates._run_check("sleep 5", Path("."), 1, {"PATH": "/usr/bin:/bin"})
    assert code == 124 and isinstance(out, gates._TimedOut) and out.startswith("timed out after 1s"), out


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
