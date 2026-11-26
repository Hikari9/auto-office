"""#1373: the self-review proof reverts data files with the source, so data fixes and loader+data changes prove."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

from office import contract, self_review_proof

NEW_RUN = {"id": "r", "gear": "M", "risk_json": None, "gates": {"review_contract": contract.CONVERGENCE}}
RUNNER = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
LOADER_BASE = (
    "import json, pathlib\n\n"
    "def load():\n"
    "    data = json.loads((pathlib.Path(__file__).parent / 'policy.json').read_text())\n"
    "    if set(data) - {'size'}:\n"
    "        raise ValueError('unknown key')\n"
    "    return data\n\n"
    "POLICY = load()  # validated at import, like office.executor_slates\n")
LOADER_HEAD = LOADER_BASE.replace("{'size'}", "{'size', 'canary'}")
TEST = "from loader import POLICY\n\ndef test_policy():\n    assert POLICY == {'size': 'S', 'canary': 'x'}\n"


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(wt: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text(text, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", message)
    return _git(wt, "rev-parse", "HEAD")


def _repo(tmp_path: Path, base: dict[str, str], head: dict[str, str]) -> tuple[Path, str, str]:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    return wt, _commit(wt, base, "base"), _commit(wt, head, "work")


def test_a_loader_changed_with_its_data_is_reverted_together_and_fails_by_a_test_failure(tmp_path):
    """The task teaches the loader a new key and adds that key to the data. Before #1373 only loader.py was
    reverted, so the old loader met the new data. A loader that validates at import made the test error at collection
    (exit 4, never a proof). Now both revert, and the test fails by its assertion."""
    wt, base, head = _repo(
        tmp_path,
        {"loader.py": LOADER_BASE, "policy.json": '{"size": "M"}\n', "README.md": "# r\n"},
        {"loader.py": LOADER_HEAD, "policy.json": '{"size": "S", "canary": "x"}\n', "tests/test_policy.py": TEST})
    changed = ["loader.py", "policy.json", "tests/test_policy.py"]
    result = self_review_proof.prove(wt, base, head, changed, RUNNER, ["tests/test_policy.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "passed", result


def test_a_fix_in_a_data_file_is_proven(tmp_path):
    """The medium fix lives in policy.json only; the task also changes source. Reverting the data fails the test."""
    loader = LOADER_HEAD
    wt, base, head = _repo(
        tmp_path,
        {"loader.py": loader, "policy.json": '{"size": "M"}\n'},
        {"loader.py": loader + "\nVERSION = 2\n", "policy.json": '{"size": "S", "canary": "x"}\n',
         "tests/test_policy.py": TEST})
    changed = ["loader.py", "policy.json", "tests/test_policy.py"]
    result = self_review_proof.prove(wt, base, head, changed, RUNNER, ["tests/test_policy.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "passed", result


def test_test_directory_data_is_not_reverted(tmp_path):
    """A fixture under tests/ belongs to the test: it keeps the submitted version, whatever its suffix."""
    assert self_review_proof._revertible(
        ["src/a.py", "catalog/x.yaml", "docs/a.md", "tests/fixtures/data.json", "tests/test_a.py",
         "pkg/Calc.Tests/case.json", "spec/x.yml"]) == ["catalog/x.yaml", "docs/a.md", "src/a.py"]


def _prove(wt, base, head, changed, test="tests/test_policy.py"):
    return self_review_proof.prove(wt, base, head, changed, RUNNER, [test], NEW_RUN)


def test_an_added_doc_does_not_exempt_a_test_that_passes_without_the_fix(tmp_path):
    """Only an added source can hold an unreachable fix. A task that modifies source and adds a doc, with a named
    test that passes on the reverted checkout, is still blocked."""
    wt, base, head = _repo(
        tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"},
        {"calc.py": "def add(a, b):\n    return a + b\n\nX = 1\n", "docs/new.md": "# new\n",
         "data/new.json": "{}\n", "tests/test_policy.py": "def test_ok():\n    assert True\n"})
    result = _prove(wt, base, head, ["calc.py", "data/new.json", "docs/new.md", "tests/test_policy.py"])
    assert not result["ok"] and result["status"] == "blocked", result
    assert "passes without the fix" in result["reason"]


def test_a_fixture_under_tests_keeps_the_submitted_version(tmp_path):
    """The test reads a new fixture key at import. If the fixture were reverted the test would error at collection
    (never a proof). It stays at head, so only the source revert fails the test, by its assertion."""
    test = ("import json, pathlib\nfrom calc import add\n"
            "CASE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'case.json').read_text())['sum']\n\n"
            "def test_add():\n    assert add(2, 3) == CASE\n")
    wt, base, head = _repo(
        tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n", "tests/fixtures/case.json": "{}\n"},
        {"calc.py": "def add(a, b):\n    return a + b\n", "tests/fixtures/case.json": '{"sum": 5}\n',
         "tests/test_policy.py": test})
    result = _prove(wt, base, head, ["calc.py", "tests/fixtures/case.json", "tests/test_policy.py"])
    assert result["ok"] and result["status"] == "passed", result


def test_runner_configuration_is_not_reverted(tmp_path):
    """The task registers a marker under --strict-markers in pyproject.toml and its fix test uses it. Reverting
    pyproject.toml would fail the test at collection; it stays at head, so the genuine proof passes."""
    base_cfg = '[tool.pytest.ini_options]\naddopts = "--strict-markers"\nmarkers = []\n'
    head_cfg = base_cfg.replace("markers = []", 'markers = ["canary: new marker"]')
    test = ("import pytest\nfrom calc import add\n\n"
            "@pytest.mark.canary\ndef test_add():\n    assert add(2, 3) == 5\n")
    wt, base, head = _repo(
        tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n", "pyproject.toml": base_cfg},
        {"calc.py": "def add(a, b):\n    return a + b\n", "pyproject.toml": head_cfg, "tests/test_policy.py": test})
    changed = ["calc.py", "pyproject.toml", "tests/test_policy.py"]
    assert self_review_proof._revertible(changed) == ["calc.py"]
    result = _prove(wt, base, head, changed)
    assert result["ok"] and result["status"] == "passed", result
