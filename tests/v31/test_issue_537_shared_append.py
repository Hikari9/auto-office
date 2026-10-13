"""Two sibling branches append to an explicit shared registry (#537)."""
import subprocess
from pathlib import Path
from office import integration, paths


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_shared_sibling_appends_compose_without_lost_entries(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    for branch, entry in (("t1", "A\n"), ("t2", "B\n")):
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
    assert (wt / "registry.txt").read_text() == "base\nA\nB\n"
    assert git(wt, "rev-list", "--parents", "-n", "1", "HEAD").count(" ") == 2
