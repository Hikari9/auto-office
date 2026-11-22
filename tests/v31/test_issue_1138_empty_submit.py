"""#1138: a task with accept criteria that submits a tree equal to its base is refused; `--no-change -- "<why>"`
acknowledges it, and an acknowledged revision is accepted only after an independent code review PASS."""
from __future__ import annotations

import json

import pytest

from test_self_review_ledger import _dispatch, _git
from test_self_review_receipt import _rev


def _empty(wt, d):
    _git(wt, "reset", "-q", "--hard", d["base_commit"])


def _gates(env, rev_id):
    return {r["kind"]: dict(r) for r in env.con().execute("SELECT * FROM gates WHERE revision_id=?", (rev_id,))}


def _status(env):
    return env.con().execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0]


def _evaluate(env):
    from office import db, gates, state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    with db.transaction(con):
        return gates.evaluate_acceptance(con, run, "T1")


@pytest.mark.integration
@pytest.mark.approved
def test_empty_submit_with_accept_criteria_is_refused_and_names_both_ways_out(env):
    wenv, wt, d = _dispatch(env)
    _empty(wt, d)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "no-change-submit" in out, out
    assert "office raise" in out and "--no-change" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_the_refusal_does_not_block_the_task(env):
    wenv, wt, d = _dispatch(env)
    _empty(wt, d)
    env.office("submit", cwd=wt, env=wenv)
    assert _status(env) not in ("blocked", "paused")


@pytest.mark.integration
@pytest.mark.approved
def test_no_change_needs_a_reason(env):
    wenv, wt, d = _dispatch(env)
    _empty(wt, d)
    code, out = env.office("submit", "--no-change", cwd=wt, env=wenv)
    assert code != 0 and "needs a reason" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_no_change_on_a_tree_that_changed_is_refused(env):
    wenv, wt, d = _dispatch(env)
    code, out = env.office("submit", "--no-change", "--", "already done", cwd=wt, env=wenv)
    assert code != 0 and "differs from the task base" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_an_acknowledged_no_change_is_recorded_and_not_accepted_on_checks_alone(env):
    wenv, wt, d = _dispatch(env)
    _empty(wt, d)
    code, out = env.office("submit", "--no-change", "--", "calc.py already adds", cwd=wt, env=wenv)
    assert code == 0 and "independent review decides acceptance" in out, out
    rev = _rev(env)
    assert json.loads(rev["self_review_json"])["no_change"] == "calc.py already adds"
    gates = _gates(env, rev["id"])
    assert gates["code_review"]["status"] in ("waiting", "queued"), gates
    # checks pass: still not accepted, the review is owed
    con = env.con()
    for g in gates.values():
        if g["kind"] == "checks":
            con.execute("UPDATE gates SET status='done', verdict='APPROVED' WHERE id=?", (g["id"],))
    con.commit()
    assert _evaluate(env) is False and _status(env) == "submitted"
    # the independent review passes: now it is
    con.execute("UPDATE gates SET status='done', verdict='PASS' WHERE id=?", (gates["code_review"]["id"],))
    con.commit()
    assert _evaluate(env) is True and _status(env) == "accepted"


@pytest.mark.integration
@pytest.mark.approved
def test_an_unreviewed_no_change_is_not_accepted_when_the_gate_is_missing(env):
    wenv, wt, d = _dispatch(env)
    _empty(wt, d)
    assert env.office("submit", "--no-change", "--", "holds", cwd=wt, env=wenv)[0] == 0
    rev = _rev(env)
    con = env.con()
    con.execute("DELETE FROM gates WHERE revision_id=? AND kind='code_review'", (rev["id"],))
    con.execute("UPDATE gates SET status='done', verdict='APPROVED' WHERE revision_id=?", (rev["id"],))
    con.commit()
    assert _evaluate(env) is False


def test_a_task_without_scope_is_exempt():
    from office import submit
    assert submit._check_no_change({"id": "T1", "scope": [], "accept": ["x"]}, None, "t", "b", False, "") is None


def test_task_review_outcome_maps_convergence_terms():
    from office import contract, gates, review_parse
    ok = gates._task_review_outcome({"status": contract.COMPLETED, "verdict": "APPROVED",
                                     "parsed": review_parse.Parsed(verdict="APPROVED")})
    assert ok["verdict"] == "PASS" and ok["parsed"].verdict == "PASS"
    bad = gates._task_review_outcome({"status": contract.COMPLETED, "verdict": "RECHECK"})
    assert bad["verdict"] == "CHANGES_REQUIRED"
    assert gates._task_review_outcome({"status": contract.UNAVAILABLE, "verdict": None})["verdict"] == "UNAVAILABLE"
    assert gates._task_review_outcome({"status": contract.REVOKED, "verdict": None})["verdict"] == "REVOKED"
