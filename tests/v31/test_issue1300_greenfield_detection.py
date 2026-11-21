"""#1300: the orchestrator judges new work at intake; greenfield with --fastlane keeps its convergence review."""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE, start_inline
from office import config as cfg
from office import convergence, fastlane, lifecycle, state, task_sizing
from office.state import Usage

PHASES = """phases:
- Establish the foundation and stable application interfaces
- Build the complete working core with automated tests
- Integrate and validate the end-to-end user flow
"""
PLAN = PLAN_ONE.replace("depends: none\n", "depends: none\n" + PHASES, 1)
WHY = "empty repository, nothing the build must fit into"


def _runs(env) -> list[dict]:
    con = env.con()
    try:
        return [state.get_run(con, r["id"]) for r in con.execute("SELECT id FROM runs ORDER BY created_at").fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------------ G4: new work with no gear

def test_new_work_without_a_gear_is_refused_as_a_greenfield_candidate_and_creates_no_run(env):
    code, out = env.office("start", "build a new app", "--new-work", "yes", WHY, "--planner", "inline")
    assert code == 4, out
    assert "greenfield-candidate" in out
    assert "--gear greenfield" in out and "--gear XS|S|M|L|XL" in out
    assert _runs(env) == []


def test_new_work_with_a_size_class_but_no_gear_is_still_the_invokers_choice(env):
    code, out = env.office("start", "build a new app", "--new-work", "yes", WHY, "--size-class", "S")
    assert code == 4 and "greenfield-candidate" in out, out
    assert _runs(env) == []


@pytest.mark.parametrize("gear", ["greenfield", "S"])
def test_new_work_with_the_invokers_gear_starts_and_records_the_judgement(env, gear):
    code, out = env.office("start", "build a new app", "--new-work", "yes", WHY, "--gear", gear,
                           "--blast-radius", "repo", "--planner", "inline")
    assert code == 0, out
    assert "warning" not in out
    [run] = _runs(env)
    assert run["gear"] == gear
    assert run["gates"]["greenfield_detection"] == {"judged_by": "orchestrator", "new_work": True, "why": WHY}


# ------------------------------------------------------------------ G2: greenfield on work that is not new

def test_greenfield_on_work_judged_not_new_warns_records_and_starts(env):
    why = "adds a page to a mature app with shared layouts"
    code, out = env.office("start", "add a page", "--new-work", "no", why, "--gear", "greenfield",
                           "--blast-radius", "repo", "--planner", "inline")
    assert code == 0, out
    warnings = [line for line in out.splitlines() if "warning" in line and "not new work" in line]
    assert len(warnings) == 1, out
    assert why in warnings[0]
    [run] = _runs(env)
    assert run["gear"] == "greenfield"
    assert run["gates"]["greenfield_detection"] == {"judged_by": "orchestrator", "new_work": False, "why": why}
    code, shown = env.office("inspect", "run")
    assert code == 0 and f"new work (judged by orchestrator): no, {why}" in shown, shown


def test_not_new_work_without_a_gear_keeps_the_default_gear(env):
    code, out = env.office("start", "fix a bug", "--new-work", "no", "one function in an existing module",
                           "--planner", "inline")
    assert code == 0, out
    [run] = _runs(env)
    assert run["gear"] == cfg.fit_gear(None, run["risk"])
    assert run["gates"]["greenfield_detection"]["new_work"] is False


def test_no_judgement_records_nothing(env):
    code, out = env.office("start", "fix a bug", "--gear", "S", "--planner", "inline")
    assert code == 0, out
    assert "greenfield_detection" not in _runs(env)[0]["gates"]


# ------------------------------------------------------------------ bad --new-work values

@pytest.mark.parametrize("value, why", [("maybe", WHY), ("true", WHY), ("yes", ""), ("no", "   ")])
def test_a_bad_new_work_value_or_empty_why_is_a_usage_error(env, value, why):
    code, out = env.office("start", "build a new app", "--new-work", value, why, "--gear", "S")
    assert code == 2, out
    assert "bad-new-work" in out
    assert _runs(env) == []


def test_new_work_without_a_why_is_a_usage_error(env):
    code, out = env.office("start", "build a new app", "--gear", "S", "--new-work", "yes")
    assert code == 2, out
    assert _runs(env) == []


def test_the_judgement_parser_shape():
    assert lifecycle.greenfield_detection(None) is None
    assert lifecycle.greenfield_detection(["YES", f"  {WHY} "]) == {"judged_by": "orchestrator", "new_work": True, "why": WHY}
    for bad in (["yes"], ["yes", WHY, "extra"], ["no", ""], ["y", WHY]):
        with pytest.raises(Usage):
            lifecycle.greenfield_detection(bad)


# ------------------------------------------------------------------ G3: greenfield with --fastlane

def test_greenfield_fastlane_pins_on_and_leads_at_the_xl_floor():
    on = fastlane.pin("greenfield", True)
    assert on == {"version": fastlane.VERSION, "policy": "on", "by": "flag"}
    assert fastlane.pin("greenfield", None) is None and fastlane.pin("greenfield", False) is None
    sized = task_sizing.classify({"descriptor": {}}, run_gear="greenfield")
    mode = task_sizing.execution_mode("greenfield", sized, {"classification": "low"}, fastlane=on)
    assert mode["mode"] == "inline" and mode["owner"] == "orchestrator"
    off = {**on, "policy": "off"}
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"}, fastlane=off)["mode"] == "delegated"
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"})["mode"] == "delegated"


def test_greenfield_fastlane_run_keeps_one_plan_task_and_the_convergence_review(env):
    env.trust()
    start_inline(env, plan=PLAN, gear="greenfield", extra=("--blast-radius", "repo", "--fastlane"))
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()["id"])
        assert run["gates"]["fastlane"]["policy"] == "on"
        assert run["gates"]["code_review"] is True
        assert run["gates"]["review_contract"] == "convergence-v1"
        assert state.get_task(con, run["id"], "T1")["descriptor"]["task_size"] == "XL"
        assert [lane["tasks"] for lane in convergence.lanes(con, run)] == [["T1"]]
    finally:
        con.close()
    assert env.office("approve", "plan", "--quote", "approved", check=0)[0] == 0
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    try:
        route = json.loads(con.execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()["route_json"])
    finally:
        con.close()
    assert route["execution_mode"]["mode"] == "inline", route["execution_mode"]
    assert "executor" not in [c["role"] for c in env.calls()]


def test_greenfield_no_fastlane_keeps_its_executor_contract(env):
    code, out = env.office("start", "build", "--gear", "greenfield", "--blast-radius", "repo", "--no-fastlane",
                           "--planner", "inline")
    assert code == 0, out
    assert "fastlane" not in _runs(env)[0]["gates"]
