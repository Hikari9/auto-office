"""`amend requirements` after `office revoke T1` (#1085).

Office 3.15.59, convergence RECHECK: `office revoke T1` recorded `lease.revoked` for T1, yet
`office amend requirements` still refused `scope-held: T2 scope overlaps T1, which holds a live lease`.
The amendment relaunched the revoked T1, which took a fresh lease on its scope and blocked T2 in the same
amendment. A revoked lease holds no scope. A live, unrevoked lease still blocks.
"""
from __future__ import annotations
import pytest
from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run, self_reviewed, task_row
from test_dormant_lease import _end, _worker

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
CHANGES = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | needs a comment | add one"

# T2 builds on T1 and overlaps its file, so T1's lease holds T2's scope while T1 is live.
PLAN_OVERLAP = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py, calc.py\ndepends: T1")
AMEND = ["amend", "requirements", "--quote", "also log each call", "--", "R9: log each call"]
DELTA = "also reject non-numeric arguments with a TypeError"


def _t2_submitted_and_ended(env):
    """T1 submitted with changes (its lease is dormant); T2 submitted behind it and its worker ended."""
    approved_run(env, plan=PLAN_OVERLAP, executor=[{}], code_reviewer=[{"reply": CHANGES}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    (wt1 / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt1, "calc.py")
    env.office("submit", cwd=wt1, env=w1, check=0)
    assert task_row(env, "T1")["status"] == "changes_required", task_row(env, "T1")
    _end(env, w1["OFFICE_DISPATCH_ID"])
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    w2, wt2 = _worker(env, "T2")
    (wt2 / "mul.py").write_text(GOOD_MUL)
    self_reviewed(wt2, "mul.py")
    env.office("submit", cwd=wt2, env=w2, check=0)
    assert task_row(env, "T2")["status"] == "submitted", task_row(env, "T2")
    _end(env, w2["OFFICE_DISPATCH_ID"])
    return w1


PLAN_NODEP = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py, calc.py\ndepends: none")
BAD_ADD = "def add(a, b):\n    return 0\n"
BAD_MUL = "def mul(a, b):\n    return 0\n"


def _convergence_both_unaccepted(env):
    """Convergence contract: T1 and T2 both submit with failing checks (changes_required, workers ended), so
    both keep dormant leases on overlapping scopes. This is the RECHECK shape the issue reports."""
    approved_run(env, plan=PLAN_NODEP, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    (wt1 / "calc.py").write_text(BAD_ADD)
    self_reviewed(wt1, "calc.py")
    env.office("submit", cwd=wt1, env=w1, check=0)
    assert task_row(env, "T1")["status"] == "changes_required", task_row(env, "T1")
    _end(env, w1["OFFICE_DISPATCH_ID"])
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    w2, wt2 = _worker(env, "T2")
    (wt2 / "mul.py").write_text(BAD_MUL)
    self_reviewed(wt2, "mul.py")
    env.office("submit", cwd=wt2, env=w2, check=0)
    assert task_row(env, "T2")["status"] == "changes_required", task_row(env, "T2")
    _end(env, w2["OFFICE_DISPATCH_ID"])


def test_convergence_amend_requirements_is_not_scope_held_by_a_revoked_lease(env):
    _convergence_both_unaccepted(env)
    code, out = env.office("revoke", "T1", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office(*AMEND, env=EXTERNAL)
    assert code == 0 and "scope-held" not in out, out
    assert _live_t1_leases(env) == 0, "the amendment relaunched the revoked T1 and gave it a live lease"


def _live_t1_leases(env) -> int:
    return env.con().execute("SELECT COUNT(*) FROM leases WHERE task_id='T1' AND revoked_at IS NULL AND released_at IS NULL"
                             ).fetchone()[0]


@pytest.mark.review_contract("v3.1")
def test_amend_requirements_is_not_scope_held_by_a_revoked_lease(env):
    _t2_submitted_and_ended(env)
    code, out = env.office("revoke", "T1", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office(*AMEND, env=EXTERNAL)
    assert code == 0 and "scope-held" not in out and "requirements r3 recorded" in out, out
    assert "T1 waits for office rerun (lease revoked)" in out, out
    assert "delivered to T2" in out and "delivered to T1" not in out, out
    assert task_row(env, "T1")["status"] == "paused", task_row(env, "T1")
    assert _live_t1_leases(env) == 0, "the amendment relaunched the revoked T1 and gave it a live lease"
    queued = env.con().execute("SELECT COUNT(*) FROM deliveries WHERE task_id='T1' AND status='queued'").fetchone()[0]
    assert queued == 1, "the requirements change must stay queued for T1's next launch"


def _queued_t1_dispatches(env) -> list[str]:
    return [r[0] for r in env.con().execute("SELECT dispatch_id FROM deliveries WHERE task_id='T1' AND status='queued'")]


@pytest.mark.review_contract("v3.1")
def test_ordinary_amendment_does_not_relaunch_a_revoked_task(env):
    _t2_submitted_and_ended(env)
    revoked = task_row(env)["current_dispatch_id"]
    code, out = env.office("revoke", "T1", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office("amend", "T1", "--", DELTA, env=EXTERNAL)
    assert code == 0 and "T1 waits for office rerun (lease revoked)" in out and "delivering to" not in out, out
    assert _live_t1_leases(env) == 0, "an ordinary amendment relaunched the revoked T1"
    assert _queued_t1_dispatches(env) == [revoked], "the delivery must stay queued on the revoked dispatch"


@pytest.mark.review_contract("v3.1")
def test_contract_amendment_does_not_relaunch_a_revoked_task(env):
    _t2_submitted_and_ended(env)
    revoked = task_row(env)["current_dispatch_id"]
    code, out = env.office("revoke", "T1", env=EXTERNAL)
    assert code == 0, out
    env.write_plan(PLAN_OVERLAP.replace("accept:\n- calc.add(2, 3) == 5", "accept:\n- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    code, out = env.office("amend", "T1", "--contract", "--", "T1 also accepts add(0, 0)", env=EXTERNAL)
    assert code == 0, out
    assert _live_t1_leases(env) == 0, "a contract amendment relaunched the revoked T1"
    assert _queued_t1_dispatches(env) == [revoked], "the contract delivery must stay queued on the revoked dispatch"


@pytest.mark.review_contract("v3.1")
def test_rerun_resolves_the_requirements_delivery_queued_on_a_revoked_task(env):
    _t2_submitted_and_ended(env)
    code, out = env.office("revoke", "T1", env=EXTERNAL)
    assert code == 0, out
    code, out = env.office(*AMEND, env=EXTERNAL)
    assert code == 0 and "T1 waits for office rerun (lease revoked)" in out, out
    waiting = env.con().execute("SELECT id FROM deliveries WHERE task_id='T1' AND status='queued'").fetchone()[0]
    # The requirements change relaunched T2 (launching), and that session holds the overlapping scope until revoked.
    env.office("revoke", "T2", env=EXTERNAL, check=0)
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0, out
    row = env.con().execute("SELECT status, superseded_by FROM deliveries WHERE id=?", (waiting,)).fetchone()
    # request_launch supersedes deliveries at or below the fresh session's applied plan version (every relaunch runs it).
    assert tuple(row) == ("superseded", "relaunch"), tuple(row)
    assert _queued_t1_dispatches(env) == [], "no delivery stays queued on the revoked dispatch"


@pytest.mark.review_contract("v3.1")
def test_amend_requirements_is_still_scope_held_by_a_live_unrevoked_lease(env):
    _t2_submitted_and_ended(env)
    con = env.con()
    con.execute("UPDATE tasks SET status='running' WHERE id='T1'")
    con.commit()
    code, out = env.office(*AMEND, env=EXTERNAL)
    assert code == 4 and "scope-held" in out and "T1" in out, out


@pytest.mark.review_contract("v3.1")
def test_revoked_without_live_lease_reads_the_newest_lease(env):
    from office import dispatch
    _t2_submitted_and_ended(env)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]

    def holds_back():
        return dispatch.revoked_without_live_lease(con, run_id, "T1")

    assert not holds_back(), "an unrevoked lease is not revoked"
    env.office("revoke", "T1", env=EXTERNAL)
    assert holds_back(), "the revoked newest lease, no live session"
    con.execute("UPDATE leases SET revoke_reason='superseded' WHERE task_id='T1' AND revoked_at IS NOT NULL")
    con.commit()
    assert holds_back(), "the free-text revoke reason does not change the newest revoked lease"
    con.execute("UPDATE leases SET revoke_reason='orchestrator revoke' WHERE task_id='T1' AND revoked_at IS NOT NULL")
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, task_id, fencing, "
                "dispatch_id, renewed_at) VALUES('Lnew', ?, 'executor', '[\"calc.py\"]', 'Dnew', '2026-01-01', "
                "'2999-01-01', 'T1', 999, 'Dnew', '2026-01-01')", (run_id,))
    con.commit()
    assert not holds_back(), "a newer live lease means the task was relaunched"


@pytest.mark.review_contract("v3.1")
def test_revoked_lease_with_a_live_session_is_not_held_back(env, monkeypatch):
    """A revoked lease whose task still has a live session is not treated as ended: the session is real."""
    from office import dispatch, gates
    _t2_submitted_and_ended(env)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    env.office("revoke", "T1", env=EXTERNAL)
    assert dispatch.revoked_without_live_lease(con, run_id, "T1")
    monkeypatch.setattr(gates, "live_task_session", lambda *a, **k: "Dlive")
    assert not dispatch.revoked_without_live_lease(con, run_id, "T1"), "a live session of T1 keeps its lease"


def test_revoked_without_live_lease_runs_on_the_default_tier(tmp_path):
    """Unit tier (no `env` fixture, so it is collected without --all): the check resolves gates at call time."""
    from office import db, dispatch
    con = db.connect(tmp_path / "runs.db")
    db.migrate(con)
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at, task_id, fencing, "
                "dispatch_id, renewed_at, revoked_at, revoke_reason) VALUES('L1', 'R', 'executor', '[\"calc.py\"]', 'D1', "
                "'2026-01-01', '2026-01-02', 'T1', 1, 'D1', '2026-01-01', '2026-01-01', 'orchestrator revoke')")
    assert dispatch.revoked_without_live_lease(con, "R", "T1") is True
