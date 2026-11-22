"""#739: a check whose interpreter lacks its test runner is an environment problem
(UNAVAILABLE with the fix), not CHANGES_REQUIRED. A test's own failed import still fails."""
from __future__ import annotations

import pytest

from office import db, gates

RUN = {"id": "r1"}
REV = {"id": "R1", "commit_sha": "0" * 40, "dispatch_id": "D1"}
GATE = {"id": "G1", "task_id": "T1"}


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    return db.connect(tmp_path / "runs.db")


def _outcome(con, tmp_path, monkeypatch, command, out):
    monkeypatch.setattr(gates, "_run_check", lambda cmd, cwd, timeout, env: (1, out))
    monkeypatch.setattr(gates, "host_overloaded", lambda: None)
    return gates._run_commands(con, RUN, [command], tmp_path, REV, GATE, {}, check_tree=False)


@pytest.mark.parametrize("command, out", [
    ("python3 -m pytest -q", "/opt/homebrew/bin/python3: No module named pytest\n"),
    ("pytest tests", "Traceback ...\nModuleNotFoundError: No module named 'pytest'\n"),
    ("npm test && python3 -m pytest", "ok\n/usr/bin/python3: No module named pytest\n"),
])
def test_a_missing_test_runner_is_unavailable_with_the_fix(con, tmp_path, monkeypatch, command, out):
    outcome = _outcome(con, tmp_path, monkeypatch, command, out)
    assert outcome["verdict"] == "UNAVAILABLE", outcome
    assert "no `pytest` module" in outcome["summary"] and "uv run python -m pytest" in outcome["summary"]


@pytest.mark.parametrize("command, out", [
    ("python3 -m pytest -q", "E   ModuleNotFoundError: No module named 'myapp'\n1 error\n"),
    ("npm test", "Error: No module named pytest\n"),
    ("python -m office.tests", "ModuleNotFoundError: No module named 'office.missing'\n"),
    ("python -m office.tests", "ModuleNotFoundError: No module named 'office'\n"),
])
def test_a_tests_own_failed_import_is_still_a_failure(con, tmp_path, monkeypatch, command, out):
    assert _outcome(con, tmp_path, monkeypatch, command, out)["verdict"] == "CHANGES_REQUIRED"
