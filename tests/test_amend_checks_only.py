"""An ordinary amendment whose only change to an accepted task is its `checks:` reruns those checks on
the task's accepted revision. It does not reopen the task or hand an amendment to an executor: the task
stays accepted when the checks pass and goes to changes_required, naming the failing check, when they fail.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_V31 = ROOT / "tests" / "v31"
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
GOOD_CHECK = 'python3 -c "import calc; assert calc.add(10, 5) == 15"'
BAD_CHECK = 'python3 -c "import calc; assert calc.add(10, 5) == 99"'


def _v31_conftest():
    if "v31_env" not in sys.modules:
        sys.path.insert(0, str(_V31))
        spec = importlib.util.spec_from_file_location("v31_env", _V31 / "conftest.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["v31_env"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["v31_env"]


@pytest.fixture
def cenv(request, tmp_path, monkeypatch):
    """An isolated Env; `@pytest.mark.parametrize("cenv", ["v3.1"], indirect=True)` pins the review contract."""
    v = _v31_conftest()
    e = v.Env(tmp_path, monkeypatch, contract=getattr(request, "param", None))
    e.trust_snapshots = {}
    return v._activate(e, monkeypatch)


def _rows(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _task(env):
    return _rows(env, "SELECT * FROM tasks WHERE id='T1'")[0]


def _plan_with_checks(checks: str, extra_accept: str = ""):
    v = _v31_conftest()
    plan = v.PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {checks}")
    return plan.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5" + extra_accept)


def _accepted(env):
    """T1 accepted on its first revision: its executor submitted, its checks and review passed."""
    v = _v31_conftest()
    v.approved_run(env, executor=[{"write": {"calc.py": v.GOOD_ADD}, "submit": True}],
                   code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    task = _task(env)
    assert task["status"] == "accepted" and task["accepted_revision_id"], task
    return task


def _checks_gates(env):
    return _rows(env, "SELECT revision_id, status, verdict, summary FROM gates WHERE task_id='T1' AND kind='checks' "
                      "ORDER BY created_at")


def _dispatches(env):
    return [r["id"] for r in _rows(env, "SELECT id FROM dispatches WHERE task_id='T1' ORDER BY rowid")]


def _amend(env, plan: str, *scope):
    env.write_plan(plan)
    return env.office("amend", *(scope or ("plan",)), "--", "change T1's check", env=EXTERNAL)


@pytest.mark.parametrize("cenv", [None, "v3.1"], indirect=True)
def test_a_checks_only_amendment_reruns_the_checks_on_the_same_revision_without_a_relaunch(cenv):
    before = _accepted(cenv)
    dispatches = _dispatches(cenv)
    first_gates = _checks_gates(cenv)
    assert [g["revision_id"] for g in first_gates] == [before["accepted_revision_id"]]
    code, out = _amend(cenv, _plan_with_checks(GOOD_CHECK))
    assert code == 0, out
    assert "checks rerun on T1" in out and "delivering" not in out and "affected T1" not in out, out
    after = _task(cenv)
    assert after["status"] == "accepted", after
    assert after["accepted_revision_id"] == before["accepted_revision_id"] == after["current_revision_id"]
    assert after["contract_version"] == before["contract_version"], "the contract is untouched"
    assert json.loads(after["checks_json"]) == [GOOD_CHECK]
    assert _dispatches(cenv) == dispatches, "no executor is launched"
    assert _rows(cenv, "SELECT * FROM deliveries") == [], "no amendment is delivered"
    gates = _checks_gates(cenv)
    assert len(gates) == 2 and gates[1]["revision_id"] == before["accepted_revision_id"], gates
    assert gates[1]["status"] == "done" and gates[1]["verdict"] in ("PASS", "APPROVED"), gates


@pytest.mark.parametrize("cenv", [None, "v3.1"], indirect=True)
def test_a_failing_amended_check_reopens_the_task_and_names_the_check(cenv):
    before = _accepted(cenv)
    dispatches = _dispatches(cenv)
    code, out = _amend(cenv, _plan_with_checks(BAD_CHECK))
    assert code == 0, out
    after = _task(cenv)
    assert after["status"] == "changes_required", after
    assert after["current_revision_id"] == before["accepted_revision_id"]
    assert _dispatches(cenv) == dispatches
    gates = _checks_gates(cenv)
    assert gates[-1]["verdict"] in ("CHANGES_REQUIRED", "RECHECK") and "0/1 checks passed" in gates[-1]["summary"], gates
    findings = _rows(cenv, "SELECT location, summary, state FROM findings WHERE task_id='T1' AND gate_kind='checks'")
    assert [(f["location"], f["state"]) for f in findings] == [(BAD_CHECK, "open")], findings
    assert "check failed" in findings[0]["summary"]


def test_naming_the_task_in_a_checks_only_amendment_still_does_not_reopen_it(cenv):
    before = _accepted(cenv)
    dispatches = _dispatches(cenv)
    code, out = _amend(cenv, _plan_with_checks(GOOD_CHECK), "T1")
    assert code == 0, out
    assert "delivering" not in out, out
    after = _task(cenv)
    assert after["status"] == "accepted" and after["contract_version"] == before["contract_version"], after
    assert _dispatches(cenv) == dispatches and _rows(cenv, "SELECT * FROM deliveries") == []


def test_dropping_the_checks_leaves_the_task_accepted_with_nothing_to_run(cenv):
    _accepted(cenv)
    dispatches = _dispatches(cenv)
    gates = _checks_gates(cenv)
    code, out = _amend(cenv, _plan_with_checks("none"))
    assert code == 0, out
    assert _task(cenv)["status"] == "accepted" and json.loads(_task(cenv)["checks_json"]) == []
    assert _dispatches(cenv) == dispatches and _checks_gates(cenv) == gates


def test_a_changed_acceptance_still_reopens_an_accepted_task(cenv):
    _accepted(cenv)
    first = _dispatches(cenv)
    code, out = _amend(cenv, _plan_with_checks(GOOD_CHECK, "\n- calc.add(0, 0) == 0"))
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out, out
    assert _task(cenv)["status"] == "running"
    assert len(_dispatches(cenv)) == len(first) + 1, "the executor is relaunched with the amendment"
    assert [r["status"] for r in _rows(cenv, "SELECT status FROM deliveries")] == ["queued"]


def test_a_checks_change_alongside_an_acceptance_change_still_reopens_an_accepted_task(cenv):
    _accepted(cenv)
    first = _dispatches(cenv)
    code, out = _amend(cenv, _plan_with_checks(BAD_CHECK, "\n- calc.add(0, 0) == 0"))
    assert code == 0, out
    assert "delivering to T1" in out and "checks rerun" not in out, out
    assert len(_dispatches(cenv)) == len(first) + 1


def test_a_live_task_still_receives_a_checks_amendment(cenv):
    v = _v31_conftest()
    v.approved_run(cenv, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    cenv.office("dispatch", "T1", env=EXTERNAL, check=0)
    live = _task(cenv)
    assert live["status"] == "running" and live["current_dispatch_id"], live
    code, out = _amend(cenv, _plan_with_checks(GOOD_CHECK))
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out and "checks rerun" not in out, out
    assert [(r["task_id"], r["status"]) for r in _rows(cenv, "SELECT task_id, status FROM deliveries")] == [("T1", "queued")]
    assert _checks_gates(cenv) == []
    assert _task(cenv)["current_dispatch_id"] == live["current_dispatch_id"]


def test_a_checks_only_amendment_does_not_fall_back_to_telling_every_other_live_task(cenv):
    v = _v31_conftest()
    v.approved_run(cenv, plan=v.PLAN_TWO, executor=[{"write": {"calc.py": v.GOOD_ADD}, "submit": True}],
                   code_reviewer=[{"reply": "VERDICT: PASS"}])
    cenv.office("dispatch", "T1", check=0)
    assert _task(cenv)["status"] == "accepted"
    plan = v.PLAN_TWO.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {GOOD_CHECK}")
    code, out = _amend(cenv, plan)
    assert code == 0, out
    assert "checks rerun on T1" in out and "affected" not in out and "delivering" not in out, out
