"""Run-level `checks:` (landing.run_checks): parsing, sticky-plan replacement,
and integration re-check on amendment (Hikari9/auto-office#185, favorchurch/rock-security#290)."""
from __future__ import annotations

import json

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, start_inline
from office import planfile

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

PLAN_RC_A = PLAN_ONE.replace("blast_radius: repo\n", 'blast_radius: repo\nchecks: python3 -c "assert 1 == 1"\n')
PLAN_RC_B = PLAN_RC_A.replace('checks: python3 -c "assert 1 == 1"', 'checks: python3 -c "assert 2 == 2"')
PLAN_RC_NONE = PLAN_RC_A.replace('checks: python3 -c "assert 1 == 1"', "checks: none")

PLAN_BAD_CHECK = PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: office-test-nonexistent-cmd\n")

PLAN_TWO_RC = PLAN_TWO.replace("blast_radius: repo\n", 'blast_radius: repo\nchecks: python3 -c "assert 1 == 1"\n')
PLAN_TWO_RC2 = PLAN_TWO_RC.replace('checks: python3 -c "assert 1 == 1"', 'checks: python3 -c "assert 2 == 2"')


def _go(env, plan=PLAN_ONE, gear="direct+review", **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear=gear)
    env.office("approve", "plan", "--quote", "approved", check=0)


def _landing(con, run_id):
    return json.loads(con.execute("SELECT landing_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])


def _events(con, run_id):
    return [r["kind"] for r in con.execute("SELECT kind FROM events WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()]


# ------------------------------------------------------------------ planfile parsing


def test_run_level_checks_none_parses_to_empty():
    text = PLAN_RC_NONE
    parsed = planfile.parse(text)
    assert parsed.run_checks == [], parsed.run_checks
    assert parsed.tasks[0]["checks"] == ['python3 -c "import calc; assert calc.add(2, 3) == 5"']  # task-level unaffected


def test_run_level_checks_list_form_filters_none_and_na():
    text = """# Plan

## Requirements
done:
- x
blast_radius: repo
checks:
- pytest -q
- none
- N/A

## Tasks
### T1: Do it
scope: a.py
depends: none
checks: pytest -q
accept:
- ok
visual: none
"""
    parsed = planfile.parse(text)
    assert parsed.run_checks == ["pytest -q"], parsed.run_checks


# ------------------------------------------------------------------ submit: new plan wins


def test_submit_replaces_run_checks_instead_of_merging(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_RC_A)
    code, out = env.office("submit", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    assert _landing(con, run_id)["run_checks"] == ['python3 -c "assert 1 == 1"']

    env.write_plan(PLAN_RC_B)
    code, out = env.office("submit", check=0)
    assert _landing(con, run_id)["run_checks"] == ['python3 -c "assert 2 == 2"']  # replaced, not appended

    env.write_plan(PLAN_RC_NONE)
    code, out = env.office("submit", check=0)
    assert _landing(con, run_id)["run_checks"] == []  # removed entirely


def test_amend_contract_rederives_run_checks(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_RC_A)
    code, out = env.office("submit", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    assert _landing(con, run_id)["run_checks"] == ['python3 -c "assert 1 == 1"']

    env.write_plan(PLAN_RC_B)
    code, out = env.office("amend", "plan", "--contract", "--", "swap the run-level check", check=0)
    assert _landing(con, run_id)["run_checks"] == ['python3 -c "assert 2 == 2"']


# ------------------------------------------------------------------ integration re-check


def test_amend_removing_bad_run_check_retriggers_integration(env):
    _go(env, plan=PLAN_BAD_CHECK, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    landing = _landing(con, run_id)
    assert landing["integration"]["status"] == "blocked", landing
    assert "UNAVAILABLE" in landing["integration"]["detail"], landing
    assert "integration.recheck" not in _events(con, run_id)

    env.write_plan(PLAN_ONE)  # drops the broken run-level check entirely
    code, out = env.office("amend", "plan", "--", "remove the broken run-level check", check=0)

    landing = _landing(con, run_id)
    assert landing["run_checks"] == []
    assert "integration.recheck" in _events(con, run_id)
    assert landing["integration"]["status"] == "accepted", landing


def test_no_recheck_while_a_task_is_not_accepted(env):
    _go(env, plan=PLAN_TWO_RC,
        executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}}, "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    assert con.execute("SELECT status FROM tasks WHERE id='T2'").fetchone()[0] != "accepted"

    env.write_plan(PLAN_TWO_RC2)
    code, out = env.office("amend", "plan", "--", "tighten the run-level check", check=0)

    landing = _landing(con, run_id)
    assert landing["run_checks"] == ['python3 -c "assert 2 == 2"']  # still re-derived
    assert "integration.recheck" not in _events(con, run_id)  # but no recheck: T2 isn't accepted
    assert landing.get("integration", {}).get("status") != "accepted"
