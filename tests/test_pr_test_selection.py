"""Regression coverage for Auto-Office's targeted PR selector (#854)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.select_pr_tests import SelectionError, selection


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def pr_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for path, text in {
        "tests/test_version_single_source.py": "def test_version(): pass\n",
        "tests/v31/test_verification_coverage.py": "def test_coverage(): pass\n",
        "tests/test_pr_test_selection.py": "def test_selector(): pass\n",
        "tests/test_routing.py": "from office import routing\n",
        "tests/v31/test_routing_fallback.py": "from office.routing import dispatch\n",
        "tests/test_unrelated.py": "from office import irrelevant\n",
        "src/office/routing.py": "def dispatch(): pass\n",
        "README.md": "baseline\n",
    }.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "add", ".")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "baseline"], check=True)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "checkout", "-qb", "feature")
    return root


def test_changed_runtime_module_selects_importers_but_not_all_tests(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 2\n")
    base, changed, chosen = selection(pr_repo)
    assert len(base) == 40
    assert changed == ["src/office/routing.py"]
    assert "tests/test_routing.py" in chosen
    assert "tests/v31/test_routing_fallback.py" in chosen
    assert "tests/test_unrelated.py" not in chosen
    assert all(p.startswith("tests/") and (pr_repo / p).is_file() for p in chosen)


def test_modified_test_module_is_selected(pr_repo):
    (pr_repo / "tests/test_unrelated.py").write_text("def test_new(): assert True\n")
    _, _, chosen = selection(pr_repo)
    assert "tests/test_unrelated.py" in chosen


def test_docs_change_still_checks_cross_cutting_contracts(pr_repo):
    (pr_repo / "README.md").write_text("new docs\n")
    _, changed, chosen = selection(pr_repo)
    assert changed == ["README.md"]
    assert set(chosen) == {
        "tests/test_version_single_source.py",
        "tests/v31/test_verification_coverage.py",
        "tests/test_pr_test_selection.py",
    }


def test_missing_base_fails_closed(pr_repo):
    (pr_repo / "README.md").write_text("new docs\n")
    with pytest.raises(SelectionError, match="rev-parse"):
        selection(pr_repo, "refs/remotes/origin/not-real")


def test_no_changes_cannot_attest_scoped_gate(pr_repo):
    with pytest.raises(SelectionError, match="No changed files"):
        selection(pr_repo)


def test_deleted_test_refuses_to_silently_lose_coverage(pr_repo):
    (pr_repo / "tests/test_unrelated.py").unlink()
    with pytest.raises(SelectionError, match="test file was deleted"):
        selection(pr_repo)


def test_many_indirect_importers_are_bounded(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 2\n")
    for idx in range(30):
        p = pr_repo / f"tests/test_extra_{idx:02d}.py"
        p.write_text("from office import routing\n")
    _, _, chosen = selection(pr_repo)
    extras = [p for p in chosen if p.startswith("tests/test_extra_")]
    assert len(extras) <= 4
    assert "tests/test_routing.py" in chosen
    assert "tests/v31/test_routing_fallback.py" in chosen


def test_uses_merge_base_so_updated_main_does_not_add_unrelated_tests(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 3\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "feature"], check=True)
    # An unrelated commit on main should not become part of the feature diff.
    git(pr_repo, "checkout", "main")
    (pr_repo / "README.md").write_text("main advanced\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "main"], check=True)
    git(pr_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(pr_repo, "checkout", "feature")
    _, changed, _ = selection(pr_repo)
    assert changed == ["src/office/routing.py"]
