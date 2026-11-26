"""A plan task with no `task_size:` is refused at submit and at amendment (Rico 2026-11-26, Q3).

Greenfield pins its one cohesive task to XL itself and keeps its own rule. A task that an already-approved
plan left unsized (a run approved before this rule) survives an ordinary amendment, but no task is added unsized.
"""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE, PLAN_TWO, approved_run, start_inline
from office import greenfield, planfile
from office.util import sha256_bytes
from test_issue936_greenfield_gear import PLAN as GREENFIELD_PLAN

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _unsize(plan: str, *task_ids: str) -> str:
    """`plan` with the task_size lines dropped from the named tasks (all tasks when none are named)."""
    out, current = [], None
    for line in plan.splitlines(True):
        if line.startswith("### "):
            current = line[4:].split(":", 1)[0]
        if line.startswith("task_size") and (not task_ids or current in task_ids):
            continue
        out.append(line)
    return "".join(out)


def _plan_rows(env) -> int:
    con = env.con()
    try:
        return con.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    finally:
        con.close()


def _begin(env, gear="S"):
    code, out = env.office("start", "fixture goal", "--gear", gear, "--planner", "inline")
    assert code == 0, out


def _submit(env, plan: str):
    env.write_plan(plan)
    return env.office("submit")


# ------------------------------------------------------------------ plan submit

def test_submit_refuses_an_unsized_task_and_stores_nothing(env):
    _begin(env)
    code, out = _submit(env, _unsize(PLAN_ONE))
    assert code != 0 and "plan-invalid" in out, out
    assert "T1" in out and "task_size" in out and "task_size_rationale" in out, out
    assert _plan_rows(env) == 0


def test_submit_names_every_unsized_task_and_only_those(env):
    _begin(env)
    code, out = _submit(env, _unsize(PLAN_TWO))
    assert code != 0 and "plan-invalid" in out and "T1, T2" in out, out
    assert _plan_rows(env) == 0
    code, out = _submit(env, _unsize(PLAN_TWO, "T2"))
    assert code != 0 and "T2 has no" in out and "T1" not in out.split("plan has problems:", 1)[1], out


def test_submit_accepts_a_sized_plan(env):
    start_inline(env, plan=PLAN_TWO)
    assert _plan_rows(env) == 1


def test_greenfield_keeps_its_own_rule_and_accepts_an_unsized_task(env):
    start_inline(env, plan=GREENFIELD_PLAN, gear="greenfield")
    assert _plan_rows(env) == 1


# ------------------------------------------------------------------ the rule itself

def test_sizing_problems_ignore_greenfield_and_tasks_a_carried_plan_left_unsized():
    unsized = planfile.parse(_unsize(PLAN_TWO)).tasks
    assert greenfield.sizing_problems({"gear": "greenfield"}, unsized) == []
    assert len(greenfield.sizing_problems({"gear": "S"}, unsized)) == 1
    assert greenfield.sizing_problems({"gear": "S"}, unsized, carried=unsized) == []
    sized = planfile.parse(PLAN_TWO).tasks
    assert greenfield.sizing_problems({"gear": "S"}, unsized, carried=sized) != [], "an amendment cannot unsize a sized task"
    assert greenfield.sizing_problems({"gear": "S"}, sized) == []


# ------------------------------------------------------------------ amendments

@pytest.mark.approved
def test_an_amendment_adding_an_unsized_task_is_refused(env):
    approved_run(env, executor=[{}])
    env.write_plan(_unsize(PLAN_TWO, "T2"))
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code != 0 and "plan-invalid" in out and "T2 has no" in out, out
    assert _plan_rows(env) == 1


@pytest.mark.approved
def test_a_contract_amendment_adding_an_unsized_task_is_refused(env):
    approved_run(env, executor=[{}])
    env.write_plan(_unsize(PLAN_TWO, "T2"))
    code, out = env.office("amend", "plan", "--contract", "--", "also add a mul task", env=EXTERNAL)
    assert code != 0 and "plan-invalid" in out and "T2 has no" in out, out
    assert _plan_rows(env) == 1


@pytest.mark.approved
def test_an_amendment_adding_a_sized_task_is_accepted(env):
    approved_run(env, executor=[{}])
    env.write_plan(PLAN_TWO)
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code == 0, out


def _make_plan_legacy(env):
    """Rewrite the stored plan as a run approved before this rule would hold it: no task carries a size."""
    con = env.con()
    try:
        row = con.execute("SELECT body, tasks_json FROM plans ORDER BY version DESC LIMIT 1").fetchone()
        body = _unsize(row["body"])
        tasks = [{**t, "descriptor": {}} for t in json.loads(row["tasks_json"])]
        con.execute("UPDATE plans SET body=?, tasks_json=?, content_hash=?",
                    (body, json.dumps(tasks), sha256_bytes(body.encode())))
        con.commit()
    finally:
        con.close()


@pytest.mark.approved
def test_a_legacy_unsized_task_survives_an_amendment_that_adds_a_sized_one(env):
    approved_run(env, executor=[{}])
    _make_plan_legacy(env)
    env.write_plan(_unsize(PLAN_TWO, "T1"))
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code == 0, out


@pytest.mark.approved
def test_resubmitting_the_unsized_plan_on_record_stays_a_no_op(env):
    approved_run(env, executor=[{}])
    _make_plan_legacy(env)
    con = env.con()
    try:
        body = con.execute("SELECT body FROM plans ORDER BY version DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    code, out = _submit(env, body)
    assert code == 0 and "already submitted" in out, out
    code, out = _submit(env, body + "\n### T2: Implement mul\nscope: mul.py\ndepends: none\nchecks: none\naccept:\n- mul exists\nvisual: none\n")
    assert code != 0 and "plan-invalid" in out, out


@pytest.mark.approved
def test_a_legacy_plan_still_takes_a_delta_only_amendment(env):
    approved_run(env, executor=[{}])
    _make_plan_legacy(env)
    code, out = env.office("amend", "plan", "--", "mention the module in a docstring", env=EXTERNAL)
    assert code == 0, out


@pytest.mark.approved
def test_a_legacy_plan_still_refuses_a_new_unsized_task_but_not_for_the_old_one(env):
    approved_run(env, executor=[{}])
    _make_plan_legacy(env)
    env.write_plan(_unsize(PLAN_TWO))
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code != 0 and "T2 has no" in out and "T1" not in out.split("plan has problems:", 1)[1], out


def test_submit_help_says_task_size_is_required(env):
    out = env.office("submit", "--help", check=0)
    text = " ".join(str(getattr(out, "stdout", out)).split())
    assert "task_size is required" in text
