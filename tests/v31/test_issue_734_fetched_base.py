"""A fetched default branch, rather than a stale checkout, owns the task base (#734)."""
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, self_reviewed, task_row


def fetched_default(env):
    old = env.git("rev-parse", "HEAD").strip()
    remote = env.tmp / "upstream.git"
    env.git("clone", "--bare", str(env.repo), str(remote))
    env.git("remote", "add", "origin", str(remote))
    upstream = env.tmp / "upstream-writer"
    env.git("worktree", "add", "-qb", "upstream-writer", str(upstream), old)
    (upstream / ".env.example").write_text("UPSTREAM_ONLY=1\n")
    env.git("add", ".env.example", cwd=upstream)
    env.git("commit", "-qm", "already merged upstream", cwd=upstream)
    new = env.git("rev-parse", "HEAD", cwd=upstream).strip()
    env.git("push", "origin", "HEAD:main", cwd=upstream)
    env.git("fetch", "origin")
    env.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    env.git("checkout", "--detach", old)
    return old, new


def test_detached_stale_checkout_dispatches_from_fetched_default(env):
    old, new = fetched_default(env)
    env.trust()
    env.script(executor=[{}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline", check=0)
    run = dict(env.con().execute("SELECT * FROM runs").fetchone())
    assert run["base_sha"] == new
    assert env.git("rev-parse", "HEAD").strip() == old, "starting must not move the caller's checkout"
    env.write_plan(PLAN_ONE)
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "implement this plan", check=0)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    task = task_row(env, "T1")
    dispatch = dict(env.con().execute("SELECT * FROM dispatches WHERE id=?",
                                     (task["current_dispatch_id"],)).fetchone())
    wt = Path(dispatch["worktree"])
    assert dispatch["base_commit"] == new
    assert (wt / ".env.example").read_text() == "UPSTREAM_ONLY=1\n"
    (wt / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt, "calc.py")
    worker = {"OFFICE_RUN_ID": run["id"], "OFFICE_DISPATCH_ID": dispatch["id"],
              "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor", "OFFICE_WORKER_LAUNCHER": "external"}
    env.office("submit", cwd=wt, env=worker, check=0)
    rev = env.con().execute("SELECT changed_json FROM revisions WHERE id=?",
                            (task_row(env, "T1")["current_revision_id"],)).fetchone()
    assert ".env.example" not in rev[0], "upstream content is outside the task's diff"


def test_explicit_stale_base_refuses_with_recovery_before_creating_run(env):
    old, new = fetched_default(env)
    code, out = env.office("start", "fixture goal", "--base", old, "--planner", "inline")
    assert code != 0 and "stale-run-base" in out, out
    assert "--base origin/main" in out
    assert new[:12] in out
    assert not env.con().execute("SELECT id FROM runs").fetchone()
