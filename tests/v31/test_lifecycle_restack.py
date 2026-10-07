"""Lifecycle around restacks: a dependent's lease never blocks its prerequisite (#398 item 2), and a
revoke of a refused worker on an accepted task keeps the task accepted (#307).

Runs pin the v3.1 review contract: per-task code review decides acceptance here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_TWO, approved_run, task_row

pytestmark = pytest.mark.review_contract("v3.1")

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PASS = {"reply": "VERDICT: PASS"}
CHANGES = {"reply": "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | needs a comment | add one"}
# T2 edits the file T1 owns: the plan allows it because T2 depends on T1.
PLAN_ORDERED = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: calc.py\ndepends: T1")
PLAN_ORDERED = PLAN_ORDERED.replace('checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"',
                                    'checks: python3 -c "import calc; assert calc.add(2, 3) == 5"')
DELTA = "also reject non-numeric arguments with a TypeError"


def _worker(env, tid):
    t = task_row(env, tid)
    d = dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _work(env, tid, text):
    wenv, wt = _worker(env, tid)
    (wt / "calc.py").write_text(text)
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", f"{tid} work", cwd=wt)
    return env.office("submit", cwd=wt, env=wenv)


def _end(env, tid):
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?",
                (task_row(env, tid)["current_dispatch_id"],))
    con.commit()


def _live_leases(env):
    return {r[0] for r in env.con().execute("SELECT task_id FROM leases WHERE released_at IS NULL AND revoked_at IS NULL")}


def test_a_dependents_live_lease_never_blocks_amending_or_rerunning_its_prerequisite(env):
    """T1 accepted, T2 (depends T1, same file) running; amending T1 used to die with scope-held."""
    approved_run(env, plan=PLAN_ORDERED, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", GOOD_ADD)
    _end(env, "T1")  # the accepted worker has exited
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    t2_first = task_row(env, "T2")["current_dispatch_id"]
    assert "T2" in _live_leases(env)
    t1_first = task_row(env, "T1")["accepted_revision_id"]

    code, out = env.office("amend", "T1", "--", DELTA, env=EXTERNAL)
    assert code == 0 and "scope-held" not in out, out
    assert task_row(env, "T1")["status"] == "running"
    assert {"T1", "T2"} <= _live_leases(env), "both lanes hold their lease"

    # The prerequisite's new revision is accepted; T2 is the one that is stale, and says so.
    wenv, _ = _worker(env, "T1")
    assert env.office("ack", "A1", env=wenv)[0] == 0
    assert _work(env, "T1", GOOD_ADD + "# amended\n")[0] == 0
    t1 = task_row(env, "T1")
    assert t1["status"] == "accepted" and t1["accepted_revision_id"] != t1_first, t1
    assert _work(env, "T2", "# t2\n" + GOOD_ADD)[0] == 0
    code, out = env.office("status")
    assert f"T2 waiting: built on T1 {t1_first}, but T1 was accepted on {t1['accepted_revision_id']}" in out, out

    # A rerun of the dependent restacks it onto the prerequisite instead of deadlocking.
    _end(env, "T2")
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and f"T2 restacked onto T1 {t1['accepted_revision_id']}" in out, out
    assert task_row(env, "T2")["current_dispatch_id"] != t2_first


def test_rerunning_the_prerequisite_is_not_blocked_by_the_dependents_lease_either(env):
    approved_run(env, plan=PLAN_ORDERED, executor=[{}], code_reviewer=[CHANGES, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", GOOD_ADD)
    assert task_row(env, "T1")["status"] == "changes_required"
    _end(env, "T1")
    assert env.office("revoke", "T1")[0] == 0  # T1 is paused with its lease gone, as after a reaped worker
    env.office("dispatch", "T2", env=EXTERNAL, check=0)  # stacked on T1's first revision; T2 holds a live lease
    assert "T2" in _live_leases(env)
    first = task_row(env, "T1")["current_dispatch_id"]
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and "scope-held" not in out, out
    assert task_row(env, "T1")["current_dispatch_id"] != first
    assert {"T1", "T2"} <= _live_leases(env)


def test_unordered_tasks_with_overlapping_scopes_still_exclude_each_other(env):
    """The exemption is for tasks ordered by depends; a lease on an unrelated overlapping scope still holds."""
    from office import dispatch as dispatch_mod
    approved_run(env, plan=PLAN_TWO, executor=[{}], code_reviewer=[PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    con.execute("UPDATE tasks SET scope_json='[\"calc.py\"]' WHERE id='T2'")
    con.commit()
    from office import state
    with pytest.raises(state.Refused) as e:
        with con:
            dispatch_mod.acquire_lease(con, state.get_run(con, run["id"]), state.get_task(con, run["id"], "T2"), "Dx", "executor")
    assert e.value.category == "scope-held"


def test_revoking_a_refused_relaunch_of_an_accepted_task_keeps_it_accepted(env):
    """#307: T3 accepted, an amendment relaunched a worker whose submit was refused; revoke released
    the lease but turned the task from accepted to paused."""
    approved_run(env, executor=[{}], code_reviewer=[PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", GOOD_ADD)
    accepted = task_row(env)["accepted_revision_id"]
    assert accepted and task_row(env)["status"] == "accepted"
    _end(env, "T1")
    assert env.office("amend", "T1", "--", DELTA, env=EXTERNAL)[0] == 0
    reopened = task_row(env)
    assert reopened["status"] == "running" and "T1" in _live_leases(env)
    # The relaunched worker's submit is refused: it edits outside T1's scope.
    wenv, wt = _worker(env, "T1")
    (wt / "other.py").write_text("X = 1\n")
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", "wrong file", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "outside" in out, out

    code, out = env.office("revoke", "T1")
    assert code == 0 and "stays accepted" in out, out
    assert "A1 was not applied to T1" in out, out  # the amendment the refused worker never got in is not lost silently
    row = task_row(env)
    assert row["status"] == "accepted" and row["accepted_revision_id"] == accepted, row
    assert "T1" not in _live_leases(env)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM leases WHERE task_id='T1' AND revoked_at IS NOT NULL AND revoke_reason<>'superseded'").fetchone()[0] == 0
    # The revoked session is fenced out for good.
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "lease" in out.lower(), out


def test_revoking_a_task_that_never_had_an_accepted_revision_still_pauses_it(env):
    approved_run(env, executor=[{}], code_reviewer=[PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    code, out = env.office("revoke", "T1")
    assert code == 0 and "lease revoked" in out, out
    assert task_row(env)["status"] == "paused"


def test_preflight_accepts_the_fix_round_of_a_rerun_that_only_restacked(env):
    """#398 item 5, end to end: T2 built on a superseded T1 revision, rerun restacks it, and the new
    session's preflight is ready without an `office amend`."""
    from test_self_review_ledger import write_ledger
    from test_stacked_restack import PLAN_STACK, _commit, _t2_on_superseded_t1

    t1_r1, t1_acc, w2, wt2 = _t2_on_superseded_t1(env)
    _commit(env, wt2, {"mul.py": "def mul(a, b):\n    return a * b\n"}, "t2")
    env.office("submit", cwd=wt2, env=w2, check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?", (w2["OFFICE_DISPATCH_ID"],))
    con.commit()
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and f"T2 restacked onto T1 {t1_acc}" in out, out
    w2b, _ = _worker(env, "T2")
    write_ledger(wt2)
    code, out = env.office("preflight", cwd=wt2, env=w2b)
    assert code == 0 and "PREFLIGHT ready" in out and f"restack: T1 {t1_acc}" in out, out
    assert "no open findings" not in out, out


def test_a_prerequisites_live_lease_still_blocks_starting_its_overlapping_dependent(env):
    """The exemption is one-way: only a dependent's lease is ignored when the prerequisite needs its own."""
    approved_run(env, plan=PLAN_ORDERED, executor=[{}], code_reviewer=[CHANGES])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", GOOD_ADD)
    assert task_row(env, "T1")["status"] == "changes_required" and "T1" in _live_leases(env)
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 4 and "scope-held" in out, out


def test_revoking_a_cancelled_task_does_not_make_it_accepted_again(env):
    approved_run(env, executor=[{}], code_reviewer=[PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", GOOD_ADD)
    con = env.con()
    con.execute("UPDATE tasks SET status='cancelled', pause_reason='removed in plan p2' WHERE id='T1'")
    con.commit()
    assert env.office("revoke", "T1")[0] == 0
    assert task_row(env)["status"] != "accepted"
