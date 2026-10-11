"""A task with several dependencies starts from one base holding every accepted parent (#489).

The base used to be overwritten per dependency, so the last one won and the others were missing from
the worktree. It is now every dependency's accepted revision combined; parents that conflict refuse
(and pause a stacked dependent), and a parent that is only submitted is not a base.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
REGISTRY = "".join(f"line {i}\n" for i in range(1, 31))

PLAN_FAN_IN = """# Plan

## Requirements
done:
- add, mul and sub exist
blast_radius: repo

## Tasks
### T1: Implement add
scope: calc.py
shared: registry.txt
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none

### T2: Implement mul
scope: mul.py
shared: registry.txt
depends: none
checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"
accept:
- mul.mul(2, 3) == 6
visual: none

### T3: Implement sub
scope: sub.py
depends: T1, T2
checks: python3 -c "import sub; assert sub.sub(3, 2) == 1"
accept:
- sub.sub(3, 2) == 1
visual: none
"""


def fan_in_run(env, plan=PLAN_FAN_IN, **script):
    """A run whose repo already holds registry.txt: two parents may edit it in different places."""
    (env.repo / "registry.txt").write_text(REGISTRY)
    env.git("add", "registry.txt")
    env.git("commit", "-qm", "registry")
    approved_run(env, plan=plan, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}], **script)


def run_id(env) -> str:
    return env.con().execute("SELECT id FROM runs").fetchone()[0]


def base_sha(env) -> str:
    return env.con().execute("SELECT base_sha FROM runs").fetchone()[0]


def commit_on(env, base: str, files: dict, msg: str = "work") -> str:
    """A commit of `files` on top of `base`, made in a scratch worktree so no checkout moves."""
    scratch = env.tmp / f"scratch-{uuid.uuid4().hex[:6]}"
    env.git("worktree", "add", "-q", "--detach", str(scratch), base)
    try:
        for name, text in files.items():
            (scratch / name).write_text(text)
        env.git("add", "-A", cwd=scratch)
        env.git("commit", "-qm", msg, cwd=scratch)
        return env.git("rev-parse", "HEAD", cwd=scratch).strip()
    finally:
        env.git("worktree", "remove", "--force", str(scratch))


def record_revision(env, tid: str, commit: str, *, accepted: bool) -> str:
    """A revision of `tid` at `commit`, current and, with `accepted`, accepted too."""
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    rev_id = f"Rfab-{tid}-{commit[:7]}"
    seq = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] + 1
    tree = env.git("rev-parse", f"{commit}^{{tree}}").strip()
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, commit_sha, tree_sha, base_commit, requirements_version, "
                "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rev_id, run["id"], tid, seq, commit, tree, run["base_sha"], run["requirements_version"],
                 run["plan_version"], 0, "fab", uuid.uuid4().hex, "current", "2026-01-01T00:00:00+00:00"))
    con.execute("UPDATE tasks SET status=?, current_revision_id=?, accepted_revision_id=? WHERE run_id=? AND id=?",
                ("accepted" if accepted else "submitted", rev_id, rev_id if accepted else None, run["id"], tid))
    con.commit()
    return rev_id


def accept_on_base(env, tid: str, files: dict) -> str:
    """Accept `tid` on a revision that adds `files` to the run base. Returns the commit."""
    commit = commit_on(env, base_sha(env), files, f"{tid} accepted")
    record_revision(env, tid, commit, accepted=True)
    return commit


def is_ancestor(env, ancestor: str, descendant: str, cwd=None) -> bool:
    return subprocess.run(["git", "-C", str(cwd or env.repo), "merge-base", "--is-ancestor", ancestor, descendant],
                          capture_output=True).returncode == 0


def edit_line(n: int, text: str) -> str:
    lines = REGISTRY.splitlines(keepends=True)
    lines[n - 1] = f"{text}\n"
    return "".join(lines)


def dispatch_base(env, tid: str) -> str:
    return env.con().execute("SELECT base_commit FROM dispatches WHERE id=?",
                             (task_row(env, tid)["current_dispatch_id"],)).fetchone()[0]


def test_a_clean_multi_parent_base_contains_every_parent(env):
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(2, "t1 was here")})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(25, "t2 was here")})
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0, out
    base = dispatch_base(env, "T3")
    assert is_ancestor(env, c1, base) and is_ancestor(env, c2, base), "both parents are in the base"
    wt = Path(env.con().execute("SELECT worktree FROM dispatches WHERE id=?",
                                (task_row(env, "T3")["current_dispatch_id"],)).fetchone()[0])
    merged = (wt / "registry.txt").read_text()
    assert "t1 was here" in merged and "t2 was here" in merged and (wt / "calc.py").read_text() == GOOD_ADD
    brief = next((env.state / "runs").glob(f"*/dispatches/{task_row(env, 'T3')['current_dispatch_id']}/brief.md")).read_text()
    assert "BUILDS ON T1, T2 (already in this worktree's base)" in brief, brief


def test_a_parent_that_descends_from_another_is_not_merged_again(env):
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    c2 = commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2 on t1")
    record_revision(env, "T2", c2, accepted=True)
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0, out
    assert dispatch_base(env, "T3") == c2  # the head that holds the other: no synthetic merge


def test_conflicting_parents_refuse_naming_both_tasks_and_paths(env):
    fan_in_run(env)
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(3, "from t1")})
    accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(3, "from t2")})
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code != 0 and "dependency-conflict" in out, out
    assert "T1" in out and "T2" in out and "registry.txt" in out, out
    assert task_row(env, "T3")["status"] == "planned" and task_row(env, "T3")["current_dispatch_id"] is None


def test_conflicting_parents_pause_a_stacked_dependent_instead_of_raising_out_of_acceptance(env):
    from office import db, dispatch, state
    fan_in_run(env)
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(3, "from t1")})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(3, "from t2")})
    con = env.con()
    con.execute("UPDATE tasks SET status='queued', stack_after='T2', pause_reason='stacked after T2' WHERE id='T3'")
    con.commit()
    run = state.get_run(con, run_id(env))
    with db.transaction(con):
        started = dispatch.start_stacked(con, run, "T2")  # acceptance of T2 calls this inside its transaction
    assert started == []
    t3 = task_row(env, "T3")
    assert t3["status"] == "paused" and "T1" in t3["pause_reason"] and "T2" in t3["pause_reason"], t3
    assert "registry.txt" in t3["pause_reason"], t3
    assert con.execute("SELECT 1 FROM events WHERE kind='task.paused' AND task_id='T3'").fetchone()
    assert not con.execute("SELECT 1 FROM dispatches WHERE task_id='T3'").fetchone()
    assert c2  # T2 stays accepted: pausing the dependent never reopens a parent
    assert task_row(env, "T2")["status"] == "accepted"


def test_a_submitted_parent_is_not_the_base_of_a_normal_dependency(env):
    # #489: the old base took the dependency's current revision whether or not it was accepted.
    fan_in_run(env)
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    c2 = commit_on(env, base_sha(env), {"mul.py": GOOD_MUL}, "t2 submitted")
    record_revision(env, "T2", c2, accepted=False)
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code != 0 and "dependency-not-ready" in out and "T2" in out and "not accepted" in out, out
    assert task_row(env, "T3")["current_dispatch_id"] is None
    assert not env.con().execute("SELECT 1 FROM dispatches WHERE task_id='T3'").fetchone()


def test_the_stacked_holder_released_while_submitted_is_the_unaccepted_exception_and_the_brief_says_so(env):
    from office import dispatch, state
    fan_in_run(env, plan=PLAN_FAN_IN.replace("depends: T1, T2", "depends: T1"))
    c1 = commit_on(env, base_sha(env), {"calc.py": GOOD_ADD}, "t1 submitted")
    record_revision(env, "T1", c1, accepted=False)
    con = env.con()
    run = state.get_run(con, run_id(env))
    t3 = state.get_task(con, run["id"], "T3")
    parts: list = []
    assert dispatch._base_for(con, run, t3, {}, "T1", parents=parts) == c1
    assert [(p["task"], p["accepted"]) for p in parts] == [("T1", False)]
    from office import briefs
    named = briefs._builds_on(con, run, {"base_commit": c1, "depends": ["T1"], "base_parents": parts})
    assert named == ["T1 (unaccepted: submitted Rfab-T1-" + c1[:7] + ", not yet reviewed)"], named


def test_the_brief_names_only_parents_the_base_contains(env):
    from office import briefs, state
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL})
    con = env.con()
    run = state.get_run(con, run_id(env))
    parents = [{"task": "T1", "revision": "R1", "commit": c1, "accepted": True},
               {"task": "T2", "revision": "R2", "commit": c2, "accepted": True}]
    assert briefs._builds_on(con, run, {"base_commit": c1, "depends": ["T1", "T2"], "base_parents": parents}) == ["T1"]
    # No recorded parents (a rerun, an older packet): the declared dependencies, measured the same way.
    assert briefs._builds_on(con, run, {"base_commit": c2, "depends": ["T1", "T2"]}) == ["T2"]
    assert briefs._builds_on(con, run, {"base_commit": base_sha(env), "depends": ["T1", "T2"], "base_parents": []}) == []


def test_a_dependency_with_no_revision_at_all_still_refuses(env):
    fan_in_run(env)
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code != 0 and "no submitted revision" in out and "T2" in out, out


def test_a_rerun_records_the_combined_base_of_all_accepted_dependencies(env):
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    first = task_row(env, "T3")["current_dispatch_id"]
    # Both parents are accepted again on newer revisions, T3's session has ended.
    new1 = commit_on(env, c1, {"calc.py": GOOD_ADD + "# v2\n", "registry.txt": edit_line(2, "t1 v2")}, "t1 v2")
    new2 = commit_on(env, c2, {"mul.py": GOOD_MUL + "# v2\n", "registry.txt": edit_line(25, "t2 v2")}, "t2 v2")
    record_revision(env, "T1", new1, accepted=True)
    record_revision(env, "T2", new2, accepted=True)
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?", (first,))
    con.commit()
    code, out = env.office("rerun", "T3", "--fresh", env=EXTERNAL)
    assert code == 0 and "restacked onto T1" in out and "T2" in out, out
    second = task_row(env, "T3")["current_dispatch_id"]
    assert second != first
    base = dispatch_base(env, "T3")
    assert is_ancestor(env, new1, base) and is_ancestor(env, new2, base), "the recorded base holds both new parents"
    wt = Path(env.con().execute("SELECT worktree FROM dispatches WHERE id=?", (second,)).fetchone()[0])
    assert is_ancestor(env, new1, "HEAD", wt) and is_ancestor(env, new2, "HEAD", wt)


def test_a_reopened_dependency_is_reported_not_skipped_silently(env):
    from office import rerun, state
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    accept_on_base(env, "T2", {"mul.py": GOOD_MUL})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    dispatch_row = env.con().execute("SELECT worktree FROM dispatches WHERE id=?",
                                     (task_row(env, "T3")["current_dispatch_id"],)).fetchone()
    new1 = commit_on(env, c1, {"calc.py": GOOD_ADD + "# v2\n"}, "t1 v2")
    record_revision(env, "T1", new1, accepted=True)
    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T2'")  # T2 was reopened
    con.commit()
    run = state.get_run(con, run_id(env))
    restack = rerun._restack(con, run, state.get_task(con, run["id"], "T3"), dispatch_row[0])
    assert [m["task"] for m in restack["merged"]] == ["T1"]
    assert [(r["task"], r["status"]) for r in restack["reopened"]] == [("T2", "changes_required")]
    assert "T2 is changes_required and was not restacked" in restack["line"], restack
    assert restack["base"] == new1  # only the accepted dependency is in the combined base
    # Nothing left to merge, only the reopened parent: still reported.
    again = rerun._restack(con, run, state.get_task(con, run["id"], "T3"), dispatch_row[0])
    assert again["merged"] == [] and again["reopened"][0]["task"] == "T2", again
    json.dumps(again)  # it travels in the packet


def test_ensure_worktree_reports_a_base_the_existing_branch_lacks(env):
    from office import dispatch, state
    from office.state import Refused
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    env.git("branch", "office/test/T3", base_sha(env))
    run = state.get_run(env.con(), run_id(env))
    spec = {"id": "Dx", "task_id": "T3", "branch": "office/test/T3", "base_commit": c1,
            "worktree": str(env.tmp / "wt-mismatch")}
    with pytest.raises(Refused) as err:
        dispatch.ensure_worktree(run, spec)
    assert err.value.category == "base-mismatch" and "office/test/T3" in err.value.message, err.value
    assert not (env.tmp / "wt-mismatch").exists()
    ok = dispatch.ensure_worktree(run, {**spec, "base_commit": base_sha(env), "worktree": str(env.tmp / "wt-ok")})
    assert (ok / ".git").exists()


def test_a_task_queued_behind_its_holder_is_not_refused_for_an_unaccepted_parent(env):
    # The base of a queued task is worked out when it starts; only a parent with no revision at all refuses now.
    fan_in_run(env)
    c1 = commit_on(env, base_sha(env), {"calc.py": GOOD_ADD}, "t1 submitted")
    record_revision(env, "T1", c1, accepted=False)
    code, out = env.office("dispatch", "T2", "T3", env=EXTERNAL)
    assert code == 0 and "T3 stacked after T2" in out, out
    assert task_row(env, "T3")["status"] == "queued" and task_row(env, "T3")["stack_after"] == "T2"


@pytest.fixture
def old_git(monkeypatch):
    """git without `merge-tree --write-tree` (before 2.38): it exits 129 on the option."""
    from office import integration
    real = subprocess.run

    def run(cmd, *a, **kw):
        if "merge-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 129, "", "usage: git merge-tree")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(integration.subprocess, "run", run)


def test_combine_merges_in_a_scratch_worktree_when_git_lacks_merge_tree_write_tree(env, old_git):
    from office import integration, state
    fan_in_run(env)
    c1 = commit_on(env, base_sha(env), {"calc.py": GOOD_ADD, "registry.txt": edit_line(2, "t1")})
    c2 = commit_on(env, base_sha(env), {"mul.py": GOOD_MUL, "registry.txt": edit_line(25, "t2")})
    c3 = commit_on(env, base_sha(env), {"registry.txt": edit_line(2, "t3")})
    run = state.get_run(env.con(), run_id(env))
    merged = integration.combine(run, [("T1", c1), ("T2", c2)])
    assert is_ancestor(env, c1, merged) and is_ancestor(env, c2, merged)
    with pytest.raises(integration.CombineConflict) as err:
        integration.combine(run, [("T1", c1), ("T2", c2), ("T3", c3)])
    assert (err.value.left, err.value.right, err.value.paths) == ("T1", "T3", ["registry.txt"])
    assert env.git("worktree", "list").count("office-combine") == 0, "the scratch worktree is removed"
