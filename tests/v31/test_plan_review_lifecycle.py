"""Plan review reviews the initial plan only (#418).

The initial cycle may iterate through RECHECK rounds up to its cap (3, or the
user's `office start --plan-review-rounds N`). Once it closes (APPROVED, the cap
spent, or waived), no amendment, resume or late result reopens it; only the
user's explicit `office review plan --quote ...` opens another, bounded cycle.

Jobs run manually here, so every plan-review round stays queued until the test
records its result through `plans.ingest_plan_review`: what queues a reviewer
is observed directly. No agent runs, except the dedicated-planner test, which
uses the scripted in-process fakes.
"""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE, PLAN_TWO

MANUAL = {"OFFICE_JOBS": "manual"}
GEAR = "express"
APPROVED = "VERDICT: APPROVED\nNEXT proceed"


def recheck(code="P1", where="T1", what="the contract is unclear"):
    return f"VERDICT: RECHECK\nFINDING {code} | medium | blocking | {where} | {what} | fix it\nNEXT revise"


def _o(env, *args, check=None, extra_env=None):
    return env.office(*args, check=check, env={**MANUAL, **(extra_env or {})})


def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _run(env):
    row = _q(env, "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")[0]
    for k in ("gates", "plan_review"):
        row[k] = json.loads(row[f"{k}_json"] or "{}")
    return row


def _plan_gates(env):
    return _q(env, "SELECT * FROM gates WHERE kind='plan_review' ORDER BY created_at")


def _queued(env):
    return [g for g in _plan_gates(env) if g["status"] in ("queued", "running")]


def _events(env, kind):
    return _q(env, "SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))


def _start(env, plan=PLAN_ONE, extra=()):
    env.trust()
    _o(env, "start", "fixture goal", "--gear", GEAR, "--planner", "inline", *extra, check=0)
    env.write_plan(plan)
    _o(env, "submit", check=0)


def _ingest(env, reply: str | None = None, *, status: str = "COMPLETED", gate_id: str | None = None):
    """Record one plan-review result on the newest open round (or `gate_id`), as the job would."""
    from office import db, plans, review_parse, state
    con = env.con()
    try:
        run = state.get_run(con, _run(env)["id"])
        gid = gate_id or [g for g in plans.plan_gates(con, run["id"]) if g["status"] in ("queued", "running")][-1]["id"]
        parsed = review_parse.parse(reply, plan_review=True, contract="convergence-v1") if reply else None
        outcome = {"status": status, "verdict": parsed.verdict if parsed else None, "parsed": parsed,
                   "route": "fake/reviewer", "dispatch_id": None, "summary": (reply or status)[:80]}
        with db.transaction(con):
            plans.ingest_plan_review(con, run, gid, outcome)
    finally:
        con.close()


def _revise(env, plan: str, scope="plan", contract=True, check=0):
    env.write_plan(plan)
    args = ["amend", scope] + (["--contract"] if contract else []) + ["--", "revision"]
    return _o(env, *args, check=check)


def _approved_run(env, plan=PLAN_TWO):
    _start(env, plan=plan)
    _ingest(env, APPROVED)
    _o(env, "approve", "plan", "--quote", "go", check=0)
    pr = _run(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "approved", pr
    assert len(_plan_gates(env)) == 1


# ------------------------------------------------------------------ the initial cycle

def test_initial_review_iterates_through_recheck_then_closes_on_approved(env):
    """1, 2: the funded initial review queues; a RECHECK revision queues the next round."""
    _start(env)
    gates = _queued(env)
    assert len(gates) == 1 and gates[0]["round"] == 1 and gates[0]["cycle"] == 1
    assert _run(env)["plan_review"]["lifecycle"] == "initial"
    _ingest(env, recheck())
    assert _run(env)["plan_review"]["status"] == "recheck"
    _revise(env, PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"),
            scope="T1")
    gates = _queued(env)
    assert len(gates) == 1 and gates[0]["round"] == 2 and gates[0]["plan_version"] == 2
    _ingest(env, APPROVED)
    pr = _run(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "approved"
    assert pr["history"] == [{**pr["history"][0], "cycle": 1, "lifecycle": "initial", "outcome": "approved",
                              "rounds": 2, "max_rounds": 3}]


def test_default_cap_is_three_substantive_rounds_and_runtime_failures_spend_none(env):
    """3, 4, 5: UNAVAILABLE and INVALID_RESULT spend no round; the third RECHECK closes the
    cycle unapproved, for the orchestrator; no fourth reviewer is queued."""
    _start(env)
    _ingest(env, status="UNAVAILABLE")
    assert _run(env)["plan_review"]["status"] == "unavailable"
    _o(env, "resume", check=0)
    assert [g["round"] for g in _queued(env)] == [1], "the retry is still round 1"
    _ingest(env, recheck())
    _revise(env, PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (rev 1)"))
    _ingest(env, status="INVALID_RESULT")
    assert _run(env)["plan_review"]["status"] == "attention"
    _o(env, "resume", check=0)
    assert [g["round"] for g in _queued(env)] == [2], "an unreadable reply spent no round"
    _ingest(env, recheck())
    _revise(env, PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (rev 2)"))
    assert [g["round"] for g in _queued(env)] == [3]
    _ingest(env, recheck())
    pr = _run(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "budget_exhausted_orchestrator_owned", pr
    completed = [g for g in _plan_gates(env) if g["review_status"] == "COMPLETED"]
    assert [g["round"] for g in completed] == [1, 2, 3] and all(g["verdict"] == "RECHECK" for g in completed)
    assert pr["outstanding"]["remaining"][0]["code"] == "P1"
    assert len(_events(env, "plan.budget_exhausted")) == 1
    _revise(env, PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (rev 9)"))
    assert not _queued(env), "no fourth reviewer"
    assert not _q(env, "SELECT 1 FROM authorizations WHERE target LIKE 'plan:%'"), "no user decision was needed"


def test_orchestrator_dispositions_release_the_findings_it_owns(env):
    """Issue #418 contract 3: at the cap the orchestrator records a disposition per finding;
    the RECHECK verdict stands and no APPROVED receipt appears."""
    _start(env, plan=PLAN_TWO, extra=("--plan-review-rounds", "1"))
    _ingest(env, recheck(where="T2"))
    _o(env, "approve", "plan", "--quote", "go", check=0)
    code, out = _o(env, "dispatch", "T2")
    assert code == 4 and "plan-recheck" in out, out
    code, data = env.ojson("status", env=MANUAL)
    assert "office disposition plan:" in data["next"], data["next"]
    _o(env, "disposition", "plan:P1", "dismissed", "--", "T2 contract is explicit enough; accepted", check=0)
    row = _q(env, "SELECT * FROM findings WHERE code='P1'")[0]
    assert row["disposition"] == "dismissed" and row["disposition_by"] == "orchestrator" and row["blocking"] == 1
    _o(env, "dispatch", "T2", check=0)
    assert [g["verdict"] for g in _plan_gates(env)] == ["RECHECK"]
    assert _run(env)["plan_review"]["status"] == "budget_exhausted_orchestrator_owned"


def test_user_round_override_is_pinned_and_survives_resume(env):
    """6: the user's cap is pinned in the run's gate policy and enforced after resume."""
    env.trust()
    code, out = _o(env, "start", "fixture goal", "--gear", GEAR, "--planner", "inline", "--plan-review-rounds", "0")
    assert code == 2 and "1 to 10" in out, out
    code, out = _o(env, "start", "fixture goal", "--gear", "direct+review", "--planner", "inline",
                   "--plan-review-rounds", "2")
    assert code == 2 and "funds no plan review" in out, out
    _start(env, extra=("--plan-review-rounds", "2"))
    gates = _run(env)["gates"]
    assert gates["plan_review_max_rounds"] == 2 and gates["plan_review_rounds_by"] == "user"
    assert len(_events(env, "plan.review_rounds")) == 1
    _o(env, "resume", check=0)
    _ingest(env, recheck())
    _revise(env, PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (rev)"))
    _o(env, "resume", check=0)
    _ingest(env, recheck())
    pr = _run(env)["plan_review"]
    assert pr["status"] == "budget_exhausted_orchestrator_owned" and pr["history"][-1]["max_rounds"] == 2


# ------------------------------------------------------------------ no review after the cycle closes

PLAN_LANES = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nlane: core\n")
SEAM_EDITS = {
    "dependency": PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1"),
    "scope": PLAN_TWO.replace("scope: mul.py", "scope: mul.py, mul_helpers.py"),
    "interface": PLAN_TWO.replace("scope: mul.py\n", "scope: mul.py\ninterfaces: mul(a, b) -> int\n"),
    "acceptance": PLAN_TWO.replace("- mul.mul(2, 3) == 6", "- mul.mul(2, 3) == 6\n- mul.mul(0, 1) == 0"),
    "lane": PLAN_LANES,
    "converge": PLAN_TWO.replace("scope: mul.py\n", "scope: mul.py\nconverge: arithmetic\n").replace(
        "scope: calc.py\n", "scope: calc.py\nconverge: arithmetic\n"),
    "task added": PLAN_TWO + "\n### T3: Docs\nscope: DOCS.md\ndepends: none\nchecks: none\naccept:\n- documented\nvisual: none\n",
    "task removed": PLAN_ONE,
}


def test_ordinary_amendment_after_approval_queues_no_plan_review(env):
    """7: an ordinary amendment after the initial review closes is never reviewed."""
    _approved_run(env)
    code, out = _o(env, "amend", "plan", "--", "reword the docstring", check=0)
    assert "rereview" not in out and len(_plan_gates(env)) == 1, out
    assert len(_events(env, "plan.review_closed")) == 1


@pytest.mark.parametrize("seam", sorted(SEAM_EDITS))
def test_contract_amendment_after_approval_queues_no_plan_review(env, seam):
    """8, 9, 10, 12: dependency, scope, interface, acceptance, lane, converge and task-topology
    changes after APPROVED get no plan reviewer, and still version the tasks they change."""
    _approved_run(env)
    code, out = _revise(env, SEAM_EDITS[seam])
    assert len(_plan_gates(env)) == 1 and not _queued(env), (seam, out)
    run = _run(env)
    assert run["plan_version"] == 2 and run["plan_review"]["status"] == "approved"
    assert not _events(env, "plan.rereview")
    tasks = {t["id"]: t for t in _q(env, "SELECT * FROM tasks")}
    if seam in ("dependency", "scope", "interface", "acceptance"):
        assert tasks["T2"]["contract_version"] == 2, "the amendment still versions T2's contract"
    if seam == "task removed":
        assert tasks["T2"]["status"] == "cancelled"
    if seam == "task added":
        assert tasks["T3"]["introduced_plan_version"] == 2


def test_requirements_amendment_after_approval_needs_the_user_but_no_plan_review(env):
    """11, 13: requirements stay user-owned (quote, then authorization); the revised plan
    that follows is not reviewed again."""
    _approved_run(env)
    code, out = _o(env, "amend", "requirements", "--", "mul must handle negatives")
    assert code != 0 and "user-quote-required" in out, out
    _o(env, "amend", "requirements", "--quote", "handle negatives too", "--", "mul must handle negatives", check=0)
    code, out = _o(env, "dispatch", "T1")
    assert code == 4 and "authorization-required" in out, out
    _revise(env, PLAN_TWO.replace("- mul.mul(2, 3) == 6", "- mul.mul(2, 3) == 6\n- mul.mul(-2, 3) == -6"))
    assert len(_plan_gates(env)) == 1 and not _queued(env)
    code, out = _o(env, "amend", "plan", "--", "deploy to production after merge")
    assert code == 4 and "contract-level-change" in out, "authority changes are still refused as ordinary"
    _o(env, "approve", "plan", "--quote", "authorized", check=0)
    _o(env, "dispatch", "T1", check=0)


def test_dedicated_planner_revision_after_approval_is_not_reviewed(env):
    """The dedicated-planner path: a contract amendment goes to the planner, whose revised
    plan arrives through submit and is owned by it, not reviewed again."""
    env.trust()
    revised = PLAN_ONE.replace("scope: calc.py", "scope: calc.py, calc_helpers.py")
    env.script(planner=[{"plan": PLAN_ONE, "submit": True}, {"plan": revised, "submit": True}],
               plan_reviewer=[{"reply": APPROVED}])
    env.office("start", "add numbers", "--gear", "full", check=0)
    assert _run(env)["plan_review"]["status"] == "approved"
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("amend", "T1", "--contract", "--", "widen the envelope", check=0)
    run = _run(env)
    assert run["plan_version"] == 2, run["plan_version"]
    assert len(_plan_gates(env)) == 1
    assert [c["role"] for c in env.calls()] == ["planner", "plan_reviewer", "planner"]


# ------------------------------------------------------------------ explicit review, resume, late results

def test_user_requested_review_is_explicit_bounded_and_labelled(env):
    """14: only the user reopens plan review, as a new cycle recorded with their words."""
    _approved_run(env)
    code, out = _o(env, "review", "plan")
    assert code == 2 and "user-quote-required" in out, out
    code, out = _o(env, "review", "plan", "--quote", "x", extra_env={"OFFICE_DISPATCH_ID": "D1"})
    assert code != 0 and "worker-cannot-request-review" in out, out
    _o(env, "review", "plan", "--quote", "please review the plan again", "--rounds", "2", check=0)
    pr = _run(env)["plan_review"]
    assert pr["lifecycle"] == "user-requested" and pr["max_rounds"] == 2 and pr["cycle"] == 2 and not pr["ended"]
    assert _q(env, "SELECT quote FROM authorizations WHERE target='plan:review'")[0]["quote"] == \
        "please review the plan again"
    gates = _queued(env)
    assert len(gates) == 1 and gates[0]["cycle"] == 2 and gates[0]["round"] == 1
    code, out = _o(env, "review", "plan", "--quote", "again")
    assert code == 4 and "plan-review-open" in out, out
    _ingest(env, recheck(where="T2"))
    _revise(env, PLAN_TWO.replace("- mul.mul(2, 3) == 6", "- mul.mul(2, 3) == 6\n- mul.mul(0, 1) == 0"))
    assert [g["round"] for g in _queued(env)] == [2], "RECHECK iterates inside the requested cycle"
    _ingest(env, recheck(where="T2"))
    pr = _run(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "budget_exhausted_orchestrator_owned"
    assert [(h["lifecycle"], h["outcome"]) for h in pr["history"]] == [
        ("initial", "approved"), ("user-requested", "budget_exhausted_orchestrator_owned")]
    assert len(_events(env, "plan.review_requested")) == 1


def test_resume_and_a_late_result_cannot_reopen_a_closed_review(env):
    """15, 16: closing cancels a round still queued; its late result is kept, not applied;
    resume queues nothing; the stored verdicts are untouched."""
    _start(env, plan=PLAN_TWO)
    first = _queued(env)[0]["id"]
    _o(env, "amend", "plan", "--", "reword the docstring", check=0)
    second = [g["id"] for g in _queued(env) if g["id"] != first]
    assert len(second) == 1, "the open cycle reviews the revision"
    _ingest(env, APPROVED, gate_id=first)
    gates = {g["id"]: g for g in _plan_gates(env)}
    assert gates[second[0]]["status"] == "cancelled"
    _ingest(env, recheck(where="T2"), gate_id=second[0])
    assert len(_events(env, "plan.late_result")) == 1
    pr = _run(env)["plan_review"]
    assert pr["status"] == "approved" and pr["ended"]
    assert not _q(env, "SELECT 1 FROM findings WHERE gate_kind='plan_review' AND state='open'")
    con = env.con()
    with con:
        con.execute("UPDATE runs SET plan_review_json=?", (json.dumps({**pr, "status": "unavailable"}),))
    con.close()
    _o(env, "resume", check=0)
    assert not _queued(env), "resume cannot resurrect a closed review"
    after = {g["id"]: g for g in _plan_gates(env)}
    assert after[first]["verdict"] == "APPROVED" and after[second[0]]["status"] == "cancelled"


def test_a_33_run_mid_plan_decision_is_not_moved_to_34(env):
    """Compatibility: a 3.3 run waiting on an operator plan decision, or re-reviewing after
    APPROVED, finishes that on 3.3; a closed or initial review crosses as is."""
    from office import state, upgrade
    _start(env)
    con = env.con()
    try:
        run = state.get_run(con, _run(env)["id"])
        assert upgrade._plan_review_blocker(con, run, "3.4") is None, "an initial review crosses"
        run["plan_review"] = {**run["plan_review"], "status": "escalated"}
        assert "escalated" in upgrade._plan_review_blocker(con, run, "3.4")
        assert upgrade._plan_review_blocker(con, run, "3.3") is None
    finally:
        con.close()
    _ingest(env, APPROVED)
    con = env.con()
    try:
        run = state.get_run(con, _run(env)["id"])
        assert upgrade._plan_review_blocker(con, run, "3.4") is None, "a closed review crosses"
        run["plan_review"] = {**run["plan_review"], "ended": False, "status": "recheck"}
        assert "reopened after APPROVED" in upgrade._plan_review_blocker(con, run, "3.4")
    finally:
        con.close()
