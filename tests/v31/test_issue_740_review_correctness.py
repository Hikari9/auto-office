"""Current review authority and ownership regressions for #687, #715, #716 and #720."""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, task_row
from test_convergence_contract import APPROVED, PLAN_CHAIN, PLAN_TWO, _gates, _q, _run_row, _scope, _start, finding, recheck


def _brief(env, gate):
    from office import paths
    return (paths.run_dir(_run_row(env)["id"]) / "dispatches" / gate["reviewer_dispatch_id"] / "brief.md").read_text()


@pytest.mark.parametrize("contract, verdict", [("v3.1", "PASS"), ("convergence-v1", "APPROVED")])
@pytest.mark.parametrize("bad_status", ["UNKNOWN", "123", "COMPARABLE123", ""])
def test_invalid_visual_evidence_status_names_every_allowed_value(contract, verdict, bad_status):
    from office import review_parse
    parsed = review_parse.parse(f"EVIDENCE_STATUS: {bad_status}\nVERDICT: {verdict}\nNEXT proceed", visual=True, contract=contract)
    assert not parsed.valid
    error = " ".join(parsed.errors)
    assert "EVIDENCE_STATUS" in error
    for value in review_parse.EVIDENCE_STATUSES:
        assert value in error


def test_visual_report_invalid_cli_is_actionable(env):
    _start(env)
    report = env.repo / "invalid-review.txt"
    report.write_text("EVIDENCE_STATUS: UNKNOWN\n" + APPROVED)
    code, output = env.office("review", "L-T1:visual", "--report", str(report))
    assert code == 4 and "report-invalid" in output
    for value in ("COMPARABLE", "INVALID_COMPARISON", "NOT_APPLICABLE", "CAPTURE_BLOCKED"):
        assert value in output


def test_recomposed_lane_brief_has_current_requirements_plan_tasks_and_dismissal(env):
    note = finding("F1", severity="low", blocking=False, what="keep multiplication excluded")
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"mul.py": GOOD_MUL}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED.replace("NEXT", note + "\nNEXT")}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    first = _gates(env, "convergence_review")[-1]
    first_brief = _brief(env, first)
    env.office("disposition", "L-T1:F1", "dismissed", "--quote", "Multiplication is now included", check=0)
    env.office("amend", "requirements", "--quote", "Add multiplication and allow a CLI", "--drop-non-goal", "no CLI",
               "--add-criterion", "mul() returns the product", "--", "Multiplication is included; allow a CLI", check=0)
    env.office("approve", "plan", "--quote", "approved current requirements", check=0)
    plan = PLAN_CHAIN.replace("- add and mul exist", "- add() returns the sum\n- mul() returns the product")
    env.write_plan(plan)
    env.office("amend", "plan", "--", "include dependent multiplication task", check=0)
    env.office("dispatch", "T2", check=0)
    latest = _gates(env, "convergence_review")[-1]
    brief = _brief(env, latest)
    run = _run_row(env)
    assert latest["input_key"] != first["input_key"]
    assert "T1, T2" in brief and "TASK T2 Implement mul" in brief
    assert _scope(env, "L-T1")["commit"][:12] in brief
    assert f"REQUIREMENTS r{run['requirements_version']}" in brief
    assert "Multiplication is included; allow a CLI" in brief
    assert "no CLI" not in brief
    assert f"CURRENT PLAN p{run['plan_version']}" in brief and plan in brief
    assert "DISMISSED FINDINGS" in brief and "F1" in brief and "Multiplication is now included" in brief
    assert task_row(env, "T2")["accepted_revision_id"] in brief
    assert "TASK T2" not in first_brief and "no CLI" in first_brief
    assert _scope(env, "L-T1")["status"] == "approved"


def test_an_added_task_requires_a_fresh_review_even_when_the_composed_tree_is_unchanged(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}] * 2,
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    before = _scope(env, "L-T1")
    env.write_plan(PLAN_ONE + """
### T2: Verify addition edge cases
scope: calc.py
depends: T1
checks: python3 -c "import calc; assert calc.add(0, 0) == 0"
accept:
- calc.add(0, 0) == 0
visual: none
""")
    env.office("amend", "plan", "--", "add dependent edge case verification", check=0)
    env.office("dispatch", "T2", check=0)
    after = _scope(env, "L-T1")
    assert after["tree"] == before["tree"]
    reviews = _gates(env, "convergence_review")
    assert len(reviews) == 2 and json.loads(reviews[-1]["members_json"]) == ["T1", "T2"]
    assert "TASK T2 Verify addition edge cases" in _brief(env, reviews[-1])
    assert after["status"] == "approved"


@pytest.mark.parametrize("quote, missing", [(None, "user-quote-required"), ("allow CLI", "unknown-non-goal")])
def test_non_goal_edits_require_authority_and_exact_target(env, quote, missing):
    _start(env)
    before = _q(env, "SELECT * FROM requirements")
    args = ["--quote", quote] if quote else []
    code, output = env.office("amend", "requirements", *args, "--drop-non-goal", "absent", "--", "allow CLI")
    assert code == 4 and missing in output
    assert _q(env, "SELECT * FROM requirements") == before


def test_non_goal_contract_edits_are_transactional_and_not_ordinary_amendments(env):
    _start(env)
    before = _q(env, "SELECT * FROM requirements")
    code, output = env.office("amend", "T1", "--drop-non-goal", "no CLI", "--", "allow CLI")
    assert code == 2 and "non-goals-are-requirements" in output
    assert _q(env, "SELECT * FROM requirements") == before
    env.office("amend", "requirements", "--quote", "Exclude network too", "--add-non-goal", "no network",
               "--", "exclude network", check=0)
    before = _q(env, "SELECT * FROM requirements")
    code, output = env.office("amend", "requirements", "--quote", "remove one exclusion", "--drop-non-goal", "no",
                             "--add-criterion", "new criterion", "--", "remove one exclusion")
    assert code == 4 and "ambiguous-non-goal" in output
    assert _q(env, "SELECT * FROM requirements") == before
    assert json.loads(before[-1]["frozen_json"])["non_goals"] == ["no CLI", "no network"]


def test_recheck_brief_preserves_blocking_dismissal_quote_and_distinct_note(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD + "# repaired\n"}, "submit": True}],
           convergence_reviewer=[{"reply": recheck(finding("F1", what="disputed requirement"),
                                                    finding("F2", what="missing edge case"))},
                                 {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    env.office("disposition", "L-T1:F1", "dismissed", "--quote", "Original precedence is intentional",
               "--", "operator recorded the precedence decision", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    brief = _brief(env, _gates(env, "convergence_review")[-1])
    assert "Original precedence is intentional" in brief
    assert "operator recorded the precedence decision" in brief
    assert "OPEN FINDINGS" in brief and "missing edge case" in brief
    assert _scope(env, "L-T1")["status"] == "approved"


@pytest.mark.parametrize("location", ["Dockerfile", "Dockerfile:1", "`./sibling/file.py`:20", "sibling/file.py"])
def test_extensionless_and_quoted_foreign_paths_cannot_assign_lane_producers(location, monkeypatch):
    from office import convergence, state
    tasks = [{"id": "T1", "scope": ["Dockerfile", "sibling/"], "status": "accepted"},
             {"id": "T2", "scope": ["local/"], "status": "accepted"}]
    monkeypatch.setattr(state, "tasks", lambda *_: tasks)
    monkeypatch.setattr(state, "get_task", lambda _con, _run, tid: next(t for t in tasks if t["id"] == tid))
    assert convergence._owners(None, {"id": "run"}, {"tasks": ["T2"]}, {"location": location, "owners": ["T2"]}) == []


@pytest.mark.parametrize("owner", ["T2", "T1", ""])
def test_finding_owned_by_another_lane_is_retained_without_nonowner_repair(env, owner):
    _start(env, plan=PLAN_TWO,
           executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                     {"write": {"mul.py": GOOD_MUL}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED},
                                 {"reply": recheck(finding("F1", owner, where="calc.py:1", what="sibling defect"))}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert task_row(env, "T1")["status"] == "accepted"
    assert task_row(env, "T2")["status"] == "accepted"
    rows = _q(env, "SELECT * FROM findings WHERE scope='L-T2'")
    assert len(rows) == 1 and rows[0]["task_id"] is None and rows[0]["state"] == "open"
    assert json.loads(rows[0]["owners"]) == []
    assert _scope(env, "L-T2")["status"] == "attention"
    _, data = env.ojson("status")
    assert "scope gap" in data["next"] and "T1" in data["next"]
    before = len(_gates(env, "convergence_review"))
    env.office("resume", check=0)
    assert len(_gates(env, "convergence_review")) == before
    assert _scope(env, "L-T2")["status"] == "attention"


def test_retrying_an_unavailable_sibling_review_preserves_the_scope_gap(env):
    from office import convergence, db, state
    _start(env, plan=PLAN_TWO,
           executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                     {"write": {"mul.py": GOOD_MUL}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED},
                                 {"reply": recheck(finding("F1", "T2", where="calc.py:1"))}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert _scope(env, "L-T2")["status"] == "attention"
    con = env.con()
    run = state.get_run(con, _run_row(env)["id"])
    with db.transaction(con):
        convergence._set_scope(con, run, "L-T1", status="unavailable")
        con.execute("UPDATE gates SET review_status='UNAVAILABLE', verdict=NULL WHERE scope='L-T1'")
    con.close()
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("resume", check=0)
    assert _scope(env, "L-T1")["status"] == "approved"
    assert _scope(env, "L-T2")["status"] == "attention"
    assert len([g for g in _gates(env, "convergence_review") if g["scope"] == "L-T2"]) == 1
