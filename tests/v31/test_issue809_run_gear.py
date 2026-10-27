"""#809 XS-XL run classification, per-task routing, and genuine XL audit fanout."""
import json

import pytest

from conftest import PLAN_ONE, start_inline
from office import briefs, config, contract, review_parse, risk, state, task_sizing, xl_review


@pytest.mark.parametrize("gear,plan_review,code_review,planner", [
    ("XS", False, False, False),
    ("S", False, True, False),
    ("M", False, True, False),
    ("L", True, True, True),
    ("XL", True, True, True),
])
def test_new_gear_funding_is_independent_of_low_safety_risk(gear, plan_review, code_review, planner):
    cfg = config.load_yaml(config.default_config_path())
    low = config.resolve_risk(cfg, "repo", None, False)
    low["size_class"] = gear
    low["complexity_only"] = True
    gates = config.resolve_gates(gear, low, cfg)
    assert (gates["plan_review"], gates["code_review"], gates["dedicated_planner"]) == (
        plan_review, code_review, planner)
    assert gates["review_contract"] == contract.CONVERGENCE
    assert gates["gear_model_version"] == "run-gear-1"


def test_risk_cannot_erase_review_floor_for_xs_or_raise_complexity_as_safety():
    cfg = config.load_yaml(config.default_config_path())
    for size in task_sizing.SIZES:
        low = config.resolve_risk(cfg, "repo", None, False)
        low.update(size_class=size, complexity_only=True)
        assert risk.classification(low) == "low"
        elevated = config.resolve_risk(cfg, "production", None, False)
        elevated.update(size_class=size, complexity_only=True)
        gates = config.resolve_gates(size, elevated, cfg)
        assert gates["code_review"] and gates["risk_classification"] == "elevated"
        assert gates["run_gear"] == size


def test_missing_task_sizing_is_conservative_and_never_inherits_run_size():
    unknown = {"id": "T1", "scope": [], "depends": [], "interfaces": [], "descriptor": {}}
    assert {task_sizing.classify(unknown, run_gear=g)["task_size"] for g in task_sizing.SIZES} == {"M"}
    mechanical = {**unknown, "scope": ["README.md"], "descriptor": {"work": "mechanical", "bounds": "bounded"}}
    assert {task_sizing.classify(mechanical, run_gear=g)["task_size"] for g in task_sizing.SIZES} == {"XS"}
    architecture = {**unknown, "scope": [f"svc/{i}" for i in range(8)],
                    "depends": ["T1", "T2", "T3"], "interfaces": ["a", "b", "c"],
                    "descriptor": {"work": "architecture", "difficulty_estimate": "very-high"}}
    sized = task_sizing.classify(architecture, run_gear="XL")
    assert sized["task_size"] == "XL" and sized["decomposition_recommended"]
    assert sized["profile"] == "discovery"  # architecture + very high uncertainty funds discovery


def test_explicit_sizes_profiles_and_inline_exceptions_need_evidence():
    task = {"id": "T4", "scope": ["docs/a.md"], "depends": [], "interfaces": [],
            "descriptor": {"task_size": "XS", "work": "mechanical",
                           "task_size_rationale": "one bounded reversible file"}}
    d = task_sizing.classify(task, run_gear="XL")
    assert d["task_size"] == "XS" and d["profile"] == "lean"
    assert task_sizing.execution_mode("XL", d, {"classification": "low"})["mode"] == "delegated"
    assert task_sizing.execution_mode("XS", d, {"classification": "low"})["mode"] == "inline"
    assert task_sizing.execution_mode("S", d, {"classification": "low"})["mode"] == "delegated"
    assert task_sizing.execution_mode("M", {**d, "inline_advantage": "retained context avoids three retries"},
                                      {"classification": "low"})["mode"] == "inline"


def test_cli_new_run_stores_independent_size_profile_and_gear_funding(env):
    env.trust()
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: XS\n"
                            "task_size_rationale: one bounded reversible file with no interfaces\n", 1)
    out = start_inline(env, plan=body, gear="XS")
    assert "RUN DISPATCH gear XS" in out and "profile: lean" in out, out
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    run = state.get_run(con, rid)
    desc = state.get_task(con, rid, "T1")["descriptor"]
    assert run["gear"] == "XS" and run["gates"]["gear_model_version"] == "run-gear-1"
    assert desc["task_size"] == "XS" and desc["profile"] == "lean"
    assert not run["gates"]["code_review"] and run["gates"]["review_contract"] == contract.CONVERGENCE
    diagram = con.execute("SELECT preview_json FROM plans WHERE run_id=?", (rid,)).fetchone()[0]
    assert json.loads(diagram)["tasks"]["T1"]["task_size"] == "XS"


def _parsed(verdict="APPROVED"):
    return review_parse.Parsed(verdict=verdict, contract=contract.CONVERGENCE)


def _xl_fixture(env):
    env.trust()
    start_inline(env, gear="XL")
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    run = state.get_run(con, rid)
    gate = dict(con.execute("SELECT * FROM gates WHERE run_id=? AND kind='plan_review' ORDER BY created_at DESC LIMIT 1",
                            (rid,)).fetchone())
    # The fake job worker may already have attempted the review. Isolate this
    # suite's injected reviewer dispatches from those earlier runtime attempts.
    con.execute("DELETE FROM gates WHERE run_id=? AND kind='goldfish_audit'", (rid,))
    con.execute("UPDATE gates SET status='queued', review_status=NULL, verdict=NULL WHERE id=?", (gate["id"],))
    con.commit()
    gate["status"] = "queued"
    return con, run, gate, state.current_plan(con, rid)


def test_xl_goldfish_launches_three_real_distinct_review_dispatch_calls_plus_lead(env):
    con, run, gate, plan = _xl_fixture(env)
    calls = []
    def reviewer(con, run, subgate, role, brief, **kw):
        calls.append((subgate["id"], brief, role))
        return {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(),
                "route": "agy/gemini-3.8-flash@medium", "dispatch_id": "D" + str(len(calls))}
    out = xl_review.audit_plan(con, run, gate, plan, "FROZEN REQUIREMENTS AND PLAN", env.repo, reviewer=reviewer)
    assert out["verdict"] == "APPROVED" and out["goldfish"]["mandatory"] == 3
    assert len(calls) == 4 and len({c[0] for c in calls}) == 4
    assert all("INDEPENDENT XL GOLDFISH" in c[1] for c in calls[:3])
    assert "XL LEAD PLAN REVIEW" in calls[-1][1]
    rows = con.execute("SELECT review_status, kind FROM gates WHERE run_id=? AND kind='goldfish_audit'",
                       (run["id"],)).fetchall()
    assert len(rows) == 3 and all(row["review_status"] == contract.COMPLETED for row in rows)
    assert len(con.execute("SELECT * FROM gates WHERE run_id=? AND kind='plan_review'", (run["id"],)).fetchall()) == 1


def test_xl_failed_goldfish_blocks_without_launching_lead(env):
    con, run, gate, plan = _xl_fixture(env)
    calls = []
    def reviewer(con, run, subgate, role, brief, **kw):
        calls.append(subgate["id"])
        return ({"status": contract.UNAVAILABLE, "verdict": None, "parsed": None} if len(calls) == 2 else
                {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(),
                 "dispatch_id": "D" + str(len(calls))})
    result = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert result["status"] == contract.EVIDENCE_BLOCKED and result["verdict"] is None
    assert len(calls) == 2
    assert con.execute("SELECT COUNT(*) FROM gates WHERE run_id=? AND kind='plan_review'",
                       (run["id"],)).fetchone()[0] == 1


def test_xl_hazard_reviewers_are_bounded_and_l_never_swarm():
    run = {"gear": "XL", "gates": {"gear_model_version": "run-gear-1"},
           "risk": {"blast_radius": "production-data", "irreversible": True}}
    plan = {"tasks": [{"scope": ["src/security/auth.py"], "visual": {"url": "http://localhost:1234"}}]}
    specs = xl_review.hazard_audits(run, plan)
    assert [s[0] for s in specs] == ["safety", "interaction"] and all(s[2] for s in specs)
    assert xl_review.eligible(run)
    assert not xl_review.eligible({**run, "gear": "L"})
    assert not xl_review.eligible({**run, "gear": "L", "task_size": "XL"})


def test_xs_runtime_dispatch_allocates_fenced_orchestrator_inline_worker(env):
    env.trust()
    body = PLAN_ONE.replace("visual: none", "visual: none\n"
                            "task_size: XS\n"
                            "task_size_rationale: bounded local change with no external interfaces\n", 1)
    start_inline(env, plan=body, gear="XS")
    code, out = env.office("approve", "plan", "--quote", "approved")
    assert code == 0, out
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    assert "orchestrator-inline lease/worktree prepared" in out and "no model launched" in out, out
    con = env.con()
    row = con.execute("SELECT * FROM dispatches WHERE role='executor' ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row["route_json"])["execution_mode"]["mode"] == "inline"
    lease = con.execute("SELECT * FROM leases WHERE dispatch_id=?", (row["id"],)).fetchone()
    assert lease is not None and lease["released_at"] is None


def test_single_task_s_runtime_is_still_delegated_by_default(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE, gear="S")
    code, out = env.office("approve", "plan", "--quote", "approved")
    assert code == 0, out
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    assert "orchestrator-inline" not in out, out
    con = env.con()
    row = con.execute("SELECT * FROM dispatches WHERE role='executor' ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row["route_json"]).get("execution_mode", {}).get("mode") == "delegated", json.loads(row["route_json"])
