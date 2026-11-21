from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from office import land, planfile, verification_coverage as vc
from office.state import Refused


@pytest.fixture
def tiered_repo(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "conftest.py").write_text(
        'def pytest_addoption(parser):\n    parser.addoption("--all")\n'
        "def pytest_collection_modifyitems(config, items):\n    pass  # integration legacy slow\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    return root, sha


def test_uncredited_validate_flags_compound_and_prefixed_forms():
    assert vc.uncredited_validate(["uv sync --frozen && scripts/validate.sh --pr",
                                   "scripts/validate.sh --pr > log"]) == [
        "uv sync --frozen && scripts/validate.sh --pr", "scripts/validate.sh --pr > log"]
    assert vc.uncredited_validate(["scripts/validate.sh --pr", "./scripts/validate.sh --pr", "scripts/validate.sh",
                                   "uv sync --frozen", "pytest --all"]) == []


def test_coverage_refusal_names_uncredited_check_and_recommends_pr_first(tiered_repo):
    root, sha = tiered_repo
    commands = ["uv sync --frozen && scripts/validate.sh --pr"]
    with pytest.raises(Refused) as refused:
        land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": commands}}, sha)
    nxt = refused.value.next_step
    assert "uv sync --frozen && scripts/validate.sh --pr" in nxt
    assert nxt.index("scripts/validate.sh --pr") < nxt.index("full")
    assert "last merge of a batch" in nxt


def test_plan_warns_on_uncredited_run_level_validate_check():
    body = ("## Requirements\nchecks: uv sync --frozen && scripts/validate.sh --pr\n\n"
            "## Tasks\n### T1: x\nscope: a.py\nchecks: true\naccept: x\n")
    parsed = planfile.parse(body)
    assert any("not credited" in w for w in parsed.warnings), parsed.warnings
    clean = planfile.parse(body.replace("uv sync --frozen && ", ""))
    assert not any("not credited" in w for w in clean.warnings)
