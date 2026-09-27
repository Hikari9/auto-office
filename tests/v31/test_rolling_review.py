"""Rolling plan review and authority (docs/v31-rolling-review-gates.md S1-S3, N1-N3, N10-N11)."""
from __future__ import annotations

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, PLAN_TWO

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
CR = ("VERDICT: CHANGES_REQUIRED\n"
      "FINDING P1 | material | T1 | acceptance lacks a negative-number case | add a criterion for add(-1, 1)")
DEFECT = ("VERDICT: PLAN_DEFECT\n"
          "DEFECT P2 | false-contract-assumption | T2 | T2 assumes calc.mul exists | "
          "use mul.py | evidence: calc.py has no mul (calc.py:1)")


def _start(env, plan=PLAN_ONE, **script):
    env.trust()
    env.script(**script)
    code, out = env.office("start", "fixture", "--gear", "express", "--planner", "inline")
    assert code == 0, out
    env.write_plan(plan)
    return env.office("submit")


def _jobs(env, kind):
    con = env.con()
    return con.execute("SELECT COUNT(*) FROM outbox WHERE kind=?", (kind,)).fetchone()[0]


def test_first_pass_launches_with_no_further_review(env):
    code, out = _start(env, plan_reviewer=[{"reply": "VERDICT: PASS"}], executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    assert "plan-review queued" in out, out
    env.office("approve", "plan", "--quote", "ship it", check=0)
    code, data = env.ojson("status")
    assert data["data"]["plan_review"]["ended"] is True
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 0, out
    assert _jobs(env, "plan_review") == 1


def test_first_changes_required_amend_then_launch_before_rereview(env):
    code, out = _start(env, plan_reviewer=[{"reply": CR}, {"reply": "VERDICT: PASS"}])
    env.office("approve", "plan", "--quote", "ship it", check=0)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 4 and "plan-amendment-required" in out, out
    # Amend; keep the re-review queued so we can prove launch did not wait for it.
    env.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(-1, 1) == 0"))
    code, out = env.office("amend", "plan", "--", "add the negative case", env={"OFFICE_JOBS": "manual"})
    assert code == 0 and "rereview queued" in out, out
    code, out = env.office("dispatch", "T1", env={**EXTERNAL, "OFFICE_JOBS": "manual"})
    assert code == 0 and "T1 ->" in out, out
    con = env.con()
    rows = con.execute("SELECT kind, status FROM outbox WHERE kind IN ('plan_review','launch_agent') ORDER BY created_at").fetchall()
    assert [(r["kind"], r["status"]) for r in rows][-2:] == [("plan_review", "queued"), ("launch_agent", "queued")]
    assert con.execute("SELECT requirements_version FROM authorizations WHERE kind='plan'").fetchone()[0] == \
        con.execute("SELECT requirements_version FROM runs").fetchone()[0]  # plan bump kept authorization


def test_plan_review_ends_at_first_pass_even_on_an_older_version(env):
    code, out = _start(env, plan_reviewer=[{"reply": CR}, {"reply": "VERDICT: PASS"}])
    env.office("approve", "plan", "--quote", "go", check=0)
    env.write_plan(PLAN_ONE + "\n")
    env.office("amend", "plan", "--", "tighten acceptance", env={"OFFICE_JOBS": "manual"}, check=0)
    env.office("amend", "plan", "--", "another ordinary tweak", env={"OFFICE_JOBS": "manual"}, check=0)
    code, data = env.ojson("status")  # runs the queued p2 re-review -> PASS
    assert data["data"]["plan_review"]["ended"] is True, data
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM gates WHERE subject='plan'").fetchone()[0] == 2  # no p3 review


def test_initial_plan_defect_launches_nothing_until_independent_clearance(env):
    code, out = _start(env, plan=PLAN_TWO, plan_reviewer=[{"reply": DEFECT}, {"reply": "VERDICT: PASS\nCLEARED P2"}])
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 0, out  # T1 is outside the defect's scope
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 4 and "plan-defect" in out, out
    # There is no orchestrator command that clears a defect.
    code, out = env.office("approve", "clear", "--quote", "trust me")
    assert code == 2
    fixed = PLAN_TWO.replace("### T2: Implement mul", "### T2: Implement mul in mul.py")
    env.write_plan(fixed)
    code, out = env.office("amend", "T2", "--contract", "--", "T2 builds mul.py, not calc.mul")
    assert code == 0, out
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out


def test_plan_wide_defect_blocks_every_dispatch(env):
    wide = DEFECT.replace("| T2 |", "| plan |")
    code, out = _start(env, plan_reviewer=[{"reply": wide}])
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 4 and "plan-defect" in out
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE role='executor'").fetchone()[0] == 0


def test_ordinary_amendment_cannot_change_scope_or_authority(env):
    _start(env, plan_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("approve", "plan", "--quote", "go", check=0)
    env.write_plan(PLAN_ONE.replace("scope: calc.py", "scope: calc.py, utils.py"))
    code, out = env.office("amend", "T1", "--", "let T1 touch utils too")
    assert code == 4 and "contract-level-change" in out and "T1" in out, out
    env.write_plan(PLAN_ONE)
    code, out = env.office("amend", "T1", "--", "then deploy to production")
    assert code == 4 and "authority envelope" in out, out


def test_contract_amendment_adding_an_action_needs_user_authorization(env):
    _start(env, plan_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("approve", "plan", "--quote", "go", check=0)
    env.write_plan(PLAN_ONE.replace("non_goals:", "actions:\n- deploy preview | preconditions: tests pass\nnon_goals:"))
    code, out = env.office("amend", "plan", "--contract", "--", "add a preview deploy")
    assert code == 0 and "need user authorization" in out and "X1" in out, out
    code, data = env.ojson("status")
    assert "X1" in data["next"]
    env.office("approve", "X1", "--quote", "preview deploys are fine", check=0)
    code, data = env.ojson("status")
    assert "X1" not in data["next"]


def test_requirements_change_needs_the_user_and_invalidates_authorization(env):
    _start(env, plan_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("amend", "requirements", "--", "also support floats")
    assert code == 4 and "user-quote-required" in out
    code, out = env.office("amend", "requirements", "--quote", "please support floats too", "--", "support floats")
    assert code == 0 and "authorization" in out
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 4 and "authorization-required" in out


def test_unavailable_plan_reviewer_blocks_dispatch(env):
    _start(env, plan_reviewer=[{"reply": "no idea", "exit": 0}])
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 4 and "plan-review-unavailable" in out, out
    code, out = env.office("approve", "waive", "plan-review", "--quote", "skip the plan review this time")
    assert code == 0
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 0, out


def test_later_defect_pauses_affected_scope_only(env):
    plan = PLAN_TWO.replace("### T2: Implement mul\nscope: mul.py\ndepends: none", "### T2: Implement mul\nscope: mul.py\ndepends: none")
    _start(env, plan=plan, plan_reviewer=[{"reply": CR}, {"reply": DEFECT}])
    env.office("approve", "plan", "--quote", "go", check=0)
    env.write_plan(plan + "\n")
    env.office("amend", "plan", "--", "incorporate P1", env={"OFFICE_JOBS": "manual"}, check=0)
    env.office("dispatch", "T1", "T2", "--parallel", env={**EXTERNAL, "OFFICE_JOBS": "manual"}, check=0)
    code, data = env.ojson("status", env=EXTERNAL)  # the concurrent re-review now returns a defect on T2
    assert data["data"]["tasks"]["T2"] == "paused", data
    assert data["data"]["tasks"]["T1"] in ("running", "launching"), data
