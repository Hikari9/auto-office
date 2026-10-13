"""#537: clean Git merges are not proof that shared appends are safe."""
from pathlib import Path
import subprocess
import pytest
from office import integration, convergence, paths, state


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def siblings(tmp_path, base=b"header\n", first=b"same\n", second=b"same\n",
             first_mode=False, other=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_bytes(base)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    root = git(repo, "rev-parse", "HEAD")
    for branch, edit in (("T1", first), ("T2", second)):
        git(repo, "switch", "-qc", branch, root)
        (repo / "registry.txt").write_bytes(edit if branch == "T1" and first.startswith(b"REPLACE:") else base + edit)
        if branch == "T1" and first.startswith(b"REPLACE:"):
            (repo / "registry.txt").write_bytes(first.removeprefix(b"REPLACE:"))
        if first_mode and branch == "T1":
            (repo / "registry.txt").chmod(0o755)
        if other:
            (repo / ("other-" + branch)).write_text(branch)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", branch)
    wt = tmp_path / "merge"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), root)
    git(wt, "merge", "--no-ff", "--no-edit", "T1")
    merge = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "T2"],
                           capture_output=True, text=True)
    return repo, wt, root, merge


SHARED = {"scope": ["+registry.txt"]}


def test_clean_header_rewrite_is_rejected(tmp_path, monkeypatch):
    repo, wt, root, merge = siblings(tmp_path, b"header\nbody\n",
                                      b"REPLACE:rewritten\nbody\n", b"append\n")
    assert merge.returncode == 0, merge.stderr
    with pytest.raises(integration.SharedAppendUnsafe, match="non-append"):
        integration.preserve_shared_append_multiplicity(
            wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))
    assert git(wt, "rev-list", "--parents", "-n", "1", "HEAD").count(" ") == 2


@pytest.mark.parametrize(("base", "record", "message"), [
    (b"head\n", b"\x00binary\n", "binary"),
    (b"head\n", b"\xffinvalid\n", "invalid UTF-8"),
])
def test_unsupported_shared_binary_data_blocks_clean_composition(tmp_path, base, record, message):
    repo, wt, root, merge = siblings(tmp_path, base, record, record)
    assert merge.returncode == 0, merge.stderr
    with pytest.raises(integration.SharedAppendUnsafe, match=message):
        integration.preserve_shared_append_multiplicity(
            wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))


def test_shared_file_mode_change_rejected(tmp_path):
    repo, wt, root, merge = siblings(tmp_path, first=b"", second=b"", first_mode=True, other=True)
    assert merge.returncode == 0
    with pytest.raises(integration.SharedAppendUnsafe, match="mode changed"):
        integration.preserve_shared_append_multiplicity(
            wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))


def test_crlf_duplicate_records_preserve_exact_bytes(tmp_path):
    repo, wt, root, merge = siblings(tmp_path, b"head\r\n", b"repeat\r\n", b"repeat\r\n")
    assert merge.returncode == 0
    assert integration.preserve_shared_append_multiplicity(
        wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))
    assert (wt / "registry.txt").read_bytes() == b"head\r\nrepeat\r\nrepeat\r\n"


def test_mixed_non_shared_files_are_preserved(tmp_path):
    repo, wt, root, merge = siblings(tmp_path, first=b"repeat\n", second=b"repeat\n", other=True)
    assert merge.returncode == 0
    integration.preserve_shared_append_multiplicity(
        wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))
    assert (wt / "other-T1").read_text() == "T1"
    assert (wt / "other-T2").read_text() == "T2"
    assert (wt / "registry.txt").read_bytes().count(b"repeat\n") == 2


def test_failed_amend_is_not_silent(tmp_path, monkeypatch):
    repo, wt, root, merge = siblings(tmp_path, first=b"repeat\n", second=b"repeat\n")
    assert merge.returncode == 0
    original = paths.git
    def failing_git(root, *args, **kwargs):
        if "--amend" in args:
            raise paths.GitError(args, 1, "injected failure")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(paths, "git", failing_git)
    with pytest.raises(integration.SharedAppendUnsafe, match="correction failed"):
        integration.preserve_shared_append_multiplicity(
            wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))


def test_criss_cross_ambiguous_clean_merge_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "registry.txt").write_bytes(b"base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    root = git(repo, "rev-parse", "HEAD")
    for branch, file in (("A", "a.txt"), ("B", "b.txt")):
        git(repo, "switch", "-qc", branch, root)
        (repo / file).write_text(branch)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", branch)
    for branch, parent, other in (("MA", "A", "B"), ("MB", "B", "A")):
        git(repo, "switch", "-qc", branch, parent)
        git(repo, "merge", "--no-ff", "--no-edit", other)
    for branch, parent in (("X", "MA"), ("Y", "MB")):
        git(repo, "switch", "-qc", branch, parent)
        with (repo / "registry.txt").open("ab") as f:
            f.write(b"repeat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", branch)
    wt = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), root)
    git(wt, "merge", "--no-ff", "--no-edit", "X")
    merge = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "Y"],
                           capture_output=True, text=True)
    assert merge.returncode == 0, merge.stderr
    assert len(git(wt, "merge-base", "--all", "X", "Y").splitlines()) == 2
    with pytest.raises(integration.SharedAppendUnsafe, match="multiple or missing"):
        integration.preserve_shared_append_multiplicity(
            wt, SHARED, [SHARED], env=paths.commit_identity_env(repo))



def compose_disposable(monkeypatch, tmp_path, repo, root, branches):
    tasks = {name: {"id": name, "depends": [], "scope": ["+registry.txt"],
                    "accepted_revision_id": name} for name in branches}

    class Row:
        def __init__(self, sha):
            self.sha = sha

        def fetchone(self):
            return (self.sha,)

    class Conn:
        def execute(self, statement, args):
            assert "SELECT commit_sha FROM revisions" in statement
            return Row(git(repo, "rev-parse", args[0]))

    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(paths, "worktrees_dir", lambda: tmp_path / "office-worktrees")
    run = {"id": "e" * 32, "repo_root": str(repo), "base_sha": root}
    return convergence._compose(Conn(), run, {"id": "L-T1", "tasks": list(branches)})


def test_nonappend_merge_is_blocked_in_real_convergence(tmp_path, monkeypatch):
    repo, wt, root, merge = siblings(tmp_path, b"header\nbody\n",
                                     b"REPLACE:rewritten\nbody\n", b"append\n")
    assert merge.returncode == 0
    result = compose_disposable(monkeypatch, tmp_path, repo, root, ("T1", "T2"))
    assert result["status"] == "conflict"
    assert "non-append" in result["detail"]


def test_unsupported_binary_is_blocked_in_real_convergence(tmp_path, monkeypatch):
    repo, wt, root, merge = siblings(tmp_path, first=b"\x00data\n", second=b"\x00data\n")
    assert merge.returncode == 0
    result = compose_disposable(monkeypatch, tmp_path, repo, root, ("T1", "T2"))
    assert result["status"] == "conflict"
    assert "binary" in result["detail"]


def test_three_identical_independent_appends_survive_composition(tmp_path, monkeypatch):
    repo, wt, root, merge = siblings(tmp_path, first=b"repeat\n", second=b"repeat\n")
    assert merge.returncode == 0
    git(repo, "switch", "-qc", "T3", root)
    with (repo / "registry.txt").open("ab") as f:
        f.write(b"repeat\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "T3")
    result = compose_disposable(monkeypatch, tmp_path, repo, root, ("T1", "T2", "T3"))
    assert result["status"] == "composed", result
    content = subprocess.check_output(["git", "-C", str(repo), "show",
                                        result["commit"] + ":registry.txt"])
    assert content == b"header\nrepeat\nrepeat\nrepeat\n"
    parents = git(repo, "rev-list", "--parents", "-n", "1", result["commit"])
    assert len(parents.split()) == 3
