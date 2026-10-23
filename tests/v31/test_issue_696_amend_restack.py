"""#696: amendment relaunch and dispatch must restack an existing task worktree.

Real Git ancestry is inspected after each launch; a diagram/dispatch base alone is
not evidence that an old branch contains the newly accepted dependency.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, task_row
from test_convergence_contract import APPROVED, _start

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
DEPENDENT = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1")


def _accepted_independent(env):
    work = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}
    _start(env, plan=PLAN_TWO, executor=[{"write_by_task": work, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    code, out = env.office("dispatch", "T1", "T2", "--parallel", check=0)
    assert code == 0, out
    assert task_row(env, "T1")["status"] == task_row(env, "T2")["status"] == "accepted"
    con = env.con()
    rev = task_row(env, "T1")["accepted_revision_id"]
    sha = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev,)).fetchone()[0]
    return sha


def _wt(env, tid):
    con = env.con()
    task = task_row(env, tid)
    disp = con.execute("SELECT * FROM dispatches WHERE id=?", (task["current_dispatch_id"],)).fetchone()
    return Path(disp["worktree"]), disp


def test_amend_depends_relaunch_contains_accepted_off_base_parent(env):
    parent = _accepted_independent(env)
    wt, prior = _wt(env, "T2")
    assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", parent, "HEAD"]).returncode != 0
    env.write_plan(DEPENDENT)
    code, out = env.office("amend", "T2", "--contract", "--",
                           "T2 now builds on the accepted T1", env=EXTERNAL)
    assert code == 0, out
    fresh_wt, disp = _wt(env, "T2")
    assert disp["id"] != prior["id"], "contract amendment must relaunch its executor"
    assert fresh_wt == wt
    env.git("merge-base", "--is-ancestor", parent, "HEAD", cwd=wt)
    assert disp["base_commit"] == parent
    assert "Office merged T1" in next((env.state / "runs").glob(f"*/dispatches/{disp['id']}/brief.md")).read_text()


def test_dirty_amend_restack_refuses_without_lose_and_office_rerun_stashes_wip(env):
    parent = _accepted_independent(env)
    wt, before = _wt(env, "T2")
    (wt / "mul.py").write_text(GOOD_MUL + "\n# uncommitted executor WIP\n")
    original_head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    env.write_plan(DEPENDENT)
    code, _ = env.office("amend", "T2", "--contract", "--", "depend on accepted T1", env=EXTERNAL)
    assert code == 0
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == original_head
    assert "# uncommitted executor WIP" in (wt / "mul.py").read_text()
    code, out = env.office("revoke", "T2", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code != 0 and "restack-dirty-worktree" in out and "--stash-wip" in out, out
    code, out = env.office("rerun", "T2", "--fresh", "--stash-wip", env=EXTERNAL)
    assert code == 0, out
    env.git("merge-base", "--is-ancestor", parent, "HEAD", cwd=wt)
    assert "# uncommitted executor WIP" in (wt / "mul.py").read_text()
    assert "mul.py" in env.git("status", "--porcelain", cwd=wt)
    assert not env.git("stash", "list", cwd=wt).strip(), "a successfully restored temporary stash is dropped"


def test_contract_amend_relaunch_restacks_after_parent_was_reaccepted(env):
    """Issue comment: the dependency list is unchanged, but the parent tip moved."""
    from test_multi_dependency_base import record_revision

    work = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}
    _start(env, plan=DEPENDENT, executor=[{"write_by_task": work, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert task_row(env, "T1")["status"] == task_row(env, "T2")["status"] == "accepted"
    wt1, _ = _wt(env, "T1")
    wt2, old = _wt(env, "T2")

    (wt1 / "calc.py").write_text(GOOD_ADD + "\n# newly accepted parent\n")
    env.git("add", "calc.py", cwd=wt1)
    env.git("commit", "-qm", "parent revision reaccepted", cwd=wt1)
    new_parent = env.git("rev-parse", "HEAD", cwd=wt1).strip()
    record_revision(env, "T1", new_parent, accepted=True)
    assert subprocess.run(["git", "-C", str(wt2), "merge-base", "--is-ancestor",
                           new_parent, "HEAD"]).returncode != 0

    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required', pause_reason='RECHECK: parent moved' WHERE id='T2'")
    con.commit()
    code, out = env.office("amend", "T2", "--contract", "--add-accept",
                           "mul still works after the new T1", "--", "new acceptance following recheck",
                           env=EXTERNAL)
    assert code == 0, out
    _, new = _wt(env, "T2")
    assert new["id"] != old["id"]
    env.git("merge-base", "--is-ancestor", new_parent, "HEAD", cwd=wt2)
    assert new["base_commit"] == new_parent


def test_dispatch_again_on_an_off_base_branch_restacks_before_worker(env):
    """A later direct dispatch cannot reuse a branch that lacks its newly declared parent."""
    import json

    parent = _accepted_independent(env)
    wt, old = _wt(env, "T2")
    con = env.con()
    con.execute("UPDATE tasks SET status='planned', depends_json=? WHERE id='T2'", (json.dumps(["T1"]),))
    con.commit()
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out
    _, new = _wt(env, "T2")
    assert new["id"] != old["id"]
    assert new["base_commit"] == parent
    env.git("merge-base", "--is-ancestor", parent, "HEAD", cwd=wt)
