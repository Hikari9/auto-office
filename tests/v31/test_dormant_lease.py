"""A lease whose task has no live session holds no scope (#398, #307).

Office 3.5.0, convergence RECHECK: a task that had submitted (checks APPROVED) but could not be accepted yet kept
its lease with no worker running, so every overlapping repair was refused `scope-held` and `office dispatch Ty Tx`
only queued Tx behind Ty's acceptance. An accepted task whose ack-only session had ended likewise blocked a plan
submit. Overlapping tasks still never run at the same time: a live session keeps its lease.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run, self_reviewed, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
CHANGES = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | needs a comment | add one"
# T2 builds on T1; T3 shares T2's file and is ordered against neither.
PLAN_HELD = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1") + """
### T3: Document mul
scope: mul.py
depends: none
checks: python3 -c "import mul"
accept:
- mul.py has a docstring
visual: none
"""


def _worker(env, tid):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _end(env, dispatch_id):
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited', "
                "terminal_classification='success' WHERE id=?", (dispatch_id,))
    con.commit()


def _t2_submitted_and_held(env):
    """T2 submits on T1's first revision while T1 is back in changes: T2 cannot be accepted yet."""
    approved_run(env, plan=PLAN_HELD, executor=[{}], code_reviewer=[{"reply": CHANGES}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    (wt1 / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt1, "calc.py")
    env.office("submit", cwd=wt1, env=w1, check=0)
    assert task_row(env, "T1")["status"] == "changes_required"
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    w2, wt2 = _worker(env, "T2")
    (wt2 / "mul.py").write_text(GOOD_MUL)
    self_reviewed(wt2, "mul.py")
    env.office("submit", cwd=wt2, env=w2, check=0)
    t2 = task_row(env, "T2")
    assert t2["status"] == "submitted" and t2["accepted_revision_id"] is None, t2
    return w2


@pytest.mark.review_contract("v3.1")
def test_a_submitted_task_with_no_session_does_not_hold_an_overlapping_scope(env):
    w2 = _t2_submitted_and_held(env)
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 4 and "scope-held" in out, "a live session still holds its scope: " + out
    _end(env, w2["OFFICE_DISPATCH_ID"])
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0 and "T3 ->" in out, out
    # ...and now T3's live session holds mul.py: T2 cannot run beside it.
    code, out = env.office("rerun", "T2", "--resume", env=EXTERNAL)
    assert code == 4 and "scope-held" in out and "T3" in out, out


@pytest.mark.review_contract("v3.1")
def test_status_does_not_call_a_task_with_no_session_live(env):
    w2 = _t2_submitted_and_held(env)
    _end(env, w2["OFFICE_DISPATCH_ID"])
    code, out = env.office("status")
    counts = next(line for line in out.splitlines() if line.startswith("accepted "))
    assert "live T2" not in counts and "submitted T2" in counts, counts


@pytest.mark.review_contract("v3.1")
def test_a_stacked_task_runs_once_its_holder_submitted_and_ended(env):
    approved_run(env, plan=PLAN_HELD, executor=[{}], code_reviewer=[{"reply": CHANGES}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    (wt1 / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt1, "calc.py")
    env.office("submit", cwd=wt1, env=w1, check=0)
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T2", "T3", env=EXTERNAL)
    assert code == 0 and task_row(env, "T3")["status"] == "queued", out
    w2, wt2 = _worker(env, "T2")
    (wt2 / "mul.py").write_text(GOOD_MUL)
    self_reviewed(wt2, "mul.py")
    env.office("submit", cwd=wt2, env=w2, check=0)
    assert task_row(env, "T2")["status"] == "submitted"
    assert task_row(env, "T3")["status"] == "queued", "T2's session is still live"
    _end(env, w2["OFFICE_DISPATCH_ID"])
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0 and task_row(env, "T3")["status"] in ("launching", "running"), out
    assert "already queued" not in out, out


@pytest.mark.review_contract("v3.1")
def test_a_stacked_task_starts_when_its_holder_ends_through_finish(env, monkeypatch):
    """The end a supervisor records (`_finish`), not a hand-written row: it is what starts the stacked task (#508)."""
    from office import dispatch
    approved_run(env, plan=PLAN_HELD, executor=[{}], code_reviewer=[{"reply": CHANGES}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    (wt1 / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt1, "calc.py")
    env.office("submit", cwd=wt1, env=w1, check=0)
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T2", "T3", env=EXTERNAL)
    assert code == 0 and task_row(env, "T3")["status"] == "queued", out
    w2, wt2 = _worker(env, "T2")
    (wt2 / "mul.py").write_text(GOOD_MUL)
    self_reviewed(wt2, "mul.py")
    env.office("submit", cwd=wt2, env=w2, check=0)
    assert task_row(env, "T2")["status"] == "submitted"
    assert task_row(env, "T3")["status"] == "queued", "T2's session is still live"
    monkeypatch.setenv("OFFICE_WORKER_LAUNCHER", "external")  # the stacked launch records a session, no fake agent runs
    dispatch._finish(w2["OFFICE_DISPATCH_ID"], 0, None, "success", 0.0)
    assert task_row(env, "T3")["status"] in ("launching", "running"), task_row(env, "T3")["pause_reason"]
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T3' AND role='executor'").fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM leases WHERE task_id='T2' AND revoked_at IS NOT NULL").fetchone()


def _contract_change_beside_an_ended_ack_session(env, **reviewers):
    approved_run(env, plan=PLAN_TWO, executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}], **reviewers)
    env.office("dispatch", "T1", check=0)
    assert task_row(env, "T1")["status"] == "accepted"
    # The 3.5.0 shape: an amendment relaunched T1 only to acknowledge it, that session ended, and its lease stayed.
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, role, holder_id, triple, started_at, task_id, kind, office_version, "
                "status, ended_at, lease_id) VALUES('Dack', ?, 'executor', 'Dack', 'x', '2026-01-01', 'T1', 'executor', "
                "'3.5.0', 'exited', '2026-01-01', 'Lack')", (run_id,))
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, task_id, fencing, "
                "dispatch_id, renewed_at) VALUES('Lack', ?, 'executor', '[\"calc.py\"]', 'Dack', '2026-01-01', "
                "'2999-01-01', 'T1', 99, 'Dack', '2026-01-01')", (run_id,))
    con.commit()
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    assert task_row(env, "T2")["status"] in ("launching", "running")
    env.write_plan(PLAN_TWO.replace("scope: mul.py", "scope: mul.py, calc.py"))
    code, out = env.office("amend", "T2", "--contract", "--", "T2 also touches calc.py", env=EXTERNAL)
    assert code == 0 and "scope-held" not in out, out


def test_an_accepted_task_with_an_ended_session_does_not_block_a_contract_change(env):
    _contract_change_beside_an_ended_ack_session(env)


@pytest.mark.review_contract("convergence-v1")
def test_convergence_an_accepted_task_with_an_ended_session_does_not_block_a_contract_change(env):
    _contract_change_beside_an_ended_ack_session(env, convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    from office import contract, state
    assert contract.is_convergence(state.get_run(env.con(), env.con().execute("SELECT id FROM runs").fetchone()[0]))


def test_an_ack_only_session_that_ends_leaves_the_task_resumable(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"ack": "A1", "exit": 0}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    env.office("amend", "T1", "--", "also reject strings", check=0)
    con = env.con()
    executors = con.execute("SELECT COUNT(*) FROM dispatches WHERE role='executor'").fetchone()[0]
    assert executors == 2, "the ack-only session is not relaunched blindly"
    t = task_row(env)
    assert t["status"] == "changes_required" and "A1" in (t["pause_reason"] or ""), t
    code, out = env.office("status")
    counts = next(line for line in out.splitlines() if line.startswith("accepted "))
    assert "live T1" not in counts, counts
    assert "office rerun T1 --resume" in out.splitlines()[-1], out
