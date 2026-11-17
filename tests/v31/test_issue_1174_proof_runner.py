"""#1122, #1124, #1129, #1174, #1241: the self-review proof runner keeps option values, strips ambient pytest
options, names environment failures and reverts every non-test source."""
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


def _repo(tmp_path: Path, files_base: dict, files_head: dict) -> tuple[Path, str, str]:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("# Repo\n")
    for name, text in files_base.items():
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD")
    for name, text in files_head.items():
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    return wt, base, _git(wt, "rev-parse", "HEAD")


@pytest.mark.parametrize("check, expected", [
    ("uv run --with pytest python3 -m pytest -q -n 0 -p no:cacheprovider tests",
     ["uv", "run", "--with", "pytest", "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
    ("pytest -q -c ci/pytest.ini tests/a.py", ["pytest", "-q", "-c", "ci/pytest.ini"]),
    ("pytest -o addopts= -o cache_dir=/tmp/x tests/", ["pytest", "-o", "addopts=", "-o", "cache_dir=/tmp/x"]),
    ("pytest --rootdir /repo -W error::UserWarning tests/x.py::t",
     ["pytest", "--rootdir", "/repo", "-W", "error::UserWarning"]),
    ("pytest -n4 -k 'a and b' -m slow -x tests", ["pytest", "-x"]),
    ("pytest -nauto --numprocesses 3 --numprocesses=2 -q", ["pytest", "-q"]),
    ("pytest -kfoo -mbar -q tests/a.py", ["pytest", "-q"]),
    ("pytest -p no:cacheprovider -pno:randomly -q tests", ["pytest", "-p", "no:cacheprovider", "-pno:randomly", "-q"]),
    (".venv/bin/pytest -q tests", [".venv/bin/pytest", "-q"]),
])
def test_runner_keeps_option_values_and_drops_selection(check, expected):
    assert self_review_proof._runner({"checks": [check]}) == expected


def test_option_value_is_not_dropped_as_a_path():
    """A value of `-p` or `-c` must survive: without it the command is `pytest -p -q` and exits 4."""
    kept = self_review_proof._runner({"checks": ["pytest -p no:cacheprovider tests"]})
    assert kept == ["pytest", "-p", "no:cacheprovider"]


def test_proof_environment_strips_ambient_pytest_options(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches")
    monkeypatch.setenv("PYTEST_PLUGINS", "no_such_plugin_1174")
    code, out = self_review_proof._run(tmp_path, f"{sys.executable} -c "
                                       "\"import os; print(os.environ.get('PYTEST_ADDOPTS'), "
                                       "os.environ.get('PYTEST_PLUGINS'))\"", NEW_RUN)
    assert code == 0 and out.strip() == "None None", out


GOOD = "def f():\n    return 1\n"
BAD = "def f():\n    return 0\n"
TEST = "from calc import f\n\ndef test_f():\n    assert f() == 1\n"


def test_exit_127_is_an_environment_error_with_command_checkout_and_tail(tmp_path):
    wt, base, head = _repo(tmp_path, {"calc.py": BAD}, {"calc.py": GOOD, "tests/test_calc.py": TEST})
    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"],
                                     ["no-such-interpreter-1174", "-m", "pytest"], ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"]
    reason = result["reason"]
    assert "exited 127" in reason and "environment error" in reason
    assert "no-such-interpreter-1174" in reason and "command:" in reason and "checkout:" in reason
    assert "ledger" not in reason and "fails on the submitted commit" not in reason


def test_green_run_pytest_usage_error_is_environment_not_a_failing_commit(tmp_path):
    """The reverted run must pass the usage check first, so make the option bad only for both runs: exit 4
    on the red run already names the environment error and never claims the commit fails."""
    wt, base, head = _repo(tmp_path, {"calc.py": BAD}, {"calc.py": GOOD, "tests/test_calc.py": TEST})
    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"],
                                     [*RUNNER, "--no-such-option-1174"], ["tests/test_calc.py"], NEW_RUN)
    assert not result["ok"] and "exited 4" in result["reason"] and "environment error" in result["reason"]
    assert "fails on the submitted commit" not in result["reason"]


def test_green_failure_names_command_checkout_and_bounded_tail(tmp_path):
    noisy = "def test_f():\n    for i in range(60):\n        print('line', i)\n    assert False\n"
    wt, base, head = _repo(tmp_path, {"calc.py": BAD}, {"calc.py": GOOD, "tests/test_calc.py": noisy})
    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], RUNNER,
                                     ["tests/test_calc.py"], NEW_RUN)
    # The test fails with and without the revert: the red run passes the failure check, the green run refuses.
    assert not result["ok"] and "fails on the submitted commit (exit 1)" in result["reason"]
    reason = result["reason"]
    assert "command:" in reason and "tests/test_calc.py" in reason and "checkout:" in reason
    assert "restored" in reason
    assert len(reason.split("output tail:\n", 1)[1].splitlines()) <= 20


def test_a_repo_relative_untracked_interpreter_resolves_against_the_worktree(tmp_path):
    wt, base, head = _repo(tmp_path, {"calc.py": BAD}, {"calc.py": GOOD, "tests/test_calc.py": TEST})
    link = wt / ".venv" / "bin"
    link.mkdir(parents=True)
    (link / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')  # a symlink would lose its venv
    (link / "python").chmod(0o755)
    (wt / ".gitignore").write_text(".venv/\n")  # untracked: absent from the disposable checkouts
    runner = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    result = self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], runner,
                                     ["tests/test_calc.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "passed", result


@pytest.mark.parametrize("suffix", [".lava", ".liquid", ".j2", ".jinja", ".jinja2", ".hbs", ".mustache", ".erb",
                                    ".twig", ".njk", ".ejs", ".tpl"])
def test_template_files_are_revertable_source(suffix):
    assert self_review_proof._sources([f"web/page{suffix}", "tests/test_page.py"]) == [f"web/page{suffix}"]


def test_a_lava_only_fix_is_reverted_and_proven(tmp_path):
    page = "page.lava"
    test = f"from pathlib import Path\n\ndef test_page():\n    assert Path('{page}').read_text() == 'fixed'\n"
    wt, base, head = _repo(tmp_path, {page: "broken"}, {page: "fixed", "tests/test_page.py": test})
    changed = [page, "tests/test_page.py"]
    assert self_review_proof._sources(changed) == [page]  # not skipped as not-required
    result = self_review_proof.prove(wt, base, head, changed, RUNNER, ["tests/test_page.py"], NEW_RUN)
    assert result["ok"] and result["status"] == "passed", result
