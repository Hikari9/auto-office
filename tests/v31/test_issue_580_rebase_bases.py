"""#580: task ownership/checks and integrated review follow the rebased base."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from office import convergence, dispatch, integration, rerun
from office.state import Refused


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def moved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Reviewer")
    (repo / "README.md").write_text("original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "task")
    (repo / "task.py").write_text("task = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "task only")
    git(repo, "switch", "-q", "main")
    (repo / "unrelated_feature.py").write_text("unrelated = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "other run already merged")
    new = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "task-wt"
    git(repo, "worktree", "add", "-q", str(wt), "task")
    run = {"id": "a" * 32, "repo_root": str(repo), "base_sha": old,
           "landing": {"rebase": {"onto": new}}}
    return run, repo, wt, old, new


def test_executor_without_dependencies_starts_from_current_rebased_main(moved):
    run, repo, wt, old, new = moved
    assert dispatch._base_for(None, run, {"id": "T1", "depends": []}, {}, None) == new
    assert convergence._base(run) == new


def test_fresh_rerun_merges_new_main_and_task_checks_exclude_unrelated_features(moved):
    run, repo, wt, old, new = moved
    before = git(wt, "rev-parse", "HEAD")
    result = rerun._restack(None, run, {"id": "T1", "depends": []}, str(wt))
    after = git(wt, "rev-parse", "HEAD")
    assert after != before and result["base"] == new, result
    assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", new, after]).returncode == 0
    assert git(repo, "diff", "--name-only", new, after).splitlines() == ["task.py"]
    assert "unrelated_feature.py" in git(repo, "diff", "--name-only", old, after)


def test_task_scope_attribution_uses_revision_base_not_original_run_base(moved):
    run, repo, wt, old, new = moved
    rerun._restack(None, run, {"id": "T1", "depends": []}, str(wt))
    after = git(wt, "rev-parse", "HEAD")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE revisions (id TEXT, changed_json TEXT, commit_sha TEXT, base_commit TEXT)")
    con.execute("INSERT INTO revisions VALUES(?,?,?,?)", ("R1", "[]", after, new))
    assert convergence._changed(con, run, {"accepted_revision_id": "R1"}) == {"task.py"}


def test_integrated_review_does_not_attribute_moved_main_files_to_both_tasks(moved):
    run, repo, wt, old, new = moved
    rerun._restack(None, run, {"id": "T1", "depends": []}, str(wt))
    first = git(wt, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "task2")
    (repo / "task2.py").write_text("other_task = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "task 2")
    second = git(repo, "rev-parse", "HEAD")
    revs = {tid: {"base_commit": new, "commit_sha": sha, "changed_json": "[]"}
            for tid, sha in [("T1", first), ("T2", second)]}
    tasks = [{"id": tid, "interfaces": [], "depends": []} for tid in ["T1", "T2"]]
    required, reason = integration.review_boundary(None, run, tasks, revs)
    assert required is False and "independent scopes" in reason


def test_stale_main_merge_conflict_refuses_rerun_without_corrupting_task_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Reviewer")
    (repo / "shared.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "task")
    (repo / "shared.txt").write_text("task change\n")
    git(repo, "commit", "-qam", "task change")
    git(repo, "switch", "-q", "main")
    (repo / "shared.txt").write_text("unrelated main change\n")
    git(repo, "commit", "-qam", "main change")
    new = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "task-wt"
    git(repo, "worktree", "add", "-q", str(wt), "task")
    before = git(wt, "rev-parse", "HEAD")
    run = {"id": "b" * 32, "repo_root": str(repo), "base_sha": old,
           "landing": {"rebase": {"onto": new}}}
    with pytest.raises(Refused, match="cannot restack onto rebased default branch"):
        rerun._restack(None, run, {"id": "T1", "depends": []}, str(wt))
    assert git(wt, "rev-parse", "HEAD") == before
    assert not git(wt, "diff", "--name-only", "--diff-filter=U")
