"""#1084, #1086: self-review revert proof handles chained checks and new source files."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from office import contract, self_review_proof

NEW_RUN = {"id": "r", "gear": "M", "risk_json": None, "gates": {"review_contract": contract.CONVERGENCE}}
RUNNER = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------------ #1084: chained checks in _runner

def test_runner_stops_at_shell_operator_chained_checks():
    """#1084: `_runner` stops at `&&`, `||`, `;`, `|` and keeps only the pytest invocation."""
    task = {"checks": ["pytest tests/test_calc.py -q && dotnet run --project X"]}
    assert self_review_proof._runner(task) == ["pytest", "-q"]

    task_dotnet = {
        "checks": [
            "uv run --frozen python -m pytest tests/ -q && DOTNET_ROLL_FORWARD=Major dotnet run --project <csproj>"
        ]
    }
    assert self_review_proof._runner(task_dotnet) == ["uv", "run", "--frozen", "python", "-m", "pytest", "-q"]

    assert self_review_proof._runner({"checks": ["pytest tests/ -v || exit 1"]}) == ["pytest", "-v"]
    assert self_review_proof._runner({"checks": ["pytest tests/ -q ; echo done"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["pytest tests/ -q | tee log.txt"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["setup.sh && pytest -q tests/"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["pytest tests/ -q; dotnet run --project X"]}) == ["pytest", "-q"]


def test_runner_splits_operators_glued_to_words():
    """#1084: an operator with no surrounding space must still end the pytest command, not join a flag or fail open."""
    assert self_review_proof._runner({"checks": ["pytest -q;echo hi"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["pytest -q|tee log"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["pytest -q&&echo hi"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["echo ok;pytest -q"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["setup.sh&&pytest -q tests/"]}) == ["pytest", "-q"]
    assert self_review_proof._runner({"checks": ["pytest -k 'a and b' -q && echo done"]}) == ["pytest", "-q"]


# ------------------------------------------------------------------ #1086: new source files in revert proof

def _repo_with_new_source(tmp_path: Path, test_text: str, source: str) -> tuple[Path, str, str]:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    # Base commit has only a README; calc.py does not exist in base.
    (wt / "README.md").write_text("# Repo\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD")

    # Task adds calc.py (a new source file) and tests/test_calc.py.
    (wt / "calc.py").write_text(source)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text(test_text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    return wt, base, _git(wt, "rev-parse", "HEAD")


def test_revert_proof_handles_new_source_file_and_fails_on_assertion(tmp_path):
    """#1086: Reversing a diff for a new source file would delete it and exit with an import/collection error.

    The proof's reverse step leaves the new source file in a stubbed state so test collection succeeds
    and the test fails on assertion, proving the fix.
    """
    fixed_source = "def add(a, b):\n    return a + b\n"
    good_test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    wt, base, head = _repo_with_new_source(tmp_path, good_test, fixed_source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert result["ok"], result
    assert "failed reverted and passed restored" in result["reason"]
    assert _git(wt, "status", "--porcelain") == "" and _git(wt, "worktree", "list").count("\n") == 0


def test_revert_proof_blocks_when_test_against_new_source_does_not_exercise_change(tmp_path):
    """A proof against a test that does not exercise the change in a new source file still fails."""
    source = "def add(a, b):\n    return a + b\n"
    unrelated_test = "def test_always_passes():\n    assert True\n"
    wt, base, head = _repo_with_new_source(tmp_path, unrelated_test, source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"]
    assert "passes without the fix" in result["reason"]


def test_stub_python_source_handles_classes_and_functions():
    code = """
import os

TIMEOUT = 10

class Calc:
    def __init__(self, mode="normal"):
        self.mode = mode

    def compute(self, x: int) -> int:
        return x * 2

async def fetch_data(url: str):
    return url
"""
    stubbed = self_review_proof._stub_python_source(code)
    assert "class Calc:" in stubbed
    assert "def __init__(self, mode='normal'):" in stubbed
    assert "def compute(self, x: int) -> int:" in stubbed
    assert "async def fetch_data(url: str):" in stubbed
    assert "return x * 2" not in stubbed
