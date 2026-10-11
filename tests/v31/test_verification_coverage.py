"""#563: a partial pytest tier cannot silently certify integration for landing."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from office import land, verification_coverage as vc
from office.state import Refused


@pytest.fixture
def tiered_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests" / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all", action="store_true")
def pytest_collection_modifyitems(config, items):
    # integration, legacy and slow are deselected unless --all
    if config.getoption("--all"):
        return
''')
    (root / "scripts" / "validate.sh").write_text('''#!/bin/bash
python3 -m pytest tests/ -q -n "${VALIDATE_WORKERS:-2}" --all
''')
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "tier config")
    return root, git("rev-parse", "HEAD")


@pytest.mark.parametrize("commands", [
    [],
    ["python3 -m pytest tests/ -q"],
    ["pytest -q --all tests/v31/test_land.py"],
    ["pytest -q --all -k smoke"],
    ["pytest -q --all -m integration"],
    ["pytest -q --all --ignore tests/v31"],
    ["echo pytest tests/ --all"],
    ["echo scripts/validate.sh"],
    ["cat scripts/validate.sh"],
    ["pytest tests/ --all -k=smoke"],
    ["pytest --all || true"],
    ["pytest --all; exit 0"],
    ["python3 -m pytest tests/ --all -c other.ini"],
    ["pytest --all -o addopts=-m=unit"],
    ["pytest --all -kfoo"],
    ["pytest --all --noconftest"],
    ["pytest --all $(echo tests/unit)"],
    ["python3 -m pytest --all --override-ini addopts='-m not slow'"],
    ["PYTEST_ADDOPTS='-m unit' python3 -m pytest --all"],
    ["env PYTEST_ADDOPTS='-m unit' pytest --all"],
])
def test_partial_coverage_is_reported_not_credited_as_complete(tiered_repo, commands):
    root, sha = tiered_repo
    grade, detail = vc.audit(root, sha, commands)
    assert grade == vc.PARTIAL and "deselected" in detail
    with pytest.raises(Refused, match="integration checks are insufficient") as refused:
        land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": commands}}, sha)
    assert refused.value.category == "integration-coverage-incomplete"


@pytest.mark.parametrize("commands", [
    ["pytest tests/ --all -q"],
    ["python3 -m pytest --all"],
    ["scripts/validate.sh"],
    ["VALIDATE_WORKERS=1 scripts/validate.sh"],
    ["bash scripts/validate.sh"],
    ["uv run pytest tests/ --all"],
    [".venv/bin/python -m pytest tests/ -q -n 2 --all"],
    ["env CI=1 python3 -m pytest --all"],
])
def test_full_suite_commands_are_recognized_and_landable(tiered_repo, commands):
    root, sha = tiered_repo
    assert vc.audit(root, sha, commands)[0] == vc.FULL
    land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": commands}}, sha)


def test_unfixed_validation_script_is_partial_even_when_named(tiered_repo):
    root, _ = tiered_repo
    script = root / "scripts" / "validate.sh"
    script.write_text("python3 -m pytest tests/ -q\n")
    subprocess.run(["git", "-C", str(root), "add", "scripts/validate.sh"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "incomplete gate"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["scripts/validate.sh"])[0] == vc.PARTIAL


def test_non_tiered_repo_preserves_declared_check_policy(tmp_path):
    root = tmp_path / "untiered"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "README.md").write_text("none\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["pytest -q"]) is None
    land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": ["pytest -q"]}}, sha)


def test_repositories_own_validation_script_runs_all_tiers():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "validate.sh").read_text()
    assert any(vc._complete_pytest_command(line) for line in script.splitlines()
               if line.strip().startswith("python3 -m pytest")), "validate.sh must run --all without tier filters"
    assert "unset PYTEST_ADDOPTS PYTEST_PLUGINS" in script


def test_land_cli_refuses_incomplete_tiered_integration_before_any_merge(env, monkeypatch):
    """End-to-end: PASS on an empty/default tier must not merge a real task PR."""
    import json
    from test_land import _plan, _run
    from test_task_prs import gh, remote_head

    (env.repo / "tests").mkdir()
    (env.repo / "tests" / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all")
def pytest_collection_modifyitems(config, items):
    # integration, legacy, slow are normally deselected
    if config.getoption("--all"):
        return
''')
    (env.repo / "scripts").mkdir()
    (env.repo / "scripts" / "validate.sh").write_text("python3 -m pytest tests/ --all\n")
    env.git("add", ".")
    env.git("commit", "-qm", "tiered project")
    bare, _, status = _run(env, monkeypatch, _plan("merge"))
    # Task acceptance and integration composition succeed, but no run-level
    # command proved the extra tiers. Both status and ask-mode must disclose it.
    code, report = env.office("status")
    assert code == 0 and "FULL SUITE NOT VERIFIED" in report, report
    main = remote_head(bare, "main")
    code, out = env.office("land")
    assert code == 4 and "integration-coverage-incomplete" in out, out
    assert "integration, legacy, slow" in out and "scripts/validate.sh" in out, out
    assert remote_head(bare, "main") == main
    assert all(p["state"] == "open" for p in gh(env)["prs"])

    # A handoff close does not merge, but its terminal output and durable
    # archive must still warn the recipient that the full tiers were omitted.
    code, close = env.office("close", "--handoff", "https://github.com/o/r/pull/1")
    assert code == 0 and "FULL SUITE NOT VERIFIED" in close, close
    con = env.con()
    try:
        state_dir = con.execute("SELECT state_dir FROM runs LIMIT 1").fetchone()[0]
    finally:
        con.close()
    receipt = Path(state_dir) / "archive-receipt.json"
    assert receipt.exists()
    import json
    assert json.loads(receipt.read_text())["verification"]["status"] == "partial"
