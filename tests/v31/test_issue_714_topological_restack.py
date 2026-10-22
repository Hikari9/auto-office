"""An accepted dependency tip resolves its ancestors before a dependent is restacked."""
import subprocess

import pytest

from office import integration, rerun, state
from test_multi_dependency_base import (PLAN_FAN_IN, base_sha, commit_on, fan_in_run,
                                       record_revision, run_id)


PLAN = PLAN_FAN_IN.replace("depends: T1, T2", "depends: T1, T2, T4") + """
### T4: Resolve the dependency boundary
scope: t4.py
shared: registry.txt
depends: T1, T2
checks: none
accept:
- the dependency boundary is resolved
visual: none
"""


def parents(env):
    fan_in_run(env, plan=PLAN)
    c1 = commit_on(env, base_sha(env), {"registry.txt": "resolved\n"}, "first parent")
    c2 = commit_on(env, base_sha(env), {"registry.txt": "other\n"}, "second parent")
    wt = env.tmp / "resolver"
    env.git("worktree", "add", "-q", "--detach", str(wt), c1)
    conflict = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", c2],
                              capture_output=True)
    assert conflict.returncode != 0
    (wt / "registry.txt").write_text("resolved\n")
    (wt / "t4.py").write_text("RESOLVED = True\n")
    env.git("add", ".", cwd=wt)
    env.git("commit", "-qm", "accepted resolution of both parents", cwd=wt)
    tip = env.git("rev-parse", "HEAD", cwd=wt).strip()
    env.git("worktree", "remove", "--force", str(wt))
    for tid, commit in (("T1", c1), ("T2", c2), ("T4", tip)):
        record_revision(env, tid, commit, accepted=True)
    return c1, c2, tip


@pytest.mark.parametrize("order", [(0, 1, 2), (1, 0, 2), (2, 0, 1)])
def test_combine_chooses_tip_before_attempting_ancestor_conflicts(env, order):
    commits = parents(env)
    run = state.get_run(env.con(), run_id(env))
    entries = [(tid, commit) for tid, commit in zip(("T1", "T2", "T4"), commits)]
    assert integration.combine(run, [entries[i] for i in order]) == commits[2]


@pytest.mark.parametrize("already_merged", [False, True])
def test_restack_records_tip_and_does_not_charge_dependency_files(env, already_merged):
    c1, _, tip = parents(env)
    wt = env.tmp / "dependent"
    env.git("worktree", "add", "-q", "--detach", str(wt), tip if already_merged else c1)
    (wt / "sub.py").write_text("def sub(a, b):\n    return a - b\n")
    env.git("add", "sub.py", cwd=wt)
    env.git("commit", "-qm", "only dependent work", cwd=wt)
    con = env.con()
    run = state.get_run(con, run_id(env))
    task = state.get_task(con, run["id"], "T3")
    result = rerun._restack(con, run, task, str(wt))
    assert result is not None and result["conflict"] is None, result
    assert result["base"] == tip
    assert env.git("diff", "--name-only", result["base"], "HEAD", cwd=wt).splitlines() == ["sub.py"]
    assert (wt / "t4.py").read_text() == "RESOLVED = True\n"
