"""Two sibling branches append to an explicit shared registry (#537)."""
import subprocess
from pathlib import Path

import pytest
from office import integration, paths


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.mark.parametrize(("first", "second", "expected"), [
    ("A\n", "B\n", "base\nA\nB\n"),
    ("A\nA\n", "A\nB\nA\n", "base\nA\nA\nA\nB\nA\n"),
])
def test_shared_sibling_appends_compose_without_lost_entries(tmp_path, first, second, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    for branch, entry in (("t1", first), ("t2", second)):
        git(repo, "switch", "-qc", branch, base)
        with (repo / "registry.txt").open("a") as f:
            f.write(entry)
        git(repo, "commit", "-qam", branch)
    wt = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), base)
    git(wt, "merge", "--no-ff", "--no-edit", "t1")
    bad = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "t2"],
                         capture_output=True)
    assert bad.returncode != 0
    assert integration.finish_shared_append_merge(
        wt, {"scope": ["+registry.txt"]}, [{"scope": ["+registry.txt"]}],
        env=paths.commit_identity_env(repo))
    assert (wt / "registry.txt").read_text() == expected
    assert git(wt, "rev-list", "--parents", "-n", "1", "HEAD").count(" ") == 2



def test_identical_sibling_appends_do_not_collapse_when_git_merges_cleanly(tmp_path, monkeypatch):
    """Two independently authored identical records are two append operations."""
    repo = tmp_path / "duplicate"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    root = git(repo, "rev-parse", "HEAD")
    for name in ("first", "second"):
        git(repo, "switch", "-qc", name, root)
        with (repo / "registry.txt").open("a") as f:
            f.write("the-same-record\n")
        git(repo, "commit", "-qam", f"{name} independently appends")
    wt = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), root)
    git(wt, "merge", "--no-ff", "--no-edit", "first")
    git(wt, "merge", "--no-ff", "--no-edit", "second")
    assert (wt / "registry.txt").read_text() == "base\nthe-same-record\n", (
        "the fixture must demonstrate Git's clean but lossy identical-append merge")
    integration.preserve_shared_append_multiplicity(
        wt, {"scope": ["+registry.txt"]}, [{"scope": ["+registry.txt"]}],
        env=paths.commit_identity_env(repo))
    assert (wt / "registry.txt").read_text() == "base\nthe-same-record\nthe-same-record\n"

    from office import convergence, state

    revisions = {
        "R1": git(repo, "rev-parse", "first"),
        "R2": git(repo, "rev-parse", "second"),
    }
    tasks = {
        "T1": {"id": "T1", "depends": [], "scope": ["+registry.txt"], "accepted_revision_id": "R1"},
        "T2": {"id": "T2", "depends": [], "scope": ["+registry.txt"], "accepted_revision_id": "R2"},
    }

    class Row:
        def __init__(self, sha):
            self.sha = sha

        def fetchone(self):
            return (self.sha,)

    class Conn:
        def execute(self, sql, args):
            assert "SELECT commit_sha FROM revisions" in sql
            return Row(revisions[args[0]])

    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(paths, "worktrees_dir", lambda: tmp_path / "office-worktrees")
    run = {"id": "a" * 32, "repo_root": str(repo), "base_sha": root}
    composed = convergence._compose(Conn(), run, {"id": "L-shared", "tasks": ["T1", "T2"]})
    assert composed["status"] == "composed", composed
    assert git(repo, "show", composed["commit"] + ":registry.txt") == (
        "base\nthe-same-record\nthe-same-record")
