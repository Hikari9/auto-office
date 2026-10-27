"""#809 XS-XL run classification, per-task routing, and genuine XL audit fanout."""
import json

import pytest

from conftest import PLAN_ONE, PLAN_TWO, start_inline
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
    assert config.LEGACY_EQUIVALENT == {"direct": "XS", "direct+review": "S", "light": "S", "quick": "M",
                                        "express": "M", "full": "XL"}
    _, run = _run(env)
    assert run["gear"] == "full" and run["gates"]["gear_model_version"] == "legacy-pinned"
    assert not run["risk"].get("complexity_only"), "a legacy start keeps its historical size and risk reading"


def _strip_gear_model(con, rid):
    """A run pinned before XS-XL existed: its gates JSON has none of the gear-model keys."""
    run = state.get_run(con, rid)
    gates = {k: v for k, v in run["gates"].items()
             if k not in ("gear_model_version", "run_gear", "run_gear_rationale")}
    state.update_run(con, rid, gates=gates)
    con.commit()
    return state.get_run(con, rid)["gates"]


def test_legacy_pinned_run_resumes_with_its_original_gear_and_gates(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE, gear="express")
    env.office("approve", "plan", "--quote", "approved", check=0)
    con, run = _run(env)
    before = _strip_gear_model(con, run["id"])
    env.office("resume", check=0)
    code, out = env.office("inspect", "plan")
    _, after = _run(env)
    assert code == 0 and after["gear"] == "express" and after["gates"] == before
    assert "gear_model_version" not in after["gates"]
    from office import plan_view
    pv = plan_view.preview(env.con(), after, state.current_plan(env.con(), after["id"])["tasks"])
    assert pv["gear_model_version"] == "legacy-pinned", "a run with no gear-model key is never treated as XS-XL"
    assert plan_view.render(after, 1, pv)[3].endswith("legacy pinned run; no new gear fit applied")
    # A plan revision on that run must not give its tasks an XS-XL size or profile.
    env.write_plan(PLAN_ONE.replace("done:\n", "done:\n- legacy run revised after the XS-XL upgrade\n", 1))
    env.office("amend", "plan", "--contract", "--", "revise the plan", check=0)
    desc = state.get_task(env.con(), after["id"], "T1")["descriptor"]
    assert not {"task_size", "profile", "profile_version", "task_size_source"} & set(desc), desc


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
        assert f"planner recommends gear {rec} (advisory; pinned gear stays {intake})" in env.office("inspect", "plan")[1]


def test_recommendation_below_the_risk_floor_is_refused_even_at_the_intake_gear(env):
    env.trust()
    env.office("start", "fixture goal", "--gear", "XS", "--planner", "inline", "--blast-radius", "local", check=0)
    con, run = _run(env)
    assert run["risk"]["classification"] == "low"
    env.write_plan(_plan_with(REC.format(gear="XS")).replace("blast_radius: repo", "blast_radius: production"))
    code, out = env.office("submit")
    # Intake called it low and XS; the plan's production blast radius raises risk, so XS is now under the floor.
    assert code != 0 and "run gear recommendation refused" in out and "below the S floor" in out, out
    assert not state.current_plan(con, run["id"])


def test_a_low_risk_recommendation_at_the_intake_gear_is_accepted(env):
    env.trust()
    env.office("start", "fixture goal", "--gear", "XS", "--planner", "inline", "--blast-radius", "local", check=0)
    env.write_plan(_plan_with(REC.format(gear="XS")))
    code, out = env.office("submit")
    assert code == 0 and "planner recommends run gear XS" in out, out


def test_risk_module_run_gear_problem_is_order_and_risk_aware():
    low = {"classification": "low"}
    elevated = {"classification": "elevated"}
    assert risk.run_gear_problem("M", low, "L") is None
    assert risk.run_gear_problem("M", low, "S") and "intake gear M" in risk.run_gear_problem("M", low, "S")
    assert risk.run_gear_problem("XS", elevated, "XS") and "floor" in risk.run_gear_problem("XS", elevated, "XS")
    assert risk.run_gear_problem("XS", elevated, "S") is None
    unknown = {"classification": "unknown"}
    assert "floor" in risk.run_gear_problem("XS", unknown, "XS") and risk.run_gear_problem("XS", unknown, "S") is None
    assert risk.run_gear_problem("XS", low, "XS") is None
    assert risk.run_gear_problem("full", low, "XS") is None  # a legacy pin has no gear order to undercut
    assert risk.run_gear_problem("S", low, "XXL")


# ---------------------------------------------------------------- R4-R8: dispatch output and routing audit

def test_dispatch_prints_run_gear_funded_gates_size_profile_and_actual_route(env):
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S\n"
                            "task_size_rationale: one bounded file with no interfaces\n", 1)
    out = _approved_dispatch(env, body, "M")
    assert "RUN DISPATCH gear M" in out and "funded gates: plan review=no, convergence=yes" in out, out
    assert "T1 S | profile balanced | why: one bounded file with no interfaces" in out, out
    con, run = _run(env)
    route = state.recorded_route(state.get_task(con, run["id"], "T1"))["candidate"]
    dispatched = con.execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at LIMIT 1").fetchone()[0]
    line = next(l for l in out.splitlines() if l.startswith("ACTUAL DISPATCH T1: "))
    assert line.startswith(f"ACTUAL DISPATCH T1: {dispatched} | ") and route, line
    # S's ideal slate starts with Gemini Flash at medium: every ideal that did not run says why.
    skipped = [l for l in out.splitlines() if l.startswith("  ideal route skipped: #")]
    ideals = executor_slates.ideal_seed("S")
    assert len(skipped) in (len(ideals) - 1, len(ideals)), out  # the adaptive pick is one of the ideals, or none of them
    for ideal in ideals[1:]:  # the Gemini ideal may be the pick; the Haiku and Terra ideals are discovery-only
        ref = f"{ideal.get('harness', '*')}/{ideal['model_id']}@{ideal['effort']}"
        assert any(f"{ref} (unqualified)" in l for l in skipped), (ref, skipped)
    assert all(" (selected)" not in l for l in skipped), skipped


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
    planned = json.loads(con.execute("SELECT preview_json FROM plans WHERE run_id=?", (run["id"],)).fetchone()[0])["tasks"]["T1"]
    assert audit["preferred_route"] == planned["route_plan"]["primary"]
    assert audit["fallback_routes"] == planned["route_plan"]["fallbacks"] and audit["fallback_routes"]
    assert audit["preferred_route_reason"] == audit["selection_reason"]
    assert audit["selected_route"] == audit["dispatch"]["dispatched"]
    assert audit["effective_gates"] == {k: run["gates"].get(k) for k in (
        "plan_review", "code_review", "visual", "review_floor", "review_contract")}
    assert audit["effective_gates"]["code_review"] is True and audit["effective_gates"]["plan_review"] is False
    assert audit["effective_gates"]["review_contract"] == contract.CONVERGENCE
    assert audit["plan_version"] == run["plan_version"] == 1
    assert audit["wave"] == 1 and audit["dispatch_version"] == 1
    # The same facts ride on the task's stored route, so a relaunch reproduces them.
    stored = json.loads(con.execute("SELECT route_json FROM tasks WHERE run_id=? AND id='T1'", (run["id"],)).fetchone()[0])
    assert stored["selected_route"] == audit["selected_route"] and stored["size_decision"]["task_size"] == "S"
    assert stored["preferred_route"] == audit["preferred_route"] and stored["fallback_routes"] == audit["fallback_routes"]
    # The harness ended without submitting, so the task was relaunched: every later audit keeps the ranking.
    later = [json.loads(r[0]) for r in con.execute(
        "SELECT disclosure_json FROM route_audit WHERE run_id=? AND phase='dispatch' ORDER BY created_at", (run["id"],))]
    assert all(a["preferred_route"] == audit["preferred_route"] and a["fallback_routes"] == audit["fallback_routes"]
               for a in later), [(a["preferred_route"], a["fallback_routes"]) for a in later]
    # A later decision for the same task is dispatch 2, from the dispatches already recorded.
    from office import dispatch
    decision = {"request": {"role": "executor", "task_id": "T1", "context": {"size_class": "S"}}, "routing": {"slate": []},
                "selected": audit["selected_route"], "selection_disclosure": {"reason": "second try"}}
    with con:
        dispatch._record_routing(con, state.get_run(con, run["id"]), decision)
    again = json.loads(con.execute("SELECT disclosure_json FROM route_audit WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                                   (run["id"],)).fetchone()[0])
    assert again["dispatch_version"] == len(state.task_dispatches(con, run["id"], "T1")) + 1 >= 2
    assert again["preferred_route"] == audit["selected_route"] and again["fallback_routes"] == []


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
    task = json.loads(con.execute("SELECT preview_json FROM plans WHERE run_id=?", (run["id"],)).fetchone()[0])["tasks"]["T1"]
    slate = task["slate"]
    assert len(slate) >= 2
    assert task["fallback_routes"] == slate[1:] and task["preferred_route"], task
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


# ---------------------------------------------------------------- self-review fixes

def test_inferred_size_is_marked_and_never_counts_as_a_planner_declared_size():
    base = {"id": "T1", "scope": ["a.py"], "depends": [], "interfaces": []}
    inferred = task_sizing.classify({**base, "descriptor": {"bounds": "bounded"}})
    assert inferred["task_size_source"] == "inferred" and inferred["task_size"] in task_sizing.SIZES
    declared = task_sizing.classify({**base, "descriptor": {"task_size": "S", "bounds": "bounded"}})
    assert declared["task_size_source"] == "planner"
    assert "task_size" not in task_sizing.planner_declared({"bounds": "bounded", **inferred})
    assert task_sizing.planner_declared({"bounds": "bounded", **declared})["task_size"] == "S"
    assert task_sizing.planner_declared(None) == {}
    # A descriptor Office sized before the source key existed is still Office's size.
    legacy_sized = {"task_size": "M", "bounds": "bounded", "profile_version": task_sizing.PROFILE_VERSION}
    assert "task_size" not in task_sizing.planner_declared(legacy_sized)
    assert task_sizing.planner_declared({"task_size": "S", "bounds": "bounded"})["task_size"] == "S"


def test_production_run_discovery_trial_needs_a_size_the_planner_declared():
    from office import route_policy
    settings = route_policy.discovery_settings({"routing": {"discovery": {"trial_bounded_production_tasks": True}}})
    risk_record = {"blast_radius": "production", "irreversible": False, "size_class": "L"}
    base = {"id": "T1", "scope": ["a.py"], "depends": [], "interfaces": []}
    inferred = {"bounds": "bounded", **task_sizing.classify({**base, "descriptor": {"bounds": "bounded"}})}
    declared = {"bounds": "bounded", **task_sizing.classify({**base, "descriptor": {"task_size": "S", "bounds": "bounded"}})}
    assert route_policy.trial_risk_blocked(settings, risk_record, task_sizing.planner_declared(inferred))
    assert not route_policy.trial_risk_blocked(settings, risk_record, task_sizing.planner_declared(declared))


def test_a_revision_that_drops_the_run_gear_recommendation_clears_it(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "S", "--planner", "inline", "--blast-radius", "local")
    assert code == 0, out
    env.write_plan(_plan_with(REC.format(gear="L")))
    assert env.office("submit")[0] == 0
    assert _run(env)[1]["gates"]["run_gear_recommendation"]["plan_version"] == 1
    env.write_plan(PLAN_ONE.replace("done:\n", "done:\n- the revision drops the recommendation\n", 1))
    code, out = env.office("submit")
    assert code == 0 and "recommends run gear" not in out, out
    assert "run_gear_recommendation" not in _run(env)[1]["gates"]
    assert "planner recommends" not in env.office("inspect", "plan")[1]


def test_sonnet_claude_to_agy_self_fallback_applies_only_to_xs_xl_runs():
    from test_issue809_real_routes import candidate, decide
    pool = [candidate("claude-sonnet-5-5", "medium", 45, harness="claude"),
            candidate("claude-sonnet-5-5", "medium", 41, harness="agy", remaining=60),
            candidate("gpt-6.1-terra", "medium", 44),
            candidate("glm-5.3-flash", "max", 42, harness="opencode")]
    new = decide("M", pool, gear="M")
    assert [e["route"].split("@")[0] for e in new["selection_disclosure"]["slate"][:2]] == ["claude", "agy"]
    assert new["routing"]["sonnet_self_fallbacks"][0]["to"].startswith("agy@")
    legacy = decide("M", pool, gear="express")
    assert "sonnet_self_fallbacks" not in legacy["routing"]
    assert len(legacy["selection_disclosure"]["slate"]) == 3


def test_planner_free_text_rejects_control_characters():
    from office import task_descriptors
    for bad in ("evidence with \x1b[2K escape", "evidence with \r carriage return", "evidence with \x7f delete",
                "evidence with \x9b2J c1 csi", "evidence with \u202e bidi override", "evidence with \u200b zero width",
                "evidence with \u061c arabic mark", "evidence with \U000e0041 tag char", "evidence with \ufeff bom"):
        with pytest.raises(ValueError):
            task_descriptors.parse_field("task_size_rationale", bad)
        if "\r" in bad:
            continue  # a carriage return ends the plan line, so nothing after it can ride on the value
        plan = planfile.parse(_plan_with(f"run_gear: L\nrun_gear_why: {bad}\n"))
        assert any("no control characters" in e for e in plan.errors), plan.errors
    assert task_descriptors.parse_field("task_size_rationale", "one bounded reversible file") == "one bounded reversible file"
    for fine in ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 persian word", "family \U0001F468\u200d\U0001F469\u200d\U0001F467 emoji"):
        assert task_descriptors.parse_field("task_size_rationale", fine) == fine


def test_plan_warns_about_unpaired_size_and_gear_fields():
    orphan = planfile.parse(_plan_with("run_gear_why: coupled interfaces across three packages\n"))
    assert any("without `run_gear:`" in w for w in orphan.warnings), orphan.warnings
    no_why = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S", 1)
    assert any("no `task_size_rationale:`" in w for w in planfile.parse(no_why).warnings)
    no_size = PLAN_ONE.replace("visual: none", "visual: none\ntask_size_rationale: a bounded single file change", 1)
    assert any("without `task_size:`" in w for w in planfile.parse(no_size).warnings)
    assert planfile.parse(_plan_with("run_gear: l   (optional note)\nrun_gear_why: three coupled packages here\n")
                          ).requirements["run_gear"]["gear"] == "L"



def test_planner_size_raise_is_complexity_not_risk_for_xs_xl_runs_only():
    xs_xl = {"classification": "low", "classified_by": "intake", "blast_radius": "repo", "size_class": "M",
             "complexity_only": True, "high": False, "irreversible": False}
    assert risk.planner_reclassify(xs_xl, {"size_class": "XL"}) is None or \
        not risk.planner_reclassify(xs_xl, {"size_class": "XL"})["high"]
    legacy = {k: v for k, v in xs_xl.items() if k != "complexity_only"}
    raised = risk.planner_reclassify(legacy, {"size_class": "XL"})
    assert raised and raised["high"] and raised["classification"] == "elevated"
    assert risk.planner_reclassify(xs_xl, {"blast_radius": "production"})["high"], "blast radius still raises risk"


def test_intake_fit_uses_declared_size_then_signals_and_never_guesses_xs():
    assert config.fit_gear(None, {"size_class": "S"}) == "S"
    assert config.fit_gear(None, {}) == "M"
    assert config.fit_gear(None, {}, volume=True) == "M"
    assert config.fit_gear(None, {}, volume=True, interview=True) == "L"
    assert config.fit_gear(None, {}, adversarial=True, interview=True, volume=True) == "L"
    assert config.fit_gear("full", {"size_class": "S"}) == "full"
    assert "multiwave volume and discovery/interview" in config.gear_rationale("L", {}, volume=True, interview=True)
    assert "conservative M" in config.gear_rationale("M", {})
    assert config.gear_rationale("S", {"size_class": "S"}).startswith("intake complexity S")


def test_replan_delta_is_silent_for_an_unchanged_task():
    from office import plan_view
    task = {"title": "t", "task_size": "S", "profile": "balanced", "route": "a", "depends": [], "wave": 1,
            "base": None, "needs": []}
    assert plan_view.diff({"tasks": {"T1": dict(task)}}, {"tasks": {"T1": dict(task)}}, 1) == ["diagram unchanged from p1"]
    unsized = {k: v for k, v in task.items() if k not in ("task_size", "profile")}
    lines = plan_view.diff({"tasks": {"T1": unsized}}, {"tasks": {"T1": dict(task)}}, 1)
    assert "  T1 size: unknown -> S (new evidence)" in lines and "  T1 profile: unknown -> balanced" in lines, lines


def test_audit_wave_follows_the_stored_plan_layout(env):
    from office import plan_view
    env.trust()
    stacked = PLAN_TWO.replace("### T2: Implement mul\nscope: mul.py\ndepends: none",
                               "### T2: Implement mul\nscope: mul.py\ndepends: T1")
    start_inline(env, plan=stacked, gear="M")
    con, run = _run(env)
    assert [plan_view.task_wave(con, run, t) for t in ("T1", "T2", "T9", None)] == [1, 2, None, None]
    code, out = env.office("approve", "plan", "--quote", "approved")
    assert code == 0, out
    code, out = env.office("dispatch", "T1", "T2")
    assert code == 0, out
    waves = {r[0]: r[1] for r in con.execute(
        "SELECT task_id, json_extract(disclosure_json, '$.wave') FROM route_audit WHERE phase='dispatch'")}
    assert waves == {"T1": 1, "T2": 2}, waves


def test_preferred_routes_prefer_the_plan_then_the_router_slate_then_the_selection():
    from office import dispatch
    planned = {"planned": {"primary": "a@1/x@low", "fallbacks": ["b@1/y@low"]},
               "routing": {"slate": [{"route": "r1"}, {"route": "r2"}]}, "selected": "z"}
    assert dispatch._preferred_routes(planned) == ("a@1/x@low", ["b@1/y@low"])
    assert dispatch._preferred_routes({**planned, "planned": {}}) == ("r1", ["r2"])
    assert dispatch._preferred_routes({"selected": "z"}) == ("z", [])
    # A relaunch decision is rebuilt from the stored route: it keeps what the first dispatch ranked.
    stored = {"preferred_route": "a@1/x@low", "fallback_routes": ["b@1/y@low"], "selected": "z"}
    assert dispatch._preferred_routes(stored) == ("a@1/x@low", ["b@1/y@low"])
    # ...even though that decision's fresh exact-route slate ranks only the selected route.
    assert dispatch._preferred_routes({**stored, "routing": {"slate": [{"route": "z"}]}}) == ("a@1/x@low", ["b@1/y@low"])

