"""#809 XS-XL run classification, per-task routing, and genuine XL audit fanout."""
import json

import pytest

from conftest import PLAN_ONE, start_inline
from office import config, contract, executor_slates, planfile, risk, state, task_sizing


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


def test_explicit_sizes_and_profiles_need_evidence():
    task = {"id": "T4", "scope": ["docs/a.md"], "depends": [], "interfaces": [],
            "descriptor": {"task_size": "XS", "work": "mechanical",
                           "task_size_rationale": "one bounded reversible file"}}
    d = task_sizing.classify(task, run_gear="XL")
    assert d["task_size"] == "XS" and d["profile"] == "lean"


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


def _run(env):
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    return con, state.get_run(con, rid)


def _approved_dispatch(env, plan, gear):
    env.trust()
    start_inline(env, plan=plan, gear=gear)
    code, out = env.office("approve", "plan", "--quote", "approved")
    assert code == 0, out
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    return out


# ---------------------------------------------------------------- R1: gear names and legacy resume

def test_start_help_offers_run_gears_alongside_legacy_names(env):
    code, out = env.office("start", "--help")
    assert code == 0 and "XS" in out and "XL" in out, out


@pytest.mark.parametrize("gear", ["XS", "S", "M", "L", "XL"])
def test_new_run_pins_the_requested_run_gear_with_why_and_funded_gates(env, gear):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", gear, "--planner", "inline")
    assert code == 0 and f"gear {gear}" in out and "run gear why:" in out and "funded gates:" in out, out
    _, run = _run(env)
    assert run["gear"] == gear and run["gates"]["gear_model_version"] == "run-gear-1"
    assert run["gates"]["run_gear_rationale"]


def test_unknown_size_is_conservative_m_and_never_guessed_xs(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0 and "gear M" in out, out
    assert "conservative M" in _run(env)[1]["gates"]["run_gear_rationale"]


def test_legacy_name_starts_an_explicit_preset_and_names_its_closest_gear(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "full", "--planner", "inline")
    assert code == 0 and "closest to XL" in out, out
    assert set(config.LEGACY_EQUIVALENT) == set(config.LEGACY_GEARS)
    assert set(config.LEGACY_EQUIVALENT.values()) <= set(config.RUN_GEARS)


def test_legacy_pinned_run_resumes_with_its_original_gear_and_gates(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE, gear="express")
    env.office("approve", "plan", "--quote", "approved", check=0)
    con, before = _run(env)
    assert before["gear"] == "express" and before["gates"]["gear_model_version"] == "legacy-pinned"
    env.office("resume", check=0)
    _, after = _run(env)
    assert after["gear"] == "express"
    for key in ("gear_model_version", "run_gear", "plan_review", "code_review", "dedicated_planner",
                "review_floor", "review_contract", "run_gear_rationale"):
        assert after["gates"].get(key) == before["gates"].get(key), key
    assert "RUN DISPATCH gear express" in env.office("inspect", "plan")[1]


# ---------------------------------------------------------------- R2: planner run-gear recommendation

REC = "run_gear: {gear}\nrun_gear_why: three coupled packages with a shared interface\n"


def _plan_with(rec):
    return PLAN_ONE.replace("## Requirements\n", "## Requirements\n" + rec, 1)


def test_planfile_parses_the_recommendation_and_requires_reasons_and_a_known_gear():
    ok = planfile.parse(_plan_with(REC.format(gear="L")))
    assert not ok.errors and ok.requirements["run_gear"] == {
        "gear": "L", "why": "three coupled packages with a shared interface"}
    assert planfile.parse(PLAN_ONE).requirements["run_gear"] is None
    assert any("run_gear_why" in e for e in planfile.parse(_plan_with("run_gear: L\n")).errors)
    assert any("run_gear must be one of" in e for e in planfile.parse(_plan_with(REC.format(gear="huge"))).errors)


@pytest.mark.parametrize("intake,rec,refused", [
    ("L", "M", True), ("XL", "L", True), ("S", "XS", True), ("M", "M", False), ("S", "XL", False),
])
def test_recommendation_below_the_intake_gear_is_refused(env, intake, rec, refused):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", intake, "--planner", "inline",
                           "--blast-radius", "local")
    assert code == 0, out
    env.write_plan(_plan_with(REC.format(gear=rec)))
    code, out = env.office("submit")
    if refused:
        assert code != 0 and "run gear recommendation refused" in out and "below the intake gear" in out, out
        con, run = _run(env)
        assert not state.current_plan(con, run["id"]) and run["gear"] == intake
    else:
        assert code == 0 and f"planner recommends run gear {rec}" in out, out
        gates = _run(env)[1]["gates"]
        assert gates["run_gear_recommendation"]["gear"] == rec
        assert _run(env)[1]["gear"] == intake, "a recommendation is advisory and never repins the run"


def test_recommendation_below_the_risk_floor_is_refused_even_at_the_intake_gear(env):
    env.trust()
    env.office("start", "fixture goal", "--gear", "XS", "--planner", "inline", check=0)
    plan = _plan_with(REC.format(gear="XS")).replace("blast_radius: repo", "blast_radius: production")
    env.write_plan(plan)
    code, out = env.office("submit")
    assert code != 0 and "run gear recommendation refused" in out and "below the S floor" in out, out
    con, run = _run(env)
    assert not state.current_plan(con, run["id"])


def test_risk_module_run_gear_problem_is_order_and_risk_aware():
    low = {"classification": "low"}
    elevated = {"classification": "elevated"}
    assert risk.run_gear_problem("M", low, "L") is None
    assert risk.run_gear_problem("M", low, "S") and "intake gear M" in risk.run_gear_problem("M", low, "S")
    assert risk.run_gear_problem("XS", elevated, "XS") and "floor" in risk.run_gear_problem("XS", elevated, "XS")
    assert risk.run_gear_problem("XS", elevated, "S") is None
    assert risk.run_gear_problem("full", low, "XS") is None  # a legacy pin has no gear order to undercut
    assert risk.run_gear_problem("S", low, "XXL")


# ---------------------------------------------------------------- R4-R8: dispatch output and routing audit

def test_dispatch_prints_run_gear_funded_gates_size_profile_and_actual_route(env):
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S\n"
                            "task_size_rationale: one bounded file with no interfaces\n", 1)
    out = _approved_dispatch(env, body, "M")
    assert "RUN DISPATCH gear M" in out and "funded gates: plan review=no, convergence=yes" in out, out
    assert "T1 S | profile balanced | why: one bounded file with no interfaces" in out, out
    assert "ACTUAL DISPATCH T1:" in out, out


def test_routing_audit_records_every_809_field(env):
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S\n"
                            "task_size_rationale: one bounded file with no interfaces\n", 1)
    _approved_dispatch(env, body, "M")
    con, run = _run(env)
    row = con.execute("SELECT disclosure_json FROM route_audit WHERE run_id=? AND phase='dispatch' "
                      "ORDER BY created_at ASC LIMIT 1", (run["id"],)).fetchone()  # the first dispatch, before any fix cycle
    audit = json.loads(row[0])
    assert audit["run_gear"] == "M" and audit["run_gear_rationale"] == run["gates"]["run_gear_rationale"]
    assert audit["task_size"] == "S" and audit["task_size_rationale"] == "one bounded file with no interfaces"
    assert audit["profile"] == "balanced" and audit["profile_version"] == task_sizing.PROFILE_VERSION
    assert audit["preferred_route"] and audit["preferred_route_reason"] and audit["selection_reason"]
    assert isinstance(audit["fallback_routes"], list)
    assert audit["selected_route"] == audit["dispatch"]["dispatched"]
    assert audit["effective_gates"]["code_review"] is True and audit["effective_gates"]["plan_review"] is False
    assert audit["plan_version"] == run["plan_version"] == 1
    assert audit["wave"] == 1 and audit["dispatch_version"] == 1
    # The same facts ride on the task's stored route, so a relaunch reproduces them.
    stored = json.loads(con.execute("SELECT route_json FROM tasks WHERE run_id=? AND id='T1'", (run["id"],)).fetchone()[0])
    assert stored["selected_route"] == audit["selected_route"] and stored["size_decision"]["task_size"] == "S"


def test_receipt_lists_why_each_unselected_ideal_route_was_skipped():
    ideal = [{"ideal_rank": 1, "route": "claude/claude-haiku-5-5@medium", "status": "selected",
              "qualified_routes": [], "rejections": [], "reason": "x"},
             {"ideal_rank": 2, "route": "*/gemini-3.8-flash@low", "status": "unqualified", "qualified_routes": [],
              "rejections": [{"route": "agy/gemini-3.8-flash@low", "reason": "XS subtask requires score >= 31"}],
              "reason": "exact route is missing or rejected"},
             {"ideal_rank": 3, "route": "codex/gpt-6.1-terra@low", "status": "not-in-catalog",
              "qualified_routes": [], "rejections": [], "reason": "exact route is missing or rejected by catalog, trust, quota, or policy"}]
    lines = executor_slates.skip_lines(ideal)
    assert len(lines) == 2 and "selected" not in " ".join(lines)
    assert "#2 */gemini-3.8-flash@low (unqualified): agy/gemini-3.8-flash@low: XS subtask requires score >= 31" in lines[0]
    assert "#3 codex/gpt-6.1-terra@low (not-in-catalog)" in lines[1]
    assert executor_slates.skip_lines(None) == []


def test_plan_diagram_shows_run_gear_header_size_profile_and_every_fallback_rationale(env):
    env.trust()
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S\n"
                            "task_size_rationale: one bounded file with no interfaces\n", 1)
    out = start_inline(env, plan=body, gear="M")
    assert "RUN DISPATCH gear M" in out and "funded gates:" in out, out
    assert "size: S | one bounded file with no interfaces" in out and "profile: balanced" in out, out
    con, run = _run(env)
    slate = json.loads(con.execute("SELECT preview_json FROM plans WHERE run_id=?", (run["id"],)).fetchone()[0])["tasks"]["T1"]["slate"]
    assert len(slate) >= 2
    for entry in slate[1:]:
        assert entry["reason"] and entry["reason"] in out, entry


def test_replan_delta_reports_size_and_profile_changes():
    from office import plan_view
    prev = {"tasks": {"T1": {"title": "t", "task_size": "S", "profile": "balanced", "route": "a", "depends": [], "wave": 1,
                            "base": None, "needs": []}}}
    cur = {"tasks": {"T1": {"title": "t", "task_size": "L", "task_size_rationale": "coupled interfaces appeared",
                            "profile": "deep", "route": "a", "depends": [], "wave": 1, "base": None, "needs": []}}}
    lines = plan_view.diff(prev, cur, 1)
    assert "  T1 size: S -> L (coupled interfaces appeared)" in lines and "  T1 profile: balanced -> deep" in lines, lines
