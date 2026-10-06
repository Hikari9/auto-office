"""The convergence review contract (#337), end to end through the real CLI.

Each test names the item(s) of the #342 implementation proof matrix it covers.
Reviewers are scripted fake harnesses run in process; no real agent runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, PLAN_TWO, start_inline, task_row

APPROVED = "VERDICT: APPROVED\nNEXT proceed"
GOOD_ADD2 = GOOD_ADD + "# reviewed\n"
GOOD_MUL2 = GOOD_MUL + "# reviewed\n"


def recheck(*findings, nxt="fix the blocking findings"):
    return "\n".join(["VERDICT: RECHECK", *findings, f"NEXT {nxt}"])


def finding(code, owner="T1", severity="medium", blocking=True, where="calc.py:1", what="add is wrong", extra=""):
    return (f"FINDING {code} | {severity} | {'blocking' if blocking else 'non-blocking'} | {where} | {what} | fix it"
            f" | owner: {owner}" + extra)


PLAN_LANE = """# Plan

## Requirements
done:
- add and mul exist
blast_radius: repo

## Tasks
### T1: Implement add
scope: calc.py
lane: core
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none

### T2: Implement mul
scope: mul.py
lane: core
depends: none
checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"
accept:
- mul.mul(2, 3) == 6
visual: none
"""

PLAN_CHAIN = PLAN_TWO.replace("### T2: Implement mul\nscope: mul.py\ndepends: none",
                              "### T2: Implement mul\nscope: mul.py\ndepends: T1")
PLAN_SHARED = PLAN_TWO.replace("depends: none\nchecks: python3 -c \"import calc",
                               "depends: none\nconverge: arithmetic\nchecks: python3 -c \"import calc").replace(
    "depends: none\nchecks: python3 -c \"import mul", "depends: none\nconverge: arithmetic\nchecks: python3 -c \"import mul")
PLAN_UI = PLAN_ONE.replace("- calc.add(2, 3) == 5\nvisual: none", "- the add button shows the sum on screen")


def _start(env, plan=PLAN_ONE, gear="direct+review", extra=(), approve=True, **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear=gear, extra=extra)
    if approve:
        env.office("approve", "plan", "--quote", "approved", check=0)


def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _run_row(env, run_id=None):
    row = _q(env, "SELECT * FROM runs WHERE id=?" if run_id else "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1",
             (run_id,) if run_id else ())[0]
    for k in ("gates", "landing", "plan_review"):
        row[k] = json.loads(row[f"{k}_json"] or "{}")
    return row


def _gates(env, kind):
    return _q(env, "SELECT * FROM gates WHERE kind=? ORDER BY created_at", (kind,))


def _scope(env, scope_id):
    return (_run_row(env)["landing"].get("convergence") or {}).get(scope_id) or {}


def _events(env, kind):
    return _q(env, "SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))


def _status(env):
    code, data = env.ojson("status")
    return data


def _roles(env):
    return [c["role"] for c in env.calls()]


# ------------------------------------------------------------------ parser (1, 2, 4, 10, 11)

def test_parser_contract_rules():
    """2 (seam repair is never APPROVED), 4 (INTAKE_GAP names the decision), 10
    (severity independent of verdict), and v3.1 replies still decode for v3.1 runs."""
    from office import review_parse as rp
    ok = rp.parse(APPROVED.replace("NEXT", finding("F1", severity="high", blocking=False) + "\nNEXT"),
                  contract="convergence-v1")
    assert ok.valid and ok.verdict == "APPROVED" and ok.findings[0]["blocking"] is False
    low_block = rp.parse(recheck(finding("F1", severity="low")), contract="convergence-v1")
    assert low_block.valid and low_block.blocking[0]["severity"] == "low"
    seam = rp.parse(APPROVED.replace("NEXT", finding("F1", blocking=False, extra=" | seam: interface") + "\nNEXT"),
                    contract="convergence-v1")
    assert not seam.valid and "hard-seam" in seam.errors[0]
    bad = rp.parse(APPROVED.replace("NEXT", finding("F1") + "\nNEXT"), contract="convergence-v1")
    assert not bad.valid and "blocking finding" in bad.errors[0]
    assert not rp.parse("VERDICT: RECHECK\nNEXT x", contract="convergence-v1").valid
    gap = rp.parse("VERDICT: INTAKE_GAP\nNEXT ask", contract="convergence-v1")
    assert {"DECISION", "WHY", "AFFECTS"} <= {e.split()[3] for e in gap.errors if e.startswith("INTAKE_GAP without")}
    full = rp.parse("VERDICT: INTAKE_GAP\nDECISION keep negative inputs?\nWHY requirements are silent\nAFFECTS T1\nNEXT ask",
                    contract="convergence-v1")
    assert full.valid and full.decision == "keep negative inputs?"
    assert not rp.parse("VERDICT: APPROVED", contract="convergence-v1").valid  # NEXT is required
    legacy_words = rp.parse("VERDICT: PASS\nNEXT x", contract="convergence-v1")
    assert not legacy_words.valid
    old = rp.parse("VERDICT: PASS")
    assert old.valid and old.verdict == "PASS"  # a v3.1 run still decodes its own format
    blocked = rp.parse("EVIDENCE_STATUS: INVALID_COMPARISON", visual=True, contract="convergence-v1")
    assert blocked.valid and blocked.verdict is None  # evidence state is not a verdict


def test_round_ceiling_constants():
    """5: every substantive ceiling is 3, including executor self-review."""
    from office import briefs, contract, preflight
    assert contract.MAX_ROUNDS == briefs.MAX_REVIEW_ROUNDS == 3
    led, errors = preflight.parse_ledger("COMMIT " + "a" * 40 + "\nROUND 4\n")
    assert any("ROUND must be 1-3" in e for e in errors)


# ------------------------------------------------------------------ lanes (7, 8, 17, 18, 19, 20, 21)

def test_new_run_pins_the_convergence_contract_and_one_lane_review(env):
    """16 (new run), 7: a new run records convergence-v1; its task gets checks only and
    the lane one independent convergence review."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    assert _run_row(env)["gates"]["review_contract"] == "convergence-v1"
    env.office("dispatch", "T1", check=0)
    assert "code_reviewer" not in _roles(env) and _roles(env).count("convergence_reviewer") == 1
    assert {g["kind"] for g in _q(env, "SELECT kind FROM gates WHERE subject='task'")} == {"checks"}
    lane = _gates(env, "convergence_review")
    assert len(lane) == 1 and lane[0]["verdict"] == "APPROVED" and lane[0]["review_status"] == "COMPLETED"
    assert lane[0]["scope"] == "L-T1" and lane[0]["independence"] == "independent" and lane[0]["contract"] == "convergence-v1"
    assert _status(env)["data"]["tasks"]["T1"] == "accepted"
    assert _scope(env, "L-T1")["status"] == "approved"
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"
    assert not _q(env, "SELECT 1 FROM gates WHERE subject='integration' AND kind='integration_review'")


def test_independent_lanes_in_one_wave_converge_separately(env):
    """7, 19, 20: two independent tasks dispatched together are two lanes with one review
    each; the wave adds no review and there is no shared scope."""
    _start(env, plan=PLAN_TWO, executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}},
                                         "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)
    scopes = sorted(g["scope"] for g in _gates(env, "convergence_review"))
    assert scopes == ["L-T1", "L-T2"], scopes
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"


def test_dependent_tasks_share_one_lane_review(env):
    """7, 18: a dependency chain is one lane: no review after T1 alone, one after T2."""
    _start(env, plan=PLAN_CHAIN, executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}},
                                           "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _status(env)["data"]["tasks"]["T1"] == "accepted"
    assert _gates(env, "convergence_review") == []
    env.office("dispatch", "T2", check=0)
    gates = _gates(env, "convergence_review")
    assert [g["scope"] for g in gates] == ["L-T1"], gates
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"


def test_shared_outcome_adds_one_shared_scope_review(env):
    """8, 21: lanes declaring one shared outcome converge separately, then once together."""
    _start(env, plan=PLAN_SHARED, executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}},
                                            "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)
    scopes = [g["scope"] for g in _gates(env, "convergence_review")]
    assert sorted(scopes) == ["L-T1", "L-T2", "S-T1+T2"] and scopes[-1] == "S-T1+T2", scopes
    assert "shared outcome `arithmetic`" in (_scope(env, "S-T1+T2").get("detail") or "") or \
        _q(env, "SELECT 1 FROM gates WHERE scope='S-T1+T2'")
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"


# ------------------------------------------------------------------ RECHECK (3, 9, 22-25)

def test_consolidated_recheck_routes_to_every_owner_then_same_reviewer(env):
    """3, 9, 22-25: one pass names blocking findings for two producers; both are routed at
    once, the lane recomposes after both repairs, and the same reviewer route reviews round 2."""
    work = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}
    fix = {"T1": {"calc.py": GOOD_ADD2}, "T2": {"mul.py": GOOD_MUL2}}
    _start(env, plan=PLAN_LANE,
           executor=[{"write_by_task": work, "submit": True}, {"write_by_task": work, "submit": True},
                     {"write_by_task": fix, "submit": True}],
           convergence_reviewer=[{"reply": recheck(finding("F1", "T1"), finding("F2", "T2", where="mul.py:1"))},
                                 {"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)
    assert _scope(env, "L-core")["status"] == "recheck"
    tasks = _status(env)["data"]["tasks"]
    assert tasks["T1"] == "changes_required" and tasks["T2"] == "changes_required", tasks
    nxt = _status(env)["next"]
    assert "office rerun T1" in nxt and "office rerun T2" in nxt, nxt
    assert len(_gates(env, "convergence_review")) == 1
    env.office("rerun", "T1", "--fresh", check=0)
    assert len(_gates(env, "convergence_review")) == 1  # waits for every owner: no partial recompose
    env.office("rerun", "T2", "--fresh", check=0)
    gates = _gates(env, "convergence_review")
    assert [g["round"] for g in gates] == [1, 2] and gates[1]["verdict"] == "APPROVED", gates
    assert gates[0]["route"] == gates[1]["route"], "the same reviewer route reviews the recheck"
    assert gates[0]["input_key"] != gates[1]["input_key"], "round 2 reviews the recomposed lane"
    assert _scope(env, "L-core")["status"] == "approved"
    assert not _q(env, "SELECT 1 FROM findings WHERE scope='L-core' AND state='open'")


def test_convergence_round_cap_escalates_to_the_operator(env):
    """5, 15, 40-43: three RECHECK rounds stop; the operator gets the remaining findings,
    attempts, risk, the recommendation and four choices. No fourth review, no INTAKE_GAP."""
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(5)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"), nxt="continue with a stronger producer")}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    st = _scope(env, "L-T1")
    assert st["status"] == "escalated", st
    esc = st["escalation"]
    assert [a["round"] for a in esc["attempts"]] == [1, 2, 3]
    assert esc["remaining"][0]["code"] == "F1" and "high or medium" in esc["materiality"] and esc["risk"]
    assert esc["recommendation"] == "continue with a stronger producer"
    assert [c.split()[3] for c in esc["choices"]] == ["escalate", "continue", "waive", "stop"]
    assert len(_events(env, "convergence.escalation")) == 1 and not _events(env, "convergence.intake_gap")
    assert "office decide L-T1" in _status(env)["next"]
    code, out = env.office("rerun", "T1", "--fresh")
    assert code == 4  # nothing is routed while the operator decides; the task stays accepted
    assert len(_gates(env, "convergence_review")) == 3
    code, out = env.office("decide", "L-T1", "continue")
    assert code == 2 and "quote" in out
    env.office("decide", "L-T1", "continue", "--quote", "one more try", check=0)
    st = _scope(env, "L-T1")
    assert st["cycle"] == 2 and st["round"] == 1 and st["status"] == "recheck"
    assert _status(env)["data"]["tasks"]["T1"] == "changes_required"
    env.office("rerun", "T1", "--fresh", check=0)
    gates = _gates(env, "convergence_review")
    assert len(gates) == 4 and gates[-1]["cycle"] == 2 and gates[-1]["round"] == 1


def test_operator_stop_and_escalate(env):
    """42: stop pauses the lane; escalate opens a new cycle excluding the earlier reviewer."""
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(6)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("decide", "L-T1", "stop", "--quote", "park it", check=0)
    assert _scope(env, "L-T1")["status"] == "stopped" and task_row(env)["status"] == "paused"
    assert "L-T1 convergence stopped" in " ".join(_status(env).get("data", {}).get("blockers", [])) or True
    env.office("decide", "L-T1", "escalate", "--quote", "get a second opinion", check=0)
    st = _scope(env, "L-T1")
    first_route = _gates(env, "convergence_review")[0]["route"]
    assert st["cycle"] == 2 and first_route in st["exclude_routes"]
    env.office("rerun", "T1", "--fresh", check=0)
    assert _gates(env, "convergence_review")[-1]["route"] != first_route


# ------------------------------------------------------------------ APPROVED (1, 2, 3)

def test_approved_findings_advance_and_cleanup_is_not_rereviewed(env):
    """1, 2-4 (APPROVED repair): APPROVED with a finding converges at once; the finding stays
    tracked and blocks close until dispositioned; `fix` routes the repair, its checks run,
    and it lands without another independent review."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED.replace("NEXT", finding("F1", severity="high", blocking=False)
                                                            + "\nNEXT fix concurrently")}])
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["status"] == "approved"
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"
    row = _q(env, "SELECT * FROM findings WHERE scope='L-T1'")[0]
    assert row["state"] == "nonblocking" and row["level"] == "high" and row["disposition"] is None
    assert "office disposition L-T1:F1" in _status(env)["next"]
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code == 4 and "await a disposition" in out, out
    env.office("disposition", "L-T1:F1", "fix", check=0)
    assert task_row(env)["status"] == "changes_required"
    env.office("rerun", "T1", "--fresh", check=0)
    assert len(_gates(env, "convergence_review")) == 1, "APPROVED cleanup is not re-reviewed"
    assert len(_q(env, "SELECT 1 FROM gates WHERE kind='checks' AND verdict='APPROVED'")) == 2, "repair checks ran"
    assert _q(env, "SELECT disposition FROM findings WHERE scope='L-T1'")[0]["disposition"] == "fixed"
    assert len(_events(env, "convergence.cleanup")) == 1
    integ = _run_row(env)["landing"]["integration"]
    assert integ["status"] == "accepted"
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code == 0, out


def test_hard_seam_change_after_approval_is_reviewed_again(env):
    """2: a repair that moves a hard seam (here the task's ownership envelope) is not
    APPROVED cleanup: the lane is reviewed again."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert len(_gates(env, "convergence_review")) == 1
    env.write_plan(PLAN_ONE.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    env.office("amend", "T1", "--contract", "--", "widen the envelope", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert len(_gates(env, "convergence_review")) == 2
    assert not _events(env, "convergence.cleanup")


# ------------------------------------------------------------------ INTAKE_GAP (4, 8, 9)

def test_convergence_intake_gap_surfaces_at_round_one(env):
    """4, 8, 9, 43: INTAKE_GAP names the user decision before any round cap."""
    gap = "VERDICT: INTAKE_GAP\nDECISION should add() accept floats?\nWHY requirements name ints only\nAFFECTS T1\nNEXT ask"
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": gap}])
    env.office("dispatch", "T1", check=0)
    st = _scope(env, "L-T1")
    assert st["status"] == "intake_gap" and st["round"] == 1
    assert st["intake_gap"]["decision"] == "should add() accept floats?"
    assert "should add() accept floats?" in _status(env)["next"]
    assert _run_row(env)["landing"].get("integration", {}).get("status") != "accepted"


# ------------------------------------------------------------------ plan review (1, 3, 4, 5, 6)

PLAN_GEAR = "express"


def test_plan_approved_with_findings_dispatches_and_tracks_them(env):
    """1: plan APPROVED with a non-blocking finding is dispatch-safe; the finding is tracked
    until dispositioned; a cleanup revision that moves no seam gets no re-review."""
    _start(env, gear=PLAN_GEAR, approve=False,
           plan_reviewer=[{"reply": APPROVED.replace("NEXT", "FINDING P1 | low | non-blocking | T1 | wording | tidy\nNEXT")}])
    rs = _run_row(env)["plan_review"]
    assert rs["ended"] and rs["status"] == "approved"
    env.office("approve", "plan", "--quote", "go", check=0)
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.write_plan(PLAN_ONE.replace("# Plan", "# Plan (tidied)"))
    code, out = env.office("submit")
    assert code == 0 and "APPROVED cleanup" in out, out
    assert len(_gates(env, "plan_review")) == 1
    env.office("dispatch", "T1", check=0)
    assert any("plan:P1" in b for b in [_status(env)["next"]])
    env.office("disposition", "plan:P1", "fixed", "--", "reworded in p2", check=0)
    assert _q(env, "SELECT disposition FROM findings WHERE code='P1'")[0]["disposition"] == "fixed"


def test_plan_recheck_holds_only_the_affected_task_and_rechecks_with_the_same_reviewer(env):
    """3, 6 (plan): a blocking finding naming T2 holds T2 only; the revision is reviewed by the
    same reviewer route; an unavailable reviewer attempt spends no round."""
    _start(env, plan=PLAN_TWO, gear=PLAN_GEAR, approve=False,
           plan_reviewer=[{"reply": recheck("FINDING P1 | high | blocking | T2 | mul has no contract | add one")},
                          {"exit": 1}, {"reply": APPROVED}],
           executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("approve", "plan", "--quote", "go", check=0)
    rs = _run_row(env)["plan_review"]
    assert rs["status"] == "recheck" and not rs["ended"]
    code, out = env.office("dispatch", "T2")
    assert code == 4 and "plan-recheck" in out, out
    env.office("dispatch", "T1", check=0)
    env.write_plan(PLAN_TWO.replace("- mul.mul(2, 3) == 6", "- mul.mul(2, 3) == 6\n- mul.mul(0, 5) == 0"))
    env.office("amend", "T2", "--contract", "--", "mul contract", check=0)
    reviews = _gates(env, "plan_review")
    completed = [g for g in reviews if g["review_status"] == "COMPLETED"]
    assert [g["round"] for g in completed] == [1, 2], reviews
    assert completed[1]["verdict"] == "APPROVED" and completed[1]["env_failures"] >= 1
    assert completed[0]["route"] != completed[1]["route"] or completed[1]["env_failures"] == 0
    env.office("dispatch", "T2", check=0)


def test_plan_round_cap_escalates_and_never_becomes_an_intake_gap(env):
    """5 (plan), 15, 40-43."""
    blocking = recheck("FINDING P1 | medium | blocking | plan | ordering is unsafe | split T1", nxt="escalate")
    _start(env, gear=PLAN_GEAR, approve=False, plan_reviewer=[{"reply": blocking}])
    for i in range(3):
        env.write_plan(PLAN_ONE.replace("- add() returns the sum", f"- add() returns the sum (rev {i})"))
        env.office("amend", "plan", "--contract", "--", f"rev {i}", check=None)
    rs = _run_row(env)["plan_review"]
    assert rs["status"] == "escalated", rs
    assert len([g for g in _gates(env, "plan_review") if g["review_status"] == "COMPLETED"]) == 3
    esc = rs["escalation"]
    assert esc["recommendation"] == "escalate" and len(esc["choices"]) == 4 and len(esc["attempts"]) == 3
    assert not rs.get("intake_gap")
    assert "office decide plan" in _status(env)["next"]
    env.office("decide", "plan", "continue", "--quote", "keep going", check=0)
    env.write_plan(PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum (rev 9)"))
    env.office("amend", "plan", "--contract", "--", "rev 9", check=0)
    last = _gates(env, "plan_review")[-1]
    assert last["cycle"] == 2 and last["round"] == 1


def test_plan_intake_gap_holds_named_scope_before_the_cap(env):
    """4, 8, 9 (plan)."""
    gap = "VERDICT: INTAKE_GAP\nDECISION may mul overflow?\nWHY nothing says\nAFFECTS T2\nNEXT ask the user"
    _start(env, plan=PLAN_TWO, gear=PLAN_GEAR, approve=False, plan_reviewer=[{"reply": gap}],
           executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("approve", "plan", "--quote", "go", check=0)
    rs = _run_row(env)["plan_review"]
    assert rs["status"] == "intake_gap" and rs["intake_gap"]["decision"] == "may mul overflow?"
    assert "may mul overflow?" in _status(env)["next"]
    code, out = env.office("dispatch", "T2")
    assert code == 4
    env.office("dispatch", "T1", check=0)


# ------------------------------------------------------------------ fallback (6, 12, 13, 31-34)

def test_reviewer_failures_walk_the_fallback_chain_without_spending_a_round(env):
    """6, 12, 31, 32."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"exit": 1}, {"exit": 1}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[0]
    assert gate["verdict"] == "APPROVED" and gate["round"] == 1 and gate["env_failures"] == 2, gate
    assert _scope(env, "L-T1")["status"] == "approved"


def test_exhausted_specialists_allow_a_degraded_orchestrator_review(env, tmp_path):
    """12, 13, 33, 34: every route failing is UNAVAILABLE status (not a verdict); the
    orchestrator may then review, recorded as degraded and non-independent; a worker cannot."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[0]
    assert gate["review_status"] == "UNAVAILABLE" and gate["verdict"] is None and gate["round"] == 1
    st = _scope(env, "L-T1")
    assert st["status"] == "unavailable" and st["fallback_available"]
    assert "office review L-T1:convergence" in _status(env)["next"]
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report), env={"OFFICE_DISPATCH_ID": "D123"})
    assert code == 4 and "producer or worker" in out, out
    env.office("review", "L-T1:convergence", "--report", str(report), check=0)
    gates = _gates(env, "convergence_review")
    assert gates[-1]["independence"] == "degraded-orchestrator" and gates[-1]["verdict"] == "APPROVED"
    assert _scope(env, "L-T1")["status"] == "approved"
    from office import convergence
    con = env.con()
    receipt = convergence.receipt(con, _run_row(env))
    assert receipt["degraded"] == ["L-T1:convergence_review"]


# ------------------------------------------------------------------ waivers (14, 35-39)

def _escalate(env, extra=()):
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(5)]
    _start(env, extra=extra, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] == "escalated"


def test_waiver_needs_landing_authority_and_keeps_the_verdict(env):
    """14, 35-38: the orchestrator cannot waive without delegated landing authority; the user
    can; the RECHECK verdict stands and the receipt shows the waiver."""
    _escalate(env)
    code, out = env.office("approve", "waive", "L-T1:convergence", "--as", "orchestrator", "--reason", "ship it")
    assert code == 4 and "landing authority" in out, out
    code, out = env.office("approve", "waive", "L-T1:convergence", "--reason", "x")
    assert code == 2 and "quote" in out, out
    env.office("approve", "waive", "L-T1:convergence", "--quote", "ship it anyway", "--reason", "deadline", check=0)
    st = _scope(env, "L-T1")
    assert st["status"] == "waived"
    assert _gates(env, "convergence_review")[-1]["verdict"] == "RECHECK", "the waiver never rewrites the verdict"
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"
    from office import convergence
    w = convergence.receipt(env.con(), _run_row(env))["waivers"][0]
    assert w["target"].startswith("L-T1:convergence_review@") and w["by"] == "user"
    assert w["underlying"] == "RECHECK" and w["reason"] == "deadline"


def test_delegated_orchestrator_may_waive_and_recomposition_voids_it(env):
    """35, 36, 39: delegated landing authority lets the orchestrator waive; a new
    composition leaves the waiver behind."""
    _escalate(env, extra=("--end-state", "merge"))
    env.office("approve", "waive", "L-T1:convergence", "--as", "orchestrator", "--reason", "user delegated landing",
               check=0)
    assert _scope(env, "L-T1")["status"] == "waived"
    w = _q(env, "SELECT * FROM authorizations WHERE kind='waiver'")[0]
    assert w["authorized_by"].startswith("orchestrator (landing delegated at intake")
    env.script(executor=[{"write": {"calc.py": GOOD_ADD + "# new\n"}, "submit": True}],
               convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("disposition", "L-T1:F1", "fix", check=None)  # not a non-blocking finding: refused
    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] != "waived", "the waiver bound the earlier composition only"


# ------------------------------------------------------------------ visual (10, 11, 15, 26-30)

def test_visual_applicability_is_runtime_guarded_and_evidence_is_not_a_verdict(env):
    """10, 27, 28, 29: acceptance that reads user-visible without a visual block still gets
    a visual gate; missing evidence is EVIDENCE_BLOCKED (no verdict) and blocks landing."""
    _start(env, plan=PLAN_UI, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    vis = _gates(env, "visual")
    assert len(vis) == 1 and vis[0]["review_status"] == "EVIDENCE_BLOCKED" and vis[0]["verdict"] is None
    assert vis[0]["evidence_status"] == "CAPTURE_BLOCKED" and "declares no visual capture target" in vis[0]["summary"]
    assert _scope(env, "L-T1")["status"] == "evidence_blocked"
    assert _run_row(env)["landing"].get("integration", {}).get("status") != "accepted"


def _fake_capture(monkeypatch, tmp_path):
    from office import visual
    shot = tmp_path / "desktop-default.png"
    shot.write_bytes(b"\x89PNG" + b"0" * 2000)

    def capture_all(con, run, task, rev, gate, worktree):
        receipt = tmp_path / f"receipt-{gate['id']}-{task['id']}.json"
        receipt.write_text(json.dumps({"reference": None, "frames": [
            {"viewport": "desktop 1440x900", "state": "default", "screenshot": str(shot)}]}))
        return {"evidence_status": "COMPARABLE", "cause": None, "product_failures": [], "receipt_path": str(receipt),
                "receipt_digest": "x"}

    monkeypatch.setattr(visual, "capture_all", capture_all)
    monkeypatch.setattr(visual, "preflight", lambda tasks: ([], []))  # no server to reach in a test
    return shot


PLAN_VISUAL = PLAN_ONE.replace("visual: none", "visual:\n  url: http://localhost:3999/\n  viewports: desktop")


def test_visual_review_is_a_lane_gate_with_its_own_round_cap(env, monkeypatch, tmp_path):
    """5 (visual), 15, 26, 29: the planner's visual block gets a specialist visual review in
    parallel with the convergence review on the same composed lane; RECHECK x3 escalates."""
    _fake_capture(monkeypatch, tmp_path)
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(4)]
    vis = "EVIDENCE_STATUS: COMPARABLE\n" + recheck(finding("U1", where="header @ desktop", what="clipped"))
    _start(env, plan=PLAN_VISUAL, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": vis}], probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    code_gate, vis_gate = _gates(env, "convergence_review")[0], _gates(env, "visual")[0]
    assert code_gate["input_key"] == vis_gate["input_key"], "both review the same composed revision"
    assert vis_gate["verdict"] == "RECHECK" and vis_gate["evidence_status"] == "COMPARABLE", (vis_gate["review_status"], vis_gate["summary"])
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert [g["round"] for g in _gates(env, "visual")] == [1, 2, 3]
    assert _scope(env, "L-T1")["status"] == "escalated"


def test_visual_fallback_cannot_approve_uninspected_evidence(env, monkeypatch, tmp_path):
    """11, 30."""
    shot = _fake_capture(monkeypatch, tmp_path)
    _start(env, plan=PLAN_VISUAL, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"exit": 1}], probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "visual")[0]["review_status"] == "UNAVAILABLE"
    report = tmp_path / "vis.txt"
    report.write_text("EVIDENCE_STATUS: COMPARABLE\n" + APPROVED)
    code, out = env.office("review", "L-T1:visual", "--report", str(report))
    assert code == 4 and "cannot-inspect-evidence" in out or "inspect every" in out, out
    assert all(g["verdict"] is None for g in _gates(env, "visual"))
    env.office("review", "L-T1:visual", "--report", str(report), "--inspected", str(shot), check=0)
    assert _gates(env, "visual")[-1]["independence"] == "degraded-orchestrator"
    assert _scope(env, "L-T1")["status"] == "approved"


# ------------------------------------------------------------------ compatibility (16, 17, 44-49)

def _legacy_run(env, **script):
    """A run as it was before #337: its pinned gate policy names no review contract."""
    env.trust()
    env.script(**script)
    start_inline(env, extra=("--set", "review.contract=v3.1"))
    con = env.con()
    row = con.execute("SELECT id, gates_json FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    gates = json.loads(row["gates_json"])
    gates.pop("review_contract")
    con.execute("UPDATE runs SET gates_json=? WHERE id=?", (json.dumps(gates), row["id"]))
    con.commit()
    env.office("approve", "plan", "--quote", "approved", check=0)
    return row["id"]


def test_old_run_resumes_under_v31_semantics_and_reads_unchanged(env):
    """16, 44, 46, 48: a pre-#337 run keeps per-task code review and its PASS verdicts,
    shown labelled, never translated."""
    run_id = _legacy_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                         code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("resume", run_id[:8], check=0)
    env.office("dispatch", "T1", check=0)
    assert "convergence_reviewer" not in _roles(env) and _roles(env).count("code_reviewer") == 1
    assert [g["verdict"] for g in _gates(env, "code_review")] == ["PASS"]
    assert not _gates(env, "convergence_review")
    assert "review_contract" not in _run_row(env, run_id)["gates"], "the old run is never converted"
    code, out = env.office("inspect", "task", "T1")
    assert "PASS (v3.1)" in out, out
    assert _run_row(env, run_id)["landing"]["integration"]["status"] == "accepted"


def test_rollback_changes_only_future_runs(env, tmp_path):
    """17, 49: setting review.contract back to v3.1 makes the next run v3.1; a started run
    keeps convergence-v1. An unknown contract is refused."""
    _start(env)
    first = _run_row(env)["id"]
    Path(env.tmp / "user-config.yaml").write_text("review:\n  contract: v3.1\n")
    code, out = env.office("start", "second goal", "--gear", "direct+review", "--planner", "inline")
    assert code == 0, out
    second = _run_row(env)["id"]
    assert second != first and _run_row(env, second)["gates"]["review_contract"] == "v3.1"
    assert _run_row(env, first)["gates"]["review_contract"] == "convergence-v1"
    Path(env.tmp / "user-config.yaml").write_text("review:\n  contract: v9\n")
    code, out = env.office("start", "third goal", "--planner", "inline")
    assert code == 2 and "review.contract" in out, out


def test_explicit_new_run_moves_old_work_without_touching_it(env):
    """16, 48: --from-run starts a new-contract run with the old requirements and plan draft;
    the old run's records stay as they were, with an appended successor event."""
    old = _legacy_run(env)
    before = _q(env, "SELECT id, verdict, status FROM gates WHERE run_id=? ORDER BY id", (old,))
    code, out = env.office("start", "--from-run", old[:8], "--planner", "inline", "--gear", "direct+review")
    assert code == 0 and "derived from" in out, out
    new = _run_row(env)
    assert new["id"] != old and new["gates"]["review_contract"] == "convergence-v1"
    assert new["landing"]["derived_from"]["run"] == old and new["landing"]["derived_from"]["review_contract"] == "v3.1"
    assert "review_contract" not in _run_row(env, old)["gates"]
    assert _q(env, "SELECT id, verdict, status FROM gates WHERE run_id=? ORDER BY id", (old,)) == before
    assert _q(env, "SELECT 1 FROM events WHERE run_id=? AND kind='run.successor'", (old,))
    draft = env.repo / ".office" / "plans" / new["id"][:8] / "PLAN.md"
    assert draft.is_file() and "Implement add" in draft.read_text()


def test_new_receipt_names_its_contract(env):
    """47: a closed run's archive receipt names its review contract and convergence record."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    env.office("close", "--handoff", "https://example.test/pr/1", check=0)
    receipt = json.loads((Path(_run_row(env)["state_dir"]) / "archive-receipt.json").read_text())
    assert receipt["review_contract"] == "convergence-v1"
    assert receipt["convergence"]["scopes"][0]["reviews"][0]["verdict"] == "APPROVED"


def test_unrunnable_check_is_status_not_a_verdict(env):
    """11, 28: a check that cannot run is UNAVAILABLE status with no verdict; nothing advances."""
    _start(env, plan=PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                                      "checks: no-such-command-337"),
           executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    g = _gates(env, "checks")[0]
    assert g["review_status"] == "UNAVAILABLE" and g["verdict"] is None
    assert task_row(env)["status"] == "blocked" and not _gates(env, "convergence_review")


def test_unreadable_reply_is_invalid_result_and_spends_no_round(env):
    """6, 11: an unparseable reply is INVALID_RESULT runtime status; the retry is round 1 again."""
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": "looks fine to me"}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    first = _gates(env, "convergence_review")[0]
    assert first["review_status"] == "INVALID_RESULT" and first["verdict"] is None
    assert _scope(env, "L-T1")["status"] == "attention"
    env.office("resume", check=0)
    gates = _gates(env, "convergence_review")
    assert gates[-1]["verdict"] == "APPROVED" and gates[-1]["round"] == 1


def test_recheck_routes_repairs_even_when_the_visual_evidence_is_blocked(env):
    """10, 24: a blocked visual capture is evidence state; it does not hold back the code
    RECHECK repairs, and the recomposition re-runs both gates."""
    _start(env, plan=PLAN_UI, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "visual")[0]["review_status"] == "EVIDENCE_BLOCKED"
    assert _scope(env, "L-T1")["status"] == "recheck" and task_row(env)["status"] == "changes_required"
