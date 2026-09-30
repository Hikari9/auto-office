"""Plan-defect redirects: a defect rooted in a requirement goes to the user, whose
answer resets the plan-review budget and picks the next reviewer (office.redirect)."""
from __future__ import annotations

import json

from conftest import PLAN_TWO

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
DEFECT = ("VERDICT: PLAN_DEFECT\n"
          "DEFECT P2 | requirement-contradiction | T2 | mul must reuse calc.add, which cannot multiply floats | "
          "drop the reuse requirement | evidence: calc.py:1 adds ints only")
CLEARED = "VERDICT: PASS\nCLEARED P2"
FIXED = PLAN_TWO.replace("### T2: Implement mul", "### T2: Implement mul directly")
REDIRECT = ["--redirect", "P2", "--root-cause", "done criterion: mul reuses calc.add",
            "--quote", "drop the reuse, implement mul directly"]


def _start(env, **script):
    env.trust()
    env.script(**script)
    code, out = env.office("start", "fixture", "--gear", "express", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_TWO)
    code, out = env.office("submit")
    assert code == 0, out
    env.office("approve", "plan", "--quote", "go", check=0)


def _plan_review(env) -> dict:
    code, data = env.ojson("inspect", "run")
    con = env.con()
    return json.loads(con.execute("SELECT plan_review_json FROM runs").fetchone()[0] or "{}")


def _gates(env):
    return [dict(r) for r in env.con().execute(
        "SELECT id, plan_version, round, escalated, status, verdict, route FROM gates WHERE subject='plan' "
        "ORDER BY created_at").fetchall()]


def _briefs(env, gate_id):
    con = env.con()
    rows = con.execute("SELECT id FROM dispatches WHERE gate_id=?", (gate_id,)).fetchall()
    return [p.read_text() for r in rows for p in env.tmp.rglob(f"dispatches/{r['id']}/brief.md")]


def test_next_action_sends_a_requirement_defect_to_the_user(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}])
    code, data = env.ojson("status")
    nxt = data["next"]
    assert "ask the user" in nxt and "--redirect P2" in nxt and "approve waive P2" in nxt, nxt


def test_redirect_records_the_requirement_resets_the_budget_and_rereviews(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}, {"reply": CLEARED}])
    env.write_plan(FIXED)
    code, out = env.office("amend", "T2", "--contract", *REDIRECT, "--requirement", "mul need not reuse calc.add",
                           "--", "T2 implements mul directly")
    assert code == 0, out
    con = env.con()
    version = con.execute("SELECT requirements_version FROM runs").fetchone()[0]
    assert f"requirements r{version} recorded and authorized by the redirect" in out, out
    assert "review budget reset" in out, out
    auth = con.execute("SELECT quote FROM authorizations WHERE kind='plan' AND requirements_version=?", (version,)).fetchone()
    assert auth["quote"] == "drop the reuse, implement mul directly"
    gates = _gates(env)
    assert len(gates) == 2 and gates[1]["round"] == 1 and gates[1]["verdict"] == "PASS", gates
    assert any("USER REDIRECT P2" in b and "drop the reuse" in b for b in _briefs(env, gates[1]["id"]))
    pr = _plan_review(env)
    assert pr["round_base"] == 1 and pr["redirects"][0]["root_cause"].startswith("done criterion")
    # Cleared by the reviewer, and r2 needs no second authorization.
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out


def test_redirect_refills_a_spent_budget_and_escalation(env):
    # express: 2 rounds, then one escalation. Spend all three on the same defect.
    _start(env, plan_reviewer=[{"reply": DEFECT}, {"reply": DEFECT}, {"reply": DEFECT}, {"reply": CLEARED}])
    env.write_plan(PLAN_TWO.replace("### T2: Implement mul", "### T2: Implement mul v2"))
    env.office("amend", "T2", "--contract", "--", "try again", check=0)
    env.ojson("status")
    con = env.con()
    assert con.execute("SELECT escalations_used FROM runs").fetchone()[0] == 1
    assert len(_gates(env)) == 3  # two rounds plus the escalation
    env.write_plan(FIXED)
    code, out = env.office("amend", "T2", "--contract", *REDIRECT, "--", "T2 implements mul directly")
    assert code == 0, out
    assert "requirements r" not in out  # an assumption-only redirect keeps r1
    gates = _gates(env)
    assert len(gates) == 4 and gates[3]["escalated"] == 0 and gates[3]["round"] == 1, gates
    assert env.con().execute("SELECT escalations_used FROM runs").fetchone()[0] == 0
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out


def test_fresh_reviewer_excludes_the_route_that_raised_the_defect(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}, {"reply": CLEARED}])
    first = _gates(env)[0]["route"]
    env.write_plan(FIXED)
    env.office("amend", "T2", "--contract", *REDIRECT, "--", "fix", env={"OFFICE_JOBS": "manual"}, check=0)
    row = env.con().execute("SELECT payload_json FROM outbox WHERE kind='plan_review' ORDER BY created_at DESC").fetchone()
    payload = json.loads(row[0])
    assert first in payload["exclude"] and "resume_from" not in payload, payload


def test_same_reviewer_resumes_or_falls_back_to_its_route(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}, {"reply": CLEARED}])
    first = _gates(env)[0]
    env.write_plan(FIXED)
    code, out = env.office("amend", "T2", "--contract", *REDIRECT, "--reviewer", "same", "--", "fix")
    assert code == 0, out
    con = env.con()
    origin = con.execute("SELECT id FROM dispatches WHERE gate_id=?", (first["id"],)).fetchone()["id"]
    events = [r[0] for r in con.execute("SELECT summary FROM events WHERE kind='review.resume_fallback'")]
    # No herdr here, so the session cannot be resumed: a fresh session runs on the same route.
    assert any(origin in e for e in events), events
    assert _gates(env)[1]["route"] == first["route"]


def test_planner_submit_can_carry_the_redirect(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}, {"reply": CLEARED}])
    env.write_plan(FIXED)
    code, out = env.office("submit", *REDIRECT)
    assert code == 0 and "defect P2 redirected" in out, out
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out


def test_user_waives_a_defect_they_judge_wrong(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}])
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 4 and "plan-defect" in out, out
    code, out = env.office("approve", "waive", "P2", "--quote", "calc.add is fine, keep it",
                           "--root-cause", "the reviewer misread calc.add")
    assert code == 0 and "P2 waived" in out, out
    assert env.con().execute("SELECT state FROM findings WHERE code='P2'").fetchone()[0] == "waived"
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0, out


def test_redirect_refusals(env):
    _start(env, plan_reviewer=[{"reply": DEFECT}])
    env.write_plan(FIXED)
    code, out = env.office("amend", "T2", *REDIRECT, "--", "fix")
    assert code == 2 and "redirect-needs-contract" in out, out
    code, out = env.office("amend", "T2", "--contract", "--redirect", "P9", "--root-cause", "x", "--quote", "y", "--", "fix")
    assert code == 4 and "no-open-defect" in out, out
    code, out = env.office("amend", "T2", "--contract", "--redirect", "P2", "--quote", "y", "--", "fix")
    assert code == 2 and "root-cause-required" in out, out
    code, out = env.office("amend", "T2", "--contract", "--redirect", "P2", "--root-cause", "x", "--", "fix")
    assert code == 2 and "user-quote-required" in out, out
    assert len(_gates(env)) == 1  # nothing was written
