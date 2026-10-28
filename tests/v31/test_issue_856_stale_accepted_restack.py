"""#856: next must never recommend a rerun that rejects stale accepted dependents."""
from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor

from conftest import GOOD_ADD, GOOD_MUL, approved_run, task_row
from test_upstream_reaccept import (EXTERNAL, PASS, PLAN_STACK, _accepted_commit, _commit_on,
                                    _is_ancestor, _worker, accept_fab)


@pytest.mark.parametrize("review_contract", [
    pytest.param("v3.1", marks=pytest.mark.review_contract("v3.1"), id="v3.1"),
    pytest.param("convergence", id="convergence"),
])
def test_stale_accepted_dependent_can_follow_status_restack_recommendation(env, review_contract):
    # Both tasks genuinely ran and were accepted. A legacy/external acceptance
    # path can change the accepted upstream revision without reopening downstream
    # tasks; the persisted downstream still says accepted when status reads it.
    approved_run(
        env, plan=PLAN_STACK, integration_reviewer=[PASS], code_reviewer=[PASS],
        convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}],
        executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}},
                   "submit": True}],
    )
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert [task_row(env, tid)["status"] for tid in ("T1", "T2")] == ["accepted", "accepted"]

    accepted = task_row(env, "T2")["accepted_revision_id"]
    before_count = env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T2'").fetchone()[0]
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 4 and "task-done" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T2'").fetchone()[0] == before_count

    newer = _commit_on(env, _accepted_commit(env, "T1"),
                       {"calc.py": GOOD_ADD + "# revised upstream\n"}, "t1 revised")
    accept_fab(env, "T1", newer)  # reproduces persisted stale acceptance without the reopen hook
    assert task_row(env, "T2")["status"] == "accepted"
    code, status = env.ojson("status")
    assert code == 0 and "office rerun T2" in status["next"], status

    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and "restacked onto T1" in out, out
    task = task_row(env, "T2")
    assert task["accepted_revision_id"] == accepted, "old acceptance remains evidence"
    assert task["status"] != "accepted"
    _, wt = _worker(env, "T2")
    assert _is_ancestor(env, newer, "HEAD", cwd=wt), "restack merges current upstream"


def test_parallel_stale_reruns_cannot_restack_twice_or_launch_two_workers(env):
    approved_run(env, plan=PLAN_STACK, integration_reviewer=[PASS], code_reviewer=[PASS],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}],
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}},
                            "submit": True}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert task_row(env, "T2")["status"] == "accepted"
    newer = _commit_on(env, _accepted_commit(env, "T1"),
                       {"calc.py": GOOD_ADD + "# revised upstream\n"}, "T1 revised concurrently")
    accept_fab(env, "T1", newer)
    con = env.con()
    try:
        before = con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T2'").fetchone()[0]
    finally:
        con.close()

    def attempt():
        # Independent CLI processes, not threads entering Env.office's global
        # chdir/env/stdout mocks. This exercises actual cross-process flock.
        return env._office_subprocess(("rerun", "T2", "--fresh"), None, EXTERNAL, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    statuses = [code for code, _ in outcomes]
    assert statuses.count(0) == 1, outcomes
    assert statuses.count(4) == 1, outcomes
    assert any("worker-live" in msg or "task-done" in msg for code, msg in outcomes if code == 4), outcomes
    con = env.con()
    try:
        after = con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T2'").fetchone()[0]
        assert after == before + 1, (before, after, outcomes)
    finally:
        con.close()
