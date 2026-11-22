"""#773: a stacked task may edit a keyed row of a shared file in place when the composition still
holds the file exactly as that task's recorded base. Concurrent edits are still refused."""
import subprocess

from office import convergence, integration, paths, state

APP = "import a\nroutes = [\n  a,\n]\nexport default routes\n"
APP_T2 = "import a\nimport b\nroutes = [\n  a,\n  b,\n]\nexport default routes\n"
APP_T3 = "import a\nimport b\nimport c\nroutes = [\n  a,\n  b,\n  c,\n]\nexport default routes\n"


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(repo, branch, parent, content, name="src_app.ts"):
    git(repo, "switch", "-qc", branch, parent)
    (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", branch)
    return git(repo, "rev-parse", "HEAD")


def fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "src_app.ts").write_text(APP)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def compose(monkeypatch, tmp_path, repo, root, revisions, depends):
    """revisions: {task: (commit, base_commit)}; depends: {task: [deps]}."""
    tasks = {t: {"id": t, "depends": depends.get(t, []), "scope": ["+src_app.ts"], "accepted_revision_id": t}
             for t in revisions}

    class Row:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Conn:
        def execute(self, statement, args):
            return Row(revisions[args[0]])

    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(paths, "worktrees_dir", lambda: tmp_path / "office-worktrees")
    run = {"id": "89ad3c5d" + "0" * 24, "repo_root": str(repo), "base_sha": root}
    return convergence._compose(Conn(), run, {"id": "L-phase-0-1", "tasks": list(revisions)})


def test_stacked_task_with_in_place_edits_composes(monkeypatch, tmp_path):
    repo, root = fixture(tmp_path)
    t2 = commit(repo, "T2", root, APP_T2)
    t3 = commit(repo, "T3", t2, APP_T3)  # +7/-0 in the middle of the file: not a suffix append
    result = compose(monkeypatch, tmp_path, repo, root, {"T2": (t2, root), "T3": (t3, t2)}, {"T3": ["T2"]})
    assert result["status"] == "composed", result
    assert git(repo, "show", f"{result['commit']}:src_app.ts") == APP_T3.rstrip("\n")


def test_stacked_task_still_refused_when_composition_changed_the_file(monkeypatch, tmp_path):
    """A sibling that is not an ancestor of T3 edited the file after T3's base: the proof does not apply."""
    repo, root = fixture(tmp_path)
    t2 = commit(repo, "T2", root, APP_T2)
    sibling = commit(repo, "S", root, APP + "// sibling\n")
    t3 = commit(repo, "T3", t2, APP_T3)
    result = compose(monkeypatch, tmp_path, repo, root,
                     {"S": (sibling, root), "T2": (t2, root), "T3": (t3, t2)}, {"T3": ["T2"]})
    assert result["status"] == "conflict"
    assert "non-append" in result["detail"]


def test_next_hint_does_not_say_serialize_when_already_serialized():
    t2 = {"id": "T2", "depends": [], "scope": ["+f"]}
    t3 = {"id": "T3", "depends": ["T2"], "scope": ["+f"]}
    t4 = {"id": "T4", "depends": ["T3"], "scope": ["+f"]}
    already = integration.shared_append_next(t4, [t2, t3])
    assert "already serialized" in already and "serialize the tasks" not in already
    parallel = integration.shared_append_next({"id": "T5", "depends": [], "scope": ["+f"]}, [t2, t3])
    assert parallel.startswith("serialize the tasks")
