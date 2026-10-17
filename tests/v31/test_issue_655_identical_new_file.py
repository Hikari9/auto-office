"""Sibling lane tasks that each add the same new byte-identical file (#655)."""
import subprocess

import pytest
from office import convergence, paths, state

MARKER = "# package marker\n"


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def compose(tmp_path, monkeypatch, scopes, *, content=MARKER, target="pkg/__init__.py"):
    """Compose len(scopes) sibling branches, each adding `target`, through the lane composer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    root = git(repo, "rev-parse", "HEAD")
    tasks, revisions = {}, {}
    for i, scope in enumerate(scopes, 1):
        git(repo, "switch", "-qc", f"t{i}", root)
        (repo / target).parent.mkdir(parents=True, exist_ok=True)
        (repo / target).write_text(content)
        (repo / f"pkg/mod{i}.py").write_text(f"x = {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"t{i}")
        revisions[f"R{i}"] = git(repo, "rev-parse", f"t{i}")
        tasks[f"T{i}"] = {"id": f"T{i}", "depends": [], "scope": scope, "accepted_revision_id": f"R{i}"}

    class Row:
        def __init__(self, sha):
            self.sha = sha

        def fetchone(self):
            return (self.sha,)

    class Conn:
        def execute(self, sql, args):
            return Row(revisions[args[0]])

    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(paths, "worktrees_dir", lambda: tmp_path / "office-worktrees")
    run = {"id": "a" * 32, "repo_root": str(repo), "base_sha": root}
    return repo, convergence._compose(Conn(), run, {"id": "L-T1", "tasks": list(tasks)})


@pytest.mark.parametrize("n", [2, 3, 4])
def test_undeclared_identical_new_file_composes(tmp_path, monkeypatch, n):
    repo, composed = compose(tmp_path, monkeypatch, [[f"pkg/mod{i}.py", "pkg/__init__.py"] for i in range(1, n + 1)])
    assert composed["status"] == "composed", composed
    assert git(repo, "show", composed["commit"] + ":pkg/__init__.py") == MARKER.strip()


@pytest.mark.parametrize("n", [2, 3, 4])
def test_declared_shared_identical_new_file_composes(tmp_path, monkeypatch, n):
    repo, composed = compose(tmp_path, monkeypatch, [[f"pkg/mod{i}.py", "+pkg/__init__.py"] for i in range(1, n + 1)])
    assert composed["status"] == "composed", composed
    assert git(repo, "show", composed["commit"] + ":pkg/__init__.py") == MARKER.strip()


def test_declared_shared_differing_new_file_still_conflicts(tmp_path, monkeypatch):
    """Only byte-identical adds converge; an add/add with different content stays a conflict."""
    repo, composed = compose(tmp_path, monkeypatch, [["pkg/mod1.py", "+pkg/__init__.py"]] * 2)
    assert composed["status"] == "composed"
    # Rebuild with T2's marker differing: compose() writes one content for all, so patch T2 afterwards.
    git(repo, "switch", "-q", "t2")
    (repo / "pkg/__init__.py").write_text("# different\n")
    git(repo, "commit", "-qam", "t2 differs")
    revisions_sha = git(repo, "rev-parse", "t2")

    class Row:
        def __init__(self, sha):
            self.sha = sha

        def fetchone(self):
            return (self.sha,)

    class Conn:
        def execute(self, sql, args):
            return Row({"R1": git(repo, "rev-parse", "t1"), "R2": revisions_sha}[args[0]])

    tasks = {f"T{i}": {"id": f"T{i}", "depends": [], "scope": ["+pkg/__init__.py"], "accepted_revision_id": f"R{i}"}
             for i in (1, 2)}
    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: tasks[tid])
    run = {"id": "b" * 32, "repo_root": str(repo), "base_sha": git(repo, "rev-parse", "main")}
    result = convergence._compose(Conn(), run, {"id": "L-T1", "tasks": ["T1", "T2"]})
    assert result["status"] == "conflict", result
