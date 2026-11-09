"""#1084, #1086: self-review revert proof handles chained checks and new source files."""
from __future__ import annotations
import sqlite3
import subprocess
import sys
from pathlib import Path

from office import briefs, contract
from office import self_review_proof
from office.util import sha256_obj

NEW_RUN = {"id": "r", "gear": "M", "risk_json": None, "gates": {"review_contract": contract.CONVERGENCE}}
RUNNER = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
GOOD_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
IMPORT_ONLY_TEST = "from calc import add\n\ndef test_add():\n    assert True\n"


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


# ------------------------------------------------------------------ #1086: revert proof and new source files

def _repo(tmp_path: Path, test_text: str, source: str, base_source: str | None = None,
          name: str = "calc.py") -> tuple[Path, str, str]:
    """Base has the README and, when `base_source` is given, the source `name` the task modifies to `source`.
    Otherwise the task adds the source `name` as a new file."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("# Repo\n")
    if base_source is not None:
        (wt / name).write_text(base_source, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / name).write_text(source, encoding="utf-8")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text(test_text, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    return wt, base, _git(wt, "rev-parse", "HEAD")


def _prove(wt: Path, base: str, head: str, runner=RUNNER):
    return self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], runner,
                                   ["tests/test_calc.py"], NEW_RUN)


def test_a_fix_in_a_modified_file_still_proves(tmp_path):
    """The fix is a change to `calc.py`, which the base already had. Reverting it fails the test, so it is proven."""
    wt, base, head = _repo(tmp_path, GOOD_TEST, FIXED, base_source=BROKEN)
    result = _prove(wt, base, head)
    assert result["ok"] and result["status"] == "passed", result
    assert "failed reverted and passed restored" in result["reason"]
    assert _git(wt, "status", "--porcelain") == "" and _git(wt, "worktree", "list").count("\n") == 0


def test_a_fix_in_a_new_file_is_exempt_not_passed(tmp_path):
    """#1086 and A34: `calc.py` is new in the task and holds the fix. The revert keeps it, so the test passes
    with the revert and the proof is exempt. It is not recorded as passed."""
    wt, base, head = _repo(tmp_path, GOOD_TEST, FIXED)
    result = _prove(wt, base, head)
    assert result["ok"] and result["status"] == "exempt", result
    assert "fix may live in a file new in the task; revert-and-fail cannot prove it" in result["reason"]


def test_import_only_test_of_a_new_module_is_exempt_not_passed(tmp_path):
    """L-T1 F2: `from calc import add` plus `assert True` must not prove anything. It passes with the revert
    (the new module is kept), so it is exempt, never recorded as passed."""
    wt, base, head = _repo(tmp_path, IMPORT_ONLY_TEST, FIXED)
    result = _prove(wt, base, head)
    assert result["ok"] and result["status"] == "exempt", result


def test_a_cached_exemption_is_reused_as_exempt_not_as_a_pass(tmp_path):
    """A recorded exemption (verdict EXEMPT) answers a later gate call as exempt. Only a PASS verdict is a pass."""
    wt, base, head = _repo(tmp_path, IMPORT_ONLY_TEST, FIXED)
    (wt / briefs.LEDGER_FILE).write_text(
        f"COMMIT {head}\nROUND 1\nLENS edge-cases reviewed\nLENS test-strength reviewed\nLENS platform skipped none\n"
        "FINDING medium edge-cases calc.py:1 | a | fixed tests/test_calc.py mutation=failed\n")
    task = {"id": "T1", "scope": ["calc.py", "tests/**"], "checks": ["pytest -q"]}
    changed = ["calc.py", "tests/test_calc.py"]
    key = sha256_obj({"task": "T1", "base": base, "head": head, "changed": sorted(changed),
                      "tests": ["tests/test_calc.py"], "runner": ["pytest", "-q"],
                      "version": self_review_proof._VERSION})
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE gates (id TEXT, run_id TEXT, task_id TEXT, kind TEXT, input_key TEXT, status TEXT,"
                " verdict TEXT, summary TEXT, created_at TEXT)")
    con.execute("INSERT INTO gates VALUES ('G1', 'r', 'T1', 'self_review_proof', ?, 'done', 'EXEMPT', 'exempt reason',"
                " '2026-01-01')", (key,))
    cached = self_review_proof.gate(con, NEW_RUN, task, wt, base, head, changed, execute=False)
    assert cached["ok"] and cached["status"] == "exempt", cached
    con.execute("UPDATE gates SET verdict = 'PASS'")
    assert self_review_proof.gate(con, NEW_RUN, task, wt, base, head, changed, execute=False)["status"] == "passed"


def test_a_new_file_with_a_non_ascii_name_is_kept_by_the_revert(tmp_path):
    """git quotes non-ASCII names unless listed with -z. A quoted name would miss the added set and the revert
    would delete the new file, failing the test for the wrong reason. The fix stays in the kept file: exempt."""
    test_text = "from naïve import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    wt, base, head = _repo(tmp_path, test_text, FIXED, name="naïve.py")
    result = self_review_proof.prove(wt, base, head, ["naïve.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "exempt", result


def test_a_test_that_passes_without_a_modified_source_fix_still_blocks(tmp_path):
    """A proof against a test that does not exercise a change to a modified source still fails."""
    wt, base, head = _repo(tmp_path, "def test_always_passes():\n    assert True\n",
                           FIXED + "\ndef unused():\n    return 1\n", base_source=FIXED)
    result = _prove(wt, base, head)
    assert not result["ok"]
    assert "passes without the fix" in result["reason"]


def test_an_import_only_test_against_a_modified_source_is_not_exempt(tmp_path):
    """Without a new source file, the same `assert True` test is blocked: exemption needs a new non-test source."""
    wt, base, head = _repo(tmp_path, IMPORT_ONLY_TEST, FIXED + "\ndef unused():\n    return 1\n", base_source=FIXED)
    result = _prove(wt, base, head)
    assert not result["ok"] and result["status"] == "blocked"
    assert "passes without the fix" in result["reason"]


def test_unrelated_import_error_fails_the_proof(tmp_path):
    """A missing-module collection error is never proof, whichever module is missing."""
    unrelated_import = "import no_such_dependency_1086\n\ndef test_add():\n    assert True\n"
    wt, base, head = _repo(tmp_path, unrelated_import, FIXED)
    result = _prove(wt, base, head)
    assert not result["ok"]
    assert "not a test failure" in result["reason"]


def test_bad_pytest_arg_fails_the_proof(tmp_path):
    """A usage error (pytest exit 4) is not a collection error on a new module and must not pass the proof."""
    wt, base, head = _repo(tmp_path, GOOD_TEST, FIXED)
    result = _prove(wt, base, head, runner=[*RUNNER, "--no-such-option-1086"])
    assert not result["ok"]
    assert "exited 4" in result["reason"]


def _repo_new_package_module(tmp_path: Path, test_text: str) -> tuple[Path, str, str]:
    """Base has the package `office/__init__.py`; the task adds the submodule `office/new_runtime.py`."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "office").mkdir()
    (wt / "office" / "__init__.py").write_text("")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "office" / "new_runtime.py").write_text(FIXED)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_new_runtime.py").write_text(test_text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    return wt, base, _git(wt, "rev-parse", "HEAD")


def test_a_fix_in_a_new_package_module_is_exempt_not_passed(tmp_path):
    """Review R5 F1 shape under A34: `from office import new_runtime` with the fix in the new submodule.
    The module is kept by the revert, the test passes, so the proof is exempt."""
    test_text = "from office import new_runtime\n\ndef test_add():\n    assert new_runtime.add(2, 3) == 5\n"
    wt, base, head = _repo_new_package_module(tmp_path, test_text)
    result = self_review_proof.prove(wt, base, head, ["office/new_runtime.py", "tests/test_new_runtime.py"],
                                     RUNNER, ["tests/test_new_runtime.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "exempt", result


def test_from_package_import_of_an_unrelated_name_fails_the_proof(tmp_path):
    """A `cannot import name` from the new module's package, for a name the task did not add, is not the proof."""
    test_text = "from office import unrelated_helper\n\ndef test_add():\n    assert True\n"
    wt, base, head = _repo_new_package_module(tmp_path, test_text)
    result = self_review_proof.prove(wt, base, head, ["office/new_runtime.py", "tests/test_new_runtime.py"],
                                     RUNNER, ["tests/test_new_runtime.py"], NEW_RUN)
    assert not result["ok"]
    assert "not a test failure" in result["reason"]
