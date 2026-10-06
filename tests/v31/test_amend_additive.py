"""M3: an ordinary plan amendment that only adds task(s) reports just those as affected and
delivers nothing to a running task; a pure-delta amendment still reaches every unaccepted task;
a changed acceptance still reaches the task it changes."""
from __future__ import annotations

import pytest

from conftest import PLAN_ONE, PLAN_TWO, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

pytestmark = pytest.mark.approved


def _running(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    assert task_row(env)["status"] == "running" and task_row(env)["current_dispatch_id"]


def _deliveries(env):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM deliveries ORDER BY created_at")]
    finally:
        con.close()


def test_adding_a_task_affects_only_the_new_task_and_delivers_to_no_running_task(env):
    _running(env)
    env.write_plan(PLAN_TWO)
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code == 0, out
    assert "affected T2" in out and "affected T1" not in out and "affected T1,T2" not in out, out
    assert "delivering" not in out, out
    assert _deliveries(env) == []
    assert task_row(env, "T2")["status"] == "planned"
    assert task_row(env)["status"] == "running"


def test_a_pure_delta_is_still_delivered_to_every_unaccepted_task(env):
    _running(env)
    code, out = env.office("amend", "plan", "--", "mention the module in a docstring", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out, out
    rows = _deliveries(env)
    assert [(r["task_id"], r["status"]) for r in rows] == [("T1", "queued")], rows


def test_a_changed_acceptance_is_delivered_to_that_task(env):
    _running(env)
    env.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    code, out = env.office("amend", "plan", "--", "also check zero", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out, out
    rows = _deliveries(env)
    assert [(r["task_id"], r["status"]) for r in rows] == [("T1", "queued")], rows


def test_an_added_task_alongside_a_changed_acceptance_still_delivers_to_the_changed_task(env):
    _running(env)
    plan = PLAN_TWO.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0")
    env.write_plan(plan)
    code, out = env.office("amend", "plan", "--", "also check zero and add mul", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1,T2" in out and "delivering to T1" in out, out
    assert [r["task_id"] for r in _deliveries(env)] == ["T1"]
