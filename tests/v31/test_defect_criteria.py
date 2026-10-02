"""A plan defect the user can always name, and done criteria the user can drop (#236 B2, #245)."""
from __future__ import annotations

import json

from conftest import PLAN_TWO

REUSED = ("VERDICT: PLAN_DEFECT\n"
          "FINDING P2 | material | T2 | mul reuses calc.add | implement it directly\n"
          "DEFECT P2 | requirement-contradiction | T2 | mul must reuse calc.add, which cannot multiply | "
          "evidence: calc.py:1 adds only")
PLAN_NOTE = PLAN_TWO.replace("- calc.add(2, 3) == 5\n", "- calc.add(2, 3) == 5\n- no production deploy is part of this\n")


def _start(env, plan=PLAN_TWO, approve=True, **script):
    env.trust()
    env.script(**script)
    code, out = env.office("start", "fixture", "--gear", "express", "--planner", "inline")
    assert code == 0, out
    env.write_plan(plan)
    code, out = env.office("submit")
    assert code == 0, out
    if approve:
        env.office("approve", "plan", "--quote", "go", check=0)


def test_defect_reusing_a_finding_code_is_an_open_defect(env):
    _start(env, plan_reviewer=[{"reply": REUSED}])
    con = env.con()
    row = con.execute("SELECT category, state FROM findings WHERE code='P2'").fetchone()
    assert (row["category"], row["state"]) == ("requirement-contradiction", "open")
    code, data = env.ojson("status")
    assert "--redirect P2" in data["next"] and "approve waive P2" in data["next"], data["next"]
    code, out = env.office("approve", "waive", "P2", "--quote", "the reuse stays out")
    assert code == 0, out


def test_requirements_amendment_drops_and_adds_done_criteria(env):
    _start(env, plan_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("amend", "requirements", "--quote", "no mul", "--drop-criterion", "nope", "--", "x")
    assert code == 4 and "unknown-criterion" in out, out
    code, out = env.office("amend", "requirements", "--quote", "only add matters now",
                           "--drop-criterion", "mul exist", "--add-criterion", "add exists", "--", "drop mul")
    assert code == 0, out
    con = env.con()
    frozen = json.loads(con.execute("SELECT frozen_json FROM requirements ORDER BY version DESC LIMIT 1").fetchone()[0])
    assert frozen["done_criteria"] == ["add exists"], frozen
    assert frozen["user_changes"][-1] == "drop mul"
    code, out = env.office("amend", "T1", "--drop-criterion", "add exists", "--", "x")
    assert code == 2 and "criteria-are-requirements" in out, out


def test_plan_reviewer_sees_the_users_requirement_changes():
    from office import briefs
    text = briefs.plan_review_brief({"goal": "g"}, {"version": 3, "body": "plan"},
                                    {"done_criteria": ["a"], "user_changes": ["leave CI disabled"]}, [], True)
    assert "user changes" in text and "- leave CI disabled" in text


def test_removing_a_line_that_names_production_is_an_ordinary_amendment(env):
    _start(env, plan=PLAN_NOTE, plan_reviewer=[{"reply": "VERDICT: PASS"}])
    env.write_plan(PLAN_NOTE.replace("- no production deploy is part of this\n", ""))
    code, out = env.office("amend", "T1", "--", "drop the production deploy note")
    assert code == 0 and "contract-level-change" not in out, out
    env.write_plan(PLAN_TWO.replace("- calc.add(2, 3) == 5\n", "- calc.add(2, 3) == 5\n- deploy to production\n"))
    code, out = env.office("amend", "T1", "--", "note")
    assert code == 4 and "contract-level-change" in out, out
