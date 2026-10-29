"""Integration regressions for #502 shadow delegation."""
import json
import pytest
from conftest import Env, PLAN_TWO, approved_run
from office import state


def _events(env):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT payload_json FROM events WHERE kind='delegation.shadow' ORDER BY seq")]
    finally:
        con.close()


def test_parallel_dispatch_keeps_the_real_decision_and_records_advice(env):
    approved_run(env, plan=PLAN_TWO)
    code, output = env.office("dispatch", "T1", "T2", "--parallel",
        env={"OFFICE_JOBS": "manual", "OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    assert code == 0, output
    records = _events(env)
    assert len(records) == 1
    shadow = json.loads(records[0]["payload_json"])
    assert shadow["mode"] == "shadow"
    assert shadow["actual"] == "parallel"
    assert shadow["proposal"] == "independent-lanes"
    assert shadow["policy_effect"].startswith("none:")
    con = env.con()
    try:
        tasks = {r["task_id"] for r in con.execute("SELECT task_id FROM dispatches WHERE role='executor'")}
        assert tasks == {"T1", "T2"}
    finally:
        con.close()
    _, response = env.ojson("inspect", "efficiency")
    assert response["data"]["delegation_shadow"][0]["actual"] == "parallel"


def test_cohesive_shadow_advice_does_not_override_requested_parallel_dispatch(env, monkeypatch):
    from office import delegation_shadow

    approved_run(env, plan=PLAN_TWO)
    original = delegation_shadow.propose

    def report_coupled_scope(tasks, *, parallel_requested, cache_policy=None):
        # Model a coupled proposal while leaving this fixture's dispatchable tasks unchanged.
        coupled = [{"id": "T1", "scope": ["src/*.py"], "depends": []},
                   {"id": "T2", "scope": ["src/office.py"], "depends": []}]
        return original(coupled, parallel_requested=parallel_requested, cache_policy=cache_policy)

    monkeypatch.setattr(delegation_shadow, "propose", report_coupled_scope)
    code, output = env.office("dispatch", "T1", "T2", "--parallel",
        env={"OFFICE_JOBS": "manual", "OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    assert code == 0, output
    shadow = json.loads(_events(env)[0]["payload_json"])
    assert shadow["proposal"] == "cohesive-or-serial"
    assert shadow["actual"] == "parallel"
    con = env.con()
    try:
        tasks = {r["task_id"] for r in con.execute("SELECT task_id FROM dispatches WHERE role='executor'")}
        assert tasks == {"T1", "T2"}
    finally:
        con.close()


def test_shadow_logging_on_or_off_keeps_dispatch_decisions_identical(tmp_path, monkeypatch):
    def run(root, enabled):
        with monkeypatch.context() as patch:
            instance = Env(root, patch)
            approved_run(instance, plan=PLAN_TWO)
            if not enabled:
                emit = state.emit

                def without_shadow(con, run, kind, *args, **kwargs):
                    if kind == "delegation.shadow":
                        return None
                    return emit(con, run, kind, *args, **kwargs)

                patch.setattr(state, "emit", without_shadow)
            code, output = instance.office("dispatch", "T1", "T2", "--parallel",
                env={"OFFICE_JOBS": "manual", "OFFICE_WORKER_LAUNCHER": "external"}, check=0)
            assert code == 0, output
            con = instance.con()
            try:
                run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
                decisions = [tuple(r) for r in con.execute(
                    "SELECT task_id,role FROM dispatches WHERE run_id=? ORDER BY task_id,role", (run_id,))]
                recorded = con.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id=? AND kind='delegation.shadow'", (run_id,)
                ).fetchone()[0]
            finally:
                con.close()
            return decisions, recorded

    with_shadow = run(tmp_path / "with-shadow", True)
    without_shadow = run(tmp_path / "without-shadow", False)
    assert with_shadow[0] == without_shadow[0] == [("T1", "executor"), ("T2", "executor")]
    assert with_shadow[1] == 1 and without_shadow[1] == 0


def test_shadow_event_rolls_back_if_dispatch_transaction_fails(env, monkeypatch):
    approved_run(env, plan=PLAN_TWO)
    original = state.emit

    def fail_after_event(con, run, kind, *args, **kwargs):
        value = original(con, run, kind, *args, **kwargs)
        if kind == "delegation.shadow":
            raise RuntimeError("simulated transaction failure")
        return value

    monkeypatch.setattr(state, "emit", fail_after_event)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        env.office("dispatch", "T1", "T2", "--parallel",
            env={"OFFICE_JOBS": "manual", "OFFICE_WORKER_LAUNCHER": "external"})
    assert _events(env) == []
    con = env.con()
    try:
        assert con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    finally:
        con.close()
