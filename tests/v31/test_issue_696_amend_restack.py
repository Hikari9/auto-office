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


# ---- _stash_wip: Office-owned WIP preservation around a restack -------------------------------

def _repo_with_worktrees(tmp_path):
    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "a.py").write_text("one\n")
    git(main, "add", "a.py")
    git(main, "commit", "-qm", "init")
    a, b = tmp_path / "wt-a", tmp_path / "wt-b"
    git(main, "worktree", "add", "-q", "-b", "task-a", str(a))
    git(main, "worktree", "add", "-q", "-b", "task-b", str(b))
    return git, a, b


def test_stash_wip_restores_tracked_and_untracked_wip_and_drops_its_stash(tmp_path):
    from office import rerun

    git, a, _ = _repo_with_worktrees(tmp_path)
    (a / "a.py").write_text("one\nwip\n")
    (a / "new.py").write_text("untracked\n")
    with rerun._stash_wip(a, "T1"):
        assert not git(a, "status", "--porcelain")
    assert (a / "a.py").read_text() == "one\nwip\n"
    assert (a / "new.py").read_text() == "untracked\n"
    assert not git(a, "stash", "list")
    assert not git(a, "for-each-ref", "refs/office/wip")


def test_stash_wip_drop_leaves_other_worktrees_stashes(tmp_path):
    """refs/stash is shared: a stash another worktree pushed meanwhile is neither applied nor dropped."""
    from office import rerun

    git, a, b = _repo_with_worktrees(tmp_path)
    (b / "a.py").write_text("b-before\n")
    git(b, "stash", "push", "-m", "b-before")
    (a / "a.py").write_text("one\nwip\n")
    with rerun._stash_wip(a, "T1"):
        (b / "a.py").write_text("b-during\n")
        git(b, "stash", "push", "-m", "b-during")
    assert (a / "a.py").read_text() == "one\nwip\n"
    remaining = git(a, "stash", "list")
    assert "b-before" in remaining and "b-during" in remaining and "preserve T1 WIP" not in remaining


def test_stash_wip_never_uses_the_shared_refs_stash_so_a_foreign_entry_cannot_be_dropped(tmp_path):
    """696-1: Office held WIP in refs/stash and dropped it by stash@{n}; a foreign entry pushed
    between lookup and drop shifted the index. WIP now lives in an Office ref, refs/stash is untouched."""
    from office import rerun

    git, a, b = _repo_with_worktrees(tmp_path)
    (b / "a.py").write_text("b-before\n")
    git(b, "stash", "push", "-m", "b-before")
    before = git(a, "rev-parse", "refs/stash")
    (a / "a.py").write_text("one\nwip\n")
    (a / "new.py").write_text("untracked\n")
    with rerun._stash_wip(a, "T1"):
        assert git(a, "rev-parse", "refs/stash") == before and "T1" not in git(a, "stash", "list")
        assert git(a, "for-each-ref", "refs/office/wip/")
        (b / "a.py").write_text("b-during\n")
        git(b, "stash", "push", "-m", "b-during")
    assert (a / "a.py").read_text() == "one\nwip\n" and (a / "new.py").read_text() == "untracked\n"
    listing = git(a, "stash", "list")
    assert "b-before" in listing and "b-during" in listing and "T1" not in listing
    assert not git(a, "for-each-ref", "refs/office/wip/")
    git(b, "stash", "pop")
    assert (b / "a.py").read_text() == "b-during\n"


def test_stash_wip_restores_index_state_and_deletions(tmp_path):
    from office import rerun

    git, a, _ = _repo_with_worktrees(tmp_path)
    (a / "b.py").write_text("keep\n")
    git(a, "add", "b.py")
    git(a, "commit", "-qm", "b")
    (a / "a.py").write_text("staged\n")
    git(a, "add", "a.py")
    (a / "b.py").unlink()
    with rerun._stash_wip(a, "T1"):
        assert not git(a, "status", "--porcelain")
    assert git(a, "status", "--porcelain").splitlines() == ["M  a.py", " D b.py"]


def test_stash_wip_restore_conflict_keeps_stash(tmp_path):
    from office import rerun
    from office.state import Refused

    git, a, _ = _repo_with_worktrees(tmp_path)
    (a / "a.py").write_text("wip\n")
    try:
        with rerun._stash_wip(a, "T1"):
            (a / "a.py").write_text("restack change\n")
            git(a, "commit", "-qam", "restack")
    except Refused as exc:
        assert exc.category == "restack-wip-restore-conflict"
    else:
        raise AssertionError("a restore conflict must surface")
    assert "preserve T1 WIP" in git(a, "stash", "list")
    assert not git(a, "for-each-ref", "refs/office/wip/"), "the stash is stored, so the Office ref is dropped"


def test_stash_wip_restore_failure_does_not_hide_restack_error(tmp_path):
    from office import rerun
    from office.state import Refused

    git, a, _ = _repo_with_worktrees(tmp_path)
    (a / "a.py").write_text("wip\n")
    try:
        with rerun._stash_wip(a, "T1"):
            (a / "a.py").write_text("restack change\n")
            git(a, "commit", "-qam", "restack")
            raise Refused("restack-conflict", "restack failed")
    except Refused as exc:
        assert exc.category == "restack-conflict"
        assert "stash" in (exc.preserved or "")
    assert "preserve T1 WIP" in git(a, "stash", "list")


def test_stash_wip_restore_exception_does_not_replace_restack_error(tmp_path, monkeypatch):
    """An OSError from the restore itself is noted on the restack error, which still propagates."""
    from office import rerun
    from office.state import Refused

    git, a, _ = _repo_with_worktrees(tmp_path)
    (a / "a.py").write_text("wip\n")
    real_run = subprocess.run

    def flaky(cmd, *args, **kwargs):
        if "apply" in cmd:
            raise OSError("git vanished")
        return real_run(cmd, *args, **kwargs)

    try:
        with rerun._stash_wip(a, "T1"):
            monkeypatch.setattr(rerun.subprocess, "run", flaky)
            raise Refused("restack-conflict", "restack failed")
    except Refused as exc:
        assert exc.category == "restack-conflict"
        assert any("git vanished" in note for note in exc.__notes__)
    monkeypatch.undo()
    assert "preserve T1 WIP" in git(a, "stash", "list")


def test_stash_wip_rerun_with_conflicting_dependency_launches_with_the_conflict(env):
    """rerun --stash-wip finds the conflict and restores the WIP; the launch job must not restack the dirty tree again."""
    import json

    _accepted_independent(env)
    wt, _ = _wt(env, "T2")
    (wt / "calc.py").write_text("# T2 collides with the accepted T1\n")
    env.git("add", "calc.py", cwd=wt)
    env.git("commit", "-qm", "t2 conflicting", cwd=wt)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    (wt / "mul.py").write_text(GOOD_MUL + "\n# uncommitted executor WIP\n")
    env.write_plan(DEPENDENT)
    code, out = env.office("amend", "T2", "--contract", "--", "depend on accepted T1", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office("revoke", "T2", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office("rerun", "T2", "--fresh", "--stash-wip", env=EXTERNAL)
    assert code == 0 and "conflicts" in out, out
    _, disp = _wt(env, "T2")
    packet = next((env.state / "runs").glob(f"*/dispatches/{disp['id']}/packet.json"))
    assert json.loads(packet.read_text())["restack"]["conflict"]["task"] == "T1"
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == head
    assert "# uncommitted executor WIP" in (wt / "mul.py").read_text()
    assert not env.git("stash", "list", cwd=wt).strip()


def test_relaunch_onto_a_branch_lacking_its_recorded_base_is_refused_without_a_restack(env):
    """No dependency moved, so the restack is a no-op: the base-mismatch refusal still applies to the reused branch."""
    _accepted_independent(env)
    wt, prior = _wt(env, "T2")
    base = env.git("rev-parse", prior["base_commit"] + "^{tree}").strip()
    other = env.git("commit-tree", base, "-p", prior["base_commit"], "-m", "unrelated base").strip()
    con = env.con()
    con.execute("UPDATE dispatches SET base_commit=? WHERE id=?", (other, prior["id"]))
    con.execute("UPDATE tasks SET status='changes_required', pause_reason='RECHECK: base moved' WHERE id='T2'")
    con.commit()
    env.git("worktree", "remove", "--force", str(wt))
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0, out
    _, new = _wt(env, "T2")
    assert new["id"] != prior["id"] and new["base_commit"] == other
    assert not list((env.state / "runs").glob(f"*/dispatches/{new['id']}/packet.json")), out
    assert "does not contain the base" in env.office("status", "--verbose")[1]


def test_restack_cleanup_failure_does_not_replace_the_restack_error(env, monkeypatch):
    """696-2: a scratch-worktree cleanup error in `finally` is noted on the error in flight."""
    import json
    import pytest
    from office import integration, rerun, state

    _accepted_independent(env)
    wt, _ = _wt(env, "T2")
    con = env.con()
    con.execute("UPDATE tasks SET status='planned', depends_json=? WHERE id='T2'", (json.dumps(["T1"]),))
    con.commit()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    task = state.get_task(con, run["id"], "T2")
    real_run = subprocess.run

    def flaky(cmd, *args, **kwargs):
        if cmd[3:5] == ["worktree", "remove"]:
            raise OSError("remove vanished")
        return real_run(cmd, *args, **kwargs)

    def boom(*_args, **_kwargs):
        raise RuntimeError("combine exploded")

    monkeypatch.setattr(integration, "combine", boom)
    monkeypatch.setattr(rerun.subprocess, "run", flaky)
    with pytest.raises(RuntimeError, match="combine exploded") as info:
        rerun._restack(con, run, task, str(wt))
    assert any("remove vanished" in note for note in info.value.__notes__)
    monkeypatch.undo()
    real_run(["git", "-C", str(run["repo_root"]), "worktree", "prune"], check=False)


def test_restack_cleanup_failure_alone_still_raises(tmp_path, monkeypatch):
    from office import rerun

    def flaky(cmd, *args, **kwargs):
        raise OSError("remove vanished")

    monkeypatch.setattr(rerun.subprocess, "run", flaky)
    try:
        rerun._remove_scratch(str(tmp_path), tmp_path / "scratch", None)
    except OSError as exc:
        assert "remove vanished" in str(exc)
    else:
        raise AssertionError("with no error in flight a cleanup failure must surface")
