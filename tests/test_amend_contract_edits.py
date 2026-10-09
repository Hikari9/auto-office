"""#425: an amendment that edits a task's structured contract (`--add-check`, `--add-accept`, `--set`) changes the
effective plan, so the new check is run at the gate, without queuing a plan review (#418), and the amendment's
old contract, effective contract, and rationale are recorded and shown by `office inspect amendments`."""
from __future__ import annotations

import json

import pytest

from office import planfile
from tests.test_amend_checks_only import (BAD_CHECK, EXTERNAL, GOOD_CHECK, _accepted, _checks_gates, _dispatches,
                                          _rows, _task, _v31_conftest, cenv)  # noqa: F401

PLAN = _v31_conftest().PLAN_ONE


def _plan_gates(env):
    return _rows(env, "SELECT id FROM gates WHERE subject='plan'")


def _amend(env, *args, scope="T1", delta="add this acceptance test"):
    return env.office("amend", scope, *args, "--", delta, env=EXTERNAL)


# ------------------------------------------------------------------ the text edit

def test_edit_task_adds_and_drops_list_entries_and_sets_scalars():
    out = planfile.edit_task(PLAN, "T1", add_checks=["pytest -q"], add_accept=["add(0, 0) == 0"],
                             set_fields={"depends": "none", "interfaces": "calc.add"})
    t = planfile.parse(out).tasks[0]
    assert t["checks"] == ['python3 -c "import calc; assert calc.add(2, 3) == 5"', "pytest -q"]
    assert t["accept"] == ["calc.add(2, 3) == 5", "add(0, 0) == 0"] and t["interfaces"] == ["calc.add"]
    dropped = planfile.parse(planfile.edit_task(out, "T1", drop_checks=["pytest"], drop_accept=["== 0"])).tasks[0]
    assert dropped["checks"] == t["checks"][:1] and dropped["accept"] == ["calc.add(2, 3) == 5"]
    assert planfile.parse(planfile.edit_task(PLAN, "T1", drop_checks=["calc.add"])).tasks[0]["checks"] == []


def test_edit_task_refuses_unknown_task_and_ambiguous_drop():
    with pytest.raises(ValueError, match="no task T9"):
        planfile.edit_task(PLAN, "T9", add_checks=["x"])
    with pytest.raises(ValueError, match="names 0"):
        planfile.edit_task(PLAN, "T1", drop_checks=["no such check"])


# ------------------------------------------------------------------ enforcement without plan review

@pytest.mark.parametrize("cenv", [None, "v3.1"], indirect=True)
def test_an_added_failing_check_is_enforced_at_the_gate_without_a_plan_review(cenv):
    before = _accepted(cenv)
    plan_gates = _plan_gates(cenv)
    code, out = _amend(cenv, "--add-check", BAD_CHECK)
    assert code == 0, out
    task = _task(cenv)
    assert json.loads(task["checks_json"]) == [json.loads(before["checks_json"])[0], BAD_CHECK], "the contract is versioned"
    assert task["status"] != "accepted", "an accepted task is reopened to the new contract"
    gates = _checks_gates(cenv)
    assert gates[-1]["verdict"] in ("CHANGES_REQUIRED", "RECHECK") and "1/2 checks passed" in gates[-1]["summary"], gates
    assert _plan_gates(cenv) == plan_gates, "no plan reviewer is launched by a post-initial amendment (#418)"


@pytest.mark.parametrize("cenv", [None, "v3.1"], indirect=True)
def test_an_added_passing_check_runs_and_the_task_stays_accepted(cenv):
    before = _accepted(cenv)
    dispatches = _dispatches(cenv)
    code, out = _amend(cenv, "--add-check", GOOD_CHECK)
    assert code == 0, out
    after = _task(cenv)
    assert after["status"] == "accepted" and after["accepted_revision_id"] == before["accepted_revision_id"]
    assert len(json.loads(after["checks_json"])) == 2 and _dispatches(cenv) == dispatches
    gates = _checks_gates(cenv)
    assert len(gates) == 2 and "2/2 checks passed" in gates[1]["summary"], gates


def test_an_added_acceptance_criterion_reopens_the_task_with_the_new_contract(cenv):
    _accepted(cenv)
    code, out = _amend(cenv, "--add-accept", "calc.add(0, 0) == 0")
    assert code == 0, out
    task = _task(cenv)
    assert json.loads(task["accept_json"]) == ["calc.add(2, 3) == 5", "calc.add(0, 0) == 0"]
    assert task["status"] != "accepted" and task["acceptance_version"] == 2, "reopened, executor relaunched"
    assert _rows(cenv, "SELECT status FROM deliveries WHERE task_id='T1'")


# ------------------------------------------------------------------ authority and class

def test_a_structured_edit_cannot_smuggle_an_authority_action(cenv):
    _accepted(cenv)
    code, out = _amend(cenv, "--add-check", "vercel deploy --prod")
    assert code != 0 and "contract-level-change" in out, out
    assert json.loads(_task(cenv)["checks_json"]) == [json.loads(_task(cenv)["checks_json"])[0]]
    assert _rows(cenv, "SELECT * FROM amendments WHERE class='ordinary'") == []


def test_scope_and_interface_edits_are_contract_amendments_only(cenv):
    _accepted(cenv)
    code, out = _amend(cenv, "--set", "scope=calc.py, extra.py")
    assert code != 0 and "contract-level-change" in out, out


def test_edits_need_a_named_task_and_a_known_entry(cenv):
    _accepted(cenv)
    code, out = cenv.office("amend", "plan", "--add-check", "true", "--", "x", env=EXTERNAL)
    assert code != 0 and "edit-needs-task" in out, out
    code, out = _amend(cenv, "--drop-check", "no such check")
    assert code != 0 and "bad-contract-edit" in out, out


def test_a_contract_edit_changes_the_interface_and_is_recorded(cenv):
    _accepted(cenv)
    code, out = _amend(cenv, "--contract", "--set", "interfaces=calc.add", delta="T1 now exports calc.add")
    assert code == 0, out
    task = _task(cenv)
    assert json.loads(task["interfaces_json"]) == ["calc.add"] and task["contract_version"] == 2, task
    assert task["status"] != "accepted", "an accepted task is reopened to the new contract"


# ------------------------------------------------------------------ audit history

def test_the_amendment_records_old_and_effective_contract_and_rationale(cenv):
    before = _accepted(cenv)
    code, out = _amend(cenv, "--add-check", GOOD_CHECK, delta="the rounding case must stay covered")
    assert code == 0, out
    row = _rows(cenv, "SELECT * FROM amendments WHERE class='ordinary'")[0]
    rec = json.loads(row["structured_json"])
    assert rec["rationale"] == "the rounding case must stay covered" and rec["plan_version"] == 2
    ch = rec["changed"]["T1"]
    assert ch["before"]["checks"] == json.loads(before["checks_json"])
    assert ch["after"]["checks"] == json.loads(before["checks_json"]) + [GOOD_CHECK]
    ev = _rows(cenv, "SELECT payload_json FROM events WHERE kind='amendment.contract_changed'")
    assert len(ev) == 1 and json.loads(ev[0]["payload_json"])["changed"]["T1"] == ch
    code, shown = cenv.office("inspect", "amendments")
    assert code == 0 and "the rounding case must stay covered" in shown and "T1 checks:" in shown, shown


def test_a_prose_only_note_stays_lightweight(cenv):
    _accepted(cenv)
    code, out = cenv.office("amend", "T1", "--", "mind the rounding", env=EXTERNAL)
    assert code == 0, out
    assert _rows(cenv, "SELECT structured_json FROM amendments")[-1]["structured_json"] is None
    assert _rows(cenv, "SELECT * FROM events WHERE kind='amendment.contract_changed'") == []


# ------------------------------------------------------------------ review follow-ups

_V = _v31_conftest()
PLAN_DEP = _V.PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1")


@pytest.fixture
def both_accepted(cenv):
    _V.approved_run(cenv, plan=PLAN_DEP, executor=[{"write": {"calc.py": _V.GOOD_ADD}, "submit": True},
                                                   {"write": {"mul.py": _V.GOOD_MUL}, "submit": True}],
                    code_reviewer=[{"reply": "VERDICT: PASS"}, {"reply": "VERDICT: PASS"}])
    cenv.office("dispatch", "T1", check=0)
    cenv.office("dispatch", "T2", check=0)
    st = {t["id"]: t["status"] for t in _rows(cenv, "SELECT id, status FROM tasks")}
    assert st == {"T1": "accepted", "T2": "accepted"}, st
    return cenv


def _dispatch_ids(env, tid):
    return [r["id"] for r in _rows(env, "SELECT id FROM dispatches WHERE task_id=? ORDER BY rowid", (tid,))]


def test_a_dependant_is_reopened_but_not_launched_before_its_dependency_is_accepted_again(both_accepted):
    env = both_accepted
    t1, t2 = _dispatch_ids(env, "T1"), _dispatch_ids(env, "T2")
    code, out = _amend(env, "--contract", "--set", "interfaces=calc.add", delta="T1 now exports calc.add")
    assert code == 0, out
    assert len(_dispatch_ids(env, "T1")) == len(t1) + 1, "the changed task is relaunched"
    assert _dispatch_ids(env, "T2") == t2, "its dependant is not launched against the stale T1"
    row = _rows(env, "SELECT status, pause_reason FROM tasks WHERE id='T2'")[0]
    assert row["status"] == "changes_required" and "A" in (row["pause_reason"] or ""), row
    assert _rows(env, "SELECT status FROM deliveries WHERE task_id='T2'"), "the delta waits for its next session"


def test_ripple_finds_dependants_through_an_edge_the_edit_removed(both_accepted):
    from office import amend, db, state
    env = both_accepted
    con = env.con()
    try:
        run = state.get_run(con, _rows(env, "SELECT id FROM runs")[0]["id"])
        sync = {"contract": ["T1"], "acceptance": [], "cancelled": []}
        with db.transaction(con):
            graph_now = {t["id"]: t["depends"] for t in state.tasks(con, run["id"])}
            assert graph_now["T2"] == ["T1"]
            con.execute("UPDATE tasks SET depends_json='[]' WHERE id='T2'")  # the edit removed the edge
            assert amend._ripple(con, run, {"T1": [], "T2": ["T1"]}, sync) == ["T2"]
            assert amend._ripple(con, run, {}, sync) == [], "the post-sync graph alone misses it"
    finally:
        con.close()


def test_a_planner_submitted_contract_reopens_dependants_without_launching_them(both_accepted):
    from office import amend, db, state
    env = both_accepted
    t2 = _dispatch_ids(env, "T2")
    con = env.con()
    try:
        run = state.get_run(con, _rows(env, "SELECT id FROM runs")[0]["id"])
        with db.transaction(con):
            amend.contract_from_planner(con, run, "A9", {"contract": ["T1"], "acceptance": [], "cancelled": []}, 2,
                                        {"T1": [], "T2": ["T1"]})
    finally:
        con.close()
    assert _dispatch_ids(env, "T2") == t2
    assert _rows(env, "SELECT status FROM tasks WHERE id='T2'")[0]["status"] == "changes_required"


def test_set_without_a_value_is_refused_not_read_as_none(cenv):
    _accepted(cenv)
    for arg in ("depends", "depends="):
        code, out = _amend(cenv, "--set", arg)
        assert code != 0 and "bad-contract-edit" in out and "needs a value" in out, out
    with pytest.raises(ValueError, match="needs a value"):
        planfile.edit_task(PLAN, "T1", set_fields={"depends": " "})
    assert planfile.parse(planfile.edit_task(PLAN, "T1", set_fields={"depends": "none"})).tasks[0]["depends"] == []


def test_edits_are_refused_on_a_requirements_amendment(cenv):
    _accepted(cenv)
    code, out = cenv.office("amend", "requirements", "--quote", "words", "--add-check", "true", "--", "x", env=EXTERNAL)
    assert code != 0 and "edits-are-not-requirements" in out, out
    assert _rows(cenv, "SELECT * FROM amendments") == []


def test_two_edit_amendments_in_a_row_keep_both_through_the_draft(cenv):
    _accepted(cenv)
    cenv.write_plan(PLAN)
    for check in (GOOD_CHECK, "true"):
        code, out = _amend(cenv, "--add-check", check)
        assert code == 0, out
    assert len(json.loads(_task(cenv)["checks_json"])) == 3
