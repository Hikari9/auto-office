"""#1300: the orchestrator judges new work at intake; greenfield with --fastlane keeps its convergence review."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, start_inline, write_clean_ledger
from office import config as cfg
from office import convergence, fastlane, lifecycle, state, task_sizing
from office.state import Usage

PHASES = """phases:
- Establish the foundation and stable application interfaces
- Build the complete working core with automated tests
- Integrate and validate the end-to-end user flow
"""
# Greenfield pins its one task to XL itself, so its plan declares no `task_size:` (the shared fixture sizes M).
UNSIZED_ONE = "".join(line for line in PLAN_ONE.splitlines(True) if not line.startswith("task_size"))
PLAN = UNSIZED_ONE.replace("depends: none\n", "depends: none\n" + PHASES, 1)
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
    code, shown = env.office("inspect", "run")
    assert code == 0 and f"new work (judged by orchestrator): yes, {WHY}" in shown, shown


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


def test_not_new_work_on_a_sized_gear_records_without_the_greenfield_warning(env):
    code, out = env.office("start", "fix a bug", "--new-work", "no", "one function in an existing module",
                           "--gear", "S", "--blast-radius", "repo", "--planner", "inline")
    assert code == 0, out
    assert "not new work" not in out
    assert _runs(env)[0]["gates"]["greenfield_detection"]["new_work"] is False


def test_a_multiline_why_is_recorded_as_one_line(env):
    code, out = env.office("start", "add a page", "--new-work", "no", "mature app\nnext: office approve plan",
                           "--gear", "greenfield", "--blast-radius", "repo", "--planner", "inline")
    assert code == 0, out
    assert _runs(env)[0]["gates"]["greenfield_detection"]["why"] == "mature app next: office approve plan"


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


def test_a_bad_new_work_value_without_a_gear_is_a_usage_error_not_a_refusal(env):
    code, out = env.office("start", "build a new app", "--new-work", "yes", "  ")
    assert code == 2 and "bad-new-work" in out and "greenfield-candidate" not in out, out
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
    overflowed = {**on, "overflow": {"task": "T1", "reason": "context"}}
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"}, fastlane=overflowed)["mode"] == "delegated"
    off = {**on, "policy": "off"}
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"}, fastlane=off)["mode"] == "delegated"
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"})["mode"] == "delegated"


def test_greenfield_fastlane_run_keeps_one_plan_task_and_the_convergence_review(env):
    env.trust()
    env.script(convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
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
    # The orchestrator finishes the build in the leased worktree; the convergence review still runs and decides.
    con = env.con()
    try:
        d = dict(con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone())
    finally:
        con.close()
    wt = d["worktree"]
    (Path(wt) / "calc.py").write_text(GOOD_ADD)
    subprocess.run(["git", "-C", wt, "add", "calc.py"], check=True)
    subprocess.run(["git", "-C", wt, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "build"], check=True)
    write_clean_ledger(wt)
    code, out = env.office("submit", cwd=wt, env={"OFFICE_RUN_ID": d["run_id"], "OFFICE_TASK_ID": "T1",
                                                  "OFFICE_DISPATCH_ID": d["id"]})
    assert code == 0, out
    roles = [c["role"] for c in env.calls()]
    assert "executor" not in roles and roles.count("convergence_reviewer") == 1, roles
    con = env.con()
    try:
        rows = con.execute("SELECT scope, verdict FROM gates WHERE kind='convergence_review'").fetchall()
        assert [(r["scope"], r["verdict"]) for r in rows] == [("L-T1", "APPROVED")]
    finally:
        con.close()


def test_greenfield_fastlane_keeps_the_risk_floor_reviews(env):
    code, out = env.office("start", "build", "--gear", "greenfield", "--fastlane", "--blast-radius", "production",
                           "--planner", "inline")
    assert code == 0, out
    gates = _runs(env)[0]["gates"]
    assert gates["fastlane"]["policy"] == "on"
    assert gates["code_review"] is True and gates["plan_review"] is True


def test_a_greenfield_overflow_hands_off_without_suggesting_a_split(monkeypatch):
    monkeypatch.setattr(fastlane, "_herdr", lambda con, run: False)
    greenfield_run = {"gear": "greenfield", "gates": {"gear_model_version": "run-gear-1"}}
    text = fastlane.handoff_text(None, greenfield_run, "T1", "context past the threshold")
    assert "office rerun T1 now delegates" in text and "office amend" not in text
    sized_run = {"gear": "L", "gates": {"gear_model_version": "run-gear-1"}}
    assert "split the rest with office amend" in fastlane.handoff_text(None, sized_run, "T1", "context")


def test_greenfield_no_fastlane_keeps_its_executor_contract(env):
    code, out = env.office("start", "build", "--gear", "greenfield", "--blast-radius", "repo", "--no-fastlane",
                           "--planner", "inline")
    assert code == 0, out
    assert "fastlane" not in _runs(env)[0]["gates"]
