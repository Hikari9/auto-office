"""What a task changed is measured against what it inherited (#490).

A task's worktree holds its parents' work, and a restack, a hand merge or an unrelated accepted revision
adds more. Scope is checked on the difference between the merge of all of that (M) and HEAD, so work the
task did not write is never out of scope, and a real out-of-scope edit still is, M or no M.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, self_reviewed, task_row
from test_multi_dependency_base import (EXTERNAL, PLAN_FAN_IN, accept_on_base, base_sha, commit_on, edit_line, fan_in_run,
                                        record_revision, run_id)


def _worker(env, tid="T3"):
    t = task_row(env, tid)
    d = dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _merge(env, wt, commit):
    env.git("merge", "--no-edit", "-q", commit, cwd=wt)


def after_a_restack(env):
    """T3 depends on T1 and T2, which both edit the shared registry.txt. They are accepted again on newer
    revisions and both are merged into the worktree whose recorded base predates them."""
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(2, "t1 was here")})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(25, "t2 was here")})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    con = env.con()
    con.execute("UPDATE dispatches SET base_commit=? WHERE id=?", (base_sha(env), wenv["OFFICE_DISPATCH_ID"]))
    new1 = commit_on(env, c1, {"calc.py": GOOD_ADD + "# v2\n", "registry.txt": edit_line(2, "t1 v2")}, "t1 v2")
    new2 = commit_on(env, c2, {"mul.py": GOOD_MUL + "# v2\n", "registry.txt": edit_line(25, "t2 v2")}, "t2 v2")
    record_revision(env, "T1", new1, accepted=True)
    record_revision(env, "T2", new2, accepted=True)
    _merge(env, wt, new1)
    _merge(env, wt, new2)
    return wenv, wt, ["registry.txt", "calc.py", "mul.py"]


def with_an_unrelated_accepted_revision(env):
    """T3 depends on T1 only. T2 is accepted and not a dependency; its commit is merged by hand."""
    fan_in_run(env, plan=PLAN_FAN_IN.replace("depends: T1, T2", "depends: T1"))
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    _merge(env, wt, c2)
    return wenv, wt, ["mul.py"]


def where_the_parents_cannot_compose(env):
    """T1 and T2 edit one line of registry.txt differently; T3 merged T2 and resolved the conflict by hand."""
    fan_in_run(env, plan=PLAN_FAN_IN.replace("depends: T1, T2", "depends: T1"))
    accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(3, "from t1")})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(3, "from t2")})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    merge = subprocess.run(["git", "-C", str(wt), "merge", "--no-edit", "-q", c2], capture_output=True)
    assert merge.returncode != 0, "the parents must conflict"
    (wt / "registry.txt").write_text(edit_line(3, "resolved by t3"))
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", "resolve", cwd=wt)
    return wenv, wt, []


SETUPS = [after_a_restack, with_an_unrelated_accepted_revision]


def _submit_in_scope_work(env, wenv, wt):
    (wt / "sub.py").write_text("def sub(a, b):\n    return a - b\n")
    self_reviewed(wt, "sub.py")
    return env.office("submit", cwd=wt, env=wenv)


@pytest.mark.parametrize("setup", SETUPS, ids=lambda f: f.__name__)
def test_work_the_task_inherited_is_not_out_of_scope(env, setup):
    wenv, wt, inherited = setup(env)
    code, out = _submit_in_scope_work(env, wenv, wt)
    assert code == 0 and "outside" not in out, out
    row = env.con().execute("SELECT changed_json, base_commit FROM revisions WHERE task_id='T3'").fetchone()
    assert row is not None, "the revision was captured"
    t3 = task_row(env, "T3")
    assert t3["status"] != "blocked", t3


@pytest.mark.parametrize("setup", SETUPS, ids=lambda f: f.__name__)
def test_preflight_measures_scope_the_same_way(env, setup):
    wenv, wt, inherited = setup(env)
    (wt / "sub.py").write_text("def sub(a, b):\n    return a - b\n")
    self_reviewed(wt, "sub.py")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert "scope:" not in out, out


@pytest.mark.parametrize("setup", SETUPS, ids=lambda f: f.__name__)
def test_a_genuine_out_of_scope_edit_is_still_refused(env, setup):
    wenv, wt, inherited = setup(env)
    (wt / "sub.py").write_text("def sub(a, b):\n    return a - b\n")
    self_reviewed(wt, "sub.py")
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert "scope: tracked edits outside SCOPE: README.md" in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope: README.md" in out, out
    for name in inherited:
        assert name not in out.split("outside its scope:")[1].splitlines()[0], (name, out)
    assert task_row(env, "T3")["status"] == "blocked"
    assert not env.con().execute("SELECT 1 FROM revisions WHERE task_id='T3'").fetchone()


def test_when_the_parents_cannot_compose_scope_falls_back_to_the_conservative_rule(env):
    from office import state, submit
    wenv, wt, _ = where_the_parents_cannot_compose(env)
    con = env.con()
    run = state.get_run(con, run_id(env))
    d = state.get_dispatch(con, wenv["OFFICE_DISPATCH_ID"])
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    assert submit.inherited_base(con, run, state.get_task(con, run["id"], "T3"), d, head) is None
    # Conservative: the hand-resolved registry.txt and the merged mul.py are counted as T3's own.
    (wt / "sub.py").write_text("def sub(a, b):\n    return a - b\n")
    self_reviewed(wt, "sub.py")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out and "registry.txt" in out, out
    # A genuine out-of-scope edit is refused in the fallback too.
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "README.md" in out.split("outside its scope:")[1].splitlines()[0], out
