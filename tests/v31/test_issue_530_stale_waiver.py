"""#530: do not record waivers on superseded lane compositions."""
import pytest
from office import convergence
from office.state import Refused


def test_waiver_refuses_when_accepted_revision_key_moved(monkeypatch):
    scope = {"id": "L-T1", "tasks": ["T1"]}
    state = {"key": "older-key", "commit": "a" * 40, "status": "escalated"}
    monkeypatch.setattr(convergence, "_key", lambda con, run, scope: "new-accepted-key")
    with pytest.raises(Refused) as exc:
        convergence._require_current_waiver_target(None, {"id": "R"}, scope, state)
    assert exc.value.category == "stale-composition"
    assert "office decide L-T1 review" in exc.value.next_step


def test_waiver_guard_requires_composed_commit_and_acceptance_key(monkeypatch):
    scope = {"id": "L-T1", "tasks": ["T1"]}
    monkeypatch.setattr(convergence, "_key", lambda con, run, scope: "accepted-key")
    for st in [{"key": "accepted-key"}, {"key": "older", "commit": "x"}, {"key": None, "commit": "x"}]:
        with pytest.raises(Refused, match="waiver for the older commit"):
            convergence._require_current_waiver_target(None, {}, scope, st)
    assert convergence._require_current_waiver_target(None, {}, scope,
                                                       {"key": "accepted-key", "commit": "b" * 40}) is None



def test_historical_waiver_reconciles_to_new_review_without_rebinding_old_receipt(env, monkeypatch):
    from office import convergence, db, jobs, state
    from test_convergence_contract import _start, APPROVED
    from conftest import GOOD_ADD
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    rid = con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    run = state.get_run(con, rid)
    landing = dict(run["landing"])
    groups = dict(landing["convergence"])
    groups["L-T1"] = {**groups["L-T1"], "status": "waived", "key": "historical-old-acceptance"}
    landing["convergence"] = groups
    with db.transaction(con):
        state.update_run(con, rid, landing=landing)
    run = state.get_run(con, rid)
    assert "historical waiver" in convergence.next_action(con, run)
    monkeypatch.setattr(jobs, "kick", lambda *args, **kwargs: None)
    with db.transaction(con):
        assert "L-T1" in convergence.retry_blocked(con, run)
    run = state.get_run(con, rid)
    assert convergence.scope_state(run, "L-T1")["status"] == "stale_waiver"
    result = convergence.decide(con, run, "L-T1", "review", quote="Please review latest accepted code")
    assert result.lines
    assert convergence.scope_state(state.get_run(con, rid), "L-T1")["status"] == "pending"
    assert con.execute("SELECT COUNT(*) FROM authorizations WHERE kind='decision' AND target='L-T1:review'").fetchone()[0] == 1
