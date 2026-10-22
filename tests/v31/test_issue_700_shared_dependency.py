"""Synthetic dependency bases must not turn unchanged shared files into conflicts."""
import pytest

from office import integration, paths
from test_issue_537_shared_append import git


def criss_cross(tmp_path, *, append=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    root = git(repo, "rev-parse", "HEAD")
    for branch in ("a", "b"):
        git(repo, "switch", "-qc", branch, root)
        (repo / (branch + ".py")).write_text(branch + " = 1\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", branch)
    git(repo, "switch", "-qc", "left", "a")
    git(repo, "merge", "--no-ff", "--no-edit", "b")
    git(repo, "switch", "-qc", "right", "b")
    git(repo, "merge", "--no-ff", "--no-edit", "a")
    task_base = git(repo, "rev-parse", "HEAD")
    (repo / "task.py").write_text("task = True\n")
    if append:
        (repo / "registry.txt").write_text("base\ntask entry\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "task changes")
    git(repo, "switch", "left")
    git(repo, "merge", "--no-ff", "--no-edit", "right")
    bases = git(repo, "merge-base", "--all", "HEAD^1", "HEAD^2").splitlines()
    assert len(bases) == 2, "the fixture must exercise real criss-cross ancestry"
    return repo, task_base, bases


@pytest.mark.parametrize("append", [False, True])
def test_composed_task_base_proves_unchanged_or_appended_shared_path(tmp_path, append):
    repo, task_base, _ = criss_cross(tmp_path, append=append)
    changed = integration.preserve_shared_append_multiplicity(
        repo, {"scope": ["+registry.txt"], "base_commit": task_base},
        [{"scope": ["+registry.txt"]}], env=paths.commit_identity_env(repo))
    assert changed is False
    assert (repo / "registry.txt").read_text() == "base\n" + ("task entry\n" if append else "")
    assert not git(repo, "status", "--porcelain")


def test_unproven_ambiguous_ancestry_names_file_and_each_base(tmp_path):
    repo, _, bases = criss_cross(tmp_path, append=True)
    with pytest.raises(integration.SharedAppendUnsafe) as caught:
        integration.preserve_shared_append_multiplicity(
            repo, {"scope": ["+registry.txt"]}, [{"scope": ["+registry.txt"]}],
            env=paths.commit_identity_env(repo))
    text = str(caught.value)
    assert "registry.txt" in text
    assert all(base in text for base in bases)


def test_unreadable_task_base_never_silently_skips_integrity(tmp_path):
    repo, _, _ = criss_cross(tmp_path)
    with pytest.raises(integration.SharedAppendUnsafe, match="registry.txt"):
        integration.preserve_shared_append_multiplicity(
            repo, {"scope": ["+registry.txt"], "base_commit": "f" * 40},
            [{"scope": ["+registry.txt"]}], env=paths.commit_identity_env(repo))
