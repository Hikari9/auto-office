"""Integration regressions for #502 shadow delegation."""
import json
import pytest
from conftest import PLAN_TWO, approved_run
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
