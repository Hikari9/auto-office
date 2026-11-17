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
    with pytest.raises(Refused, match="cannot restack onto rebased default branch") as exc:
        rerun._restack(None, run, {"id": "T1", "depends": []}, str(wt))
    assert exc.value.category == "rebase-conflict" and "by hand" in exc.value.next_step
    assert git(wt, "rev-parse", "HEAD") == before
    assert not git(wt, "diff", "--name-only", "--diff-filter=U")

def test_second_merge_conflict_does_not_advance_original_task_head(tmp_path, monkeypatch):
    from office import state
    repo = tmp_path / "atomic"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Reviewer")
    git(repo, "config", "user.email", "review@example.test")
    (repo / "shared.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "task")
    (repo / "task.py").write_text("task = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "task")
    git(repo, "switch", "-q", "main")
    (repo / "shared.txt").write_text("main\n")
    git(repo, "commit", "-qam", "new main")
    new = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "dep", old)
    (repo / "shared.txt").write_text("other dependency\n")
    git(repo, "commit", "-qam", "dependency")
    dep = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    wt = tmp_path / "task-worktree"
    git(repo, "worktree", "add", "-q", str(wt), "task")
    before = git(wt, "rev-parse", "HEAD")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE revisions (id TEXT PRIMARY KEY, commit_sha TEXT)")
    con.execute("INSERT INTO revisions VALUES ('R2',?)", (dep,))
    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: {
        "id": tid, "status": "accepted", "accepted_revision_id": "R2"})
    run = {"id": "c" * 32, "repo_root": str(repo), "base_sha": old,
           "landing": {"rebase": {"onto": new}}}
    result = rerun._restack(con, run, {"id": "T1", "depends": ["T2"]}, str(wt))
    assert result["conflict"]["task"] == "T2" and result["base"] is None, result
    assert git(wt, "rev-parse", "HEAD") == before
    assert git(wt, "status", "--porcelain") == ""


def test_rebased_run_dependency_conflict_launches_with_the_conflict_recorded(env):
    """#1035: only a conflict with the rebased default branch refuses; a dependency conflict launches."""
    from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, task_row
    from test_convergence_contract import APPROVED, _start

    external = {"OFFICE_WORKER_LAUNCHER": "external"}
    work = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}
    _start(env, plan=PLAN_TWO, executor=[{"write_by_task": work, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    assert env.office("dispatch", "T1", "T2", "--parallel", check=0)[0] == 0
    con = env.con()
    run_row = con.execute("SELECT base_sha, landing_json FROM runs").fetchone()
    tree = env.git("rev-parse", run_row["base_sha"] + "^{tree}").strip()
    onto = env.git("commit-tree", tree, "-p", run_row["base_sha"], "-m", "main moved").strip()
    landing = json.loads(run_row["landing_json"] or "{}")
    landing["rebase"] = {"onto": onto}
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.commit()

    disp = con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env, "T2")["current_dispatch_id"],)).fetchone()
    wt = Path(disp["worktree"])
    (wt / "calc.py").write_text("# T2 collides with the accepted T1\n")
    env.git("add", "calc.py", cwd=wt)
    env.git("commit", "-qm", "t2 conflicting", cwd=wt)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    con.execute("UPDATE tasks SET status='changes_required', pause_reason='RECHECK: parent moved' WHERE id='T2'")
    con.execute("UPDATE tasks SET depends_json=? WHERE id='T2'", (json.dumps(["T1"]),))
    con.commit()

    code, out = env.office("rerun", "T2", "--fresh", env=external)
    assert code == 0 and "conflicts" in out, out
    new = con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env, "T2")["current_dispatch_id"],)).fetchone()
    packet = next((env.state / "runs").glob(f"*/dispatches/{new['id']}/packet.json"))
    assert json.loads(packet.read_text())["restack"]["conflict"]["task"] == "T1"
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == head
