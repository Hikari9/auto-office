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


def test_new_module_import_failure_passes_the_proof(tmp_path):
    """#1086: with the task's new module removed, the named test fails at collection on its import of that module.

    That failure proves the test depends on the change, so the proof passes.
    """
    fixed_source = "def add(a, b):\n    return a + b\n"
    good_test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    wt, base, head = _repo_with_new_source(tmp_path, good_test, fixed_source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert result["ok"], result
    assert "failed reverted and passed restored" in result["reason"]
    assert _git(wt, "status", "--porcelain") == "" and _git(wt, "worktree", "list").count("\n") == 0


def test_unrelated_import_error_fails_the_proof(tmp_path):
    """An import error that names a module the task did not add is not the proof."""
    source = "def add(a, b):\n    return a + b\n"
    unrelated_import = "import no_such_dependency_1086\n\ndef test_add():\n    assert True\n"
    wt, base, head = _repo_with_new_source(tmp_path, unrelated_import, source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"]
    assert "not a test failure" in result["reason"]


def test_bad_pytest_arg_fails_the_proof(tmp_path):
    """A usage error (pytest exit 4) is not a collection error on a new module and must not pass the proof."""
    source = "def add(a, b):\n    return a + b\n"
    good_test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    wt, base, head = _repo_with_new_source(tmp_path, good_test, source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"],
                                     [*RUNNER, "--no-such-option-1086"], ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"]
    assert "exited 4" in result["reason"]


def test_revert_proof_blocks_when_test_against_new_source_does_not_exercise_change(tmp_path):
    """A proof against a test that does not exercise the change in a new source file still fails."""
    source = "def add(a, b):\n    return a + b\n"
    unrelated_test = "def test_always_passes():\n    assert True\n"
    wt, base, head = _repo_with_new_source(tmp_path, unrelated_test, source)

    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"]
    assert "passes without the fix" in result["reason"]


def test_module_names_cover_every_import_path_of_a_new_source_file():
    assert self_review_proof._module_names("calc.py") == {"calc"}
    assert self_review_proof._module_names("src/office/foo.py") == {"foo", "office.foo", "src.office.foo"}
    assert self_review_proof._module_names("pkg/__init__.py") == {"pkg"}
