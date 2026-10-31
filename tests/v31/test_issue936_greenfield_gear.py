"""#936: one cohesive greenfield build, not repeated task/plan-review overhead."""

import json

import pytest

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, start_inline
from office import briefs, candidates, config, contract, convergence, executor_slates, greenfield, planfile, routing, self_review_subagents, state, task_sizing

PHASES = """phases:
- Establish the foundation and stable application interfaces
- Build the complete working core with automated tests
- Integrate and validate the end-to-end user flow
"""
PLAN = PLAN_ONE.replace("depends: none\n", "depends: none\n" + PHASES, 1)


def _run(env):
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    return con, state.get_run(con, rid)


def test_explicit_opt_in_pins_a_single_final_independent_review_and_does_not_auto_fit():
    cfg = config.load_yaml(config.default_config_path())
    assert "greenfield" in config.GEARS and "greenfield" not in config.RUN_GEARS
    low = config.resolve_risk(cfg, "repo", None, False)
    gates = config.resolve_gates("greenfield", low, cfg)
    assert gates["gear_model_version"] == "run-gear-1"
    assert gates["review_contract"] == contract.CONVERGENCE
    assert (gates["plan_review"], gates["code_review"], gates["dedicated_planner"]) == (False, True, False)
    assert "one strong executor" in config.gear_rationale("greenfield", low)
    assert config.fit_gear(None, low) == "M", "greenfield must never be selected without --gear"
    elevated = config.resolve_risk(cfg, "production", None, True)
    assert config.resolve_gates("greenfield", elevated, cfg)["code_review"] is True
    assert config.resolve_gates("greenfield", elevated, cfg)["plan_review"] is True
    assert briefs.self_review_tier("greenfield", low) == "single"
    assert briefs.self_review_tier("greenfield", elevated) == "deep"
    assert not self_review_subagents.funded({"gear": "greenfield", "gates": gates})


def test_phases_are_structured_plan_data_and_not_office_tasks():
    parsed = planfile.parse(PLAN)
    assert not parsed.errors, parsed.errors
    assert parsed.tasks[0]["phases"] == [
        "Establish the foundation and stable application interfaces",
        "Build the complete working core with automated tests",
        "Integrate and validate the end-to-end user flow",
    ]
    assert len(parsed.tasks) == 1
    run = {"gear": "greenfield", "gates": {"gear_model_version": "run-gear-1"}}
    assert greenfield.plan_problems(run, parsed.tasks) == []
    assert greenfield.plan_problems({"gear": "XL", "gates": run["gates"]}, parsed.tasks) == []
    sized = task_sizing.classify(parsed.tasks[0], run_gear="greenfield")
    assert sized["task_size"] == "XL" and sized["profile"] == "deep"
    assert sized["task_size_source"] == "greenfield-gear" and not sized["decomposition_recommended"]
    assert task_sizing.execution_mode("greenfield", sized, {"classification": "low"})["mode"] == "delegated"


@pytest.mark.parametrize("plan, expected", [
    (PLAN_ONE, "2-3 `phases:`"),
    (PLAN.replace(PHASES, PHASES.split("- Integrate and validate")[0]), ""),
    (PLAN.replace(PHASES, "phases:\n- foundation only\n"), "2-3 `phases:`"),
    (PLAN.replace(PHASES, PHASES + "- Fourth separate phase\n"), "2-3 `phases:`"),
    (PLAN.replace(PHASES, "phases:\n- Same phase\n- same PHASE\n"), "must be distinct"),
    (PLAN.replace(PHASES, PHASES + "task_size: S\n"), "strong executor"),
    (PLAN_TWO.replace("depends: none\n", "depends: none\n" + PHASES, 1), "exactly one"),
])
def test_invalid_decomposition_is_rejected_before_plan_is_stored(env, plan, expected):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "greenfield", "--planner", "inline")
    assert code == 0, out
    env.write_plan(plan)
    code, out = env.office("submit")
    if not expected:
        assert code == 0, out
        return
    assert code != 0 and expected in out, out
    con, run = _run(env)
    assert state.current_plan(con, run["id"]) is None


def test_greenfield_workflow_uses_one_executor_and_one_terminal_convergence_review(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    out = start_inline(env, plan=PLAN, gear="greenfield")
    con, run = _run(env)
    assert run["gear"] == "greenfield" and run["gates"]["code_review"]
    assert not run["gates"]["plan_review"]
    assert state.get_task(con, run["id"], "T1")["descriptor"]["task_size"] == "XL"
    assert [l["tasks"] for l in convergence.lanes(con, run)] == [["T1"]]
    assert convergence.shared_scopes(con, run) == []
    assert not self_review_subagents.funded(run)
    assert "XL" in out and "T1" in out
    assert env.office("approve", "plan", "--quote", "approved", check=0)[0] == 0
    code, launched = env.office("dispatch", "T1")
    assert code == 0, launched
    calls = [c["role"] for c in env.calls()]
    assert calls.count("executor") == 1, calls
    assert calls.count("convergence_reviewer") == 1, calls
    assert "plan_reviewer" not in calls and "code_reviewer" not in calls, calls
    rows = con.execute("SELECT kind, scope, verdict FROM gates WHERE kind IN ('self_review_subagent', 'convergence_review')").fetchall()
    assert [(r["kind"], r["scope"], r["verdict"]) for r in rows] == [
        ("convergence_review", "L-T1", "APPROVED")]
    assert state.get_task(con, run["id"], "T1")["status"] == "accepted"
    # The launch brief must actually carry the named phases, not merely save them in a plan record.
    from office import paths
    dispatch_id = con.execute("SELECT id FROM dispatches WHERE run_id=? AND role='executor'", (run["id"],)).fetchone()[0]
    text = (paths.run_dir(run["id"]) / "dispatches" / dispatch_id / "brief.md").read_text()
    assert "GREENFIELD one cohesive build" in text and "single executor session" in text
    for phase in planfile.parse(PLAN).tasks[0]["phases"]:
        assert phase in text


def test_start_accepts_greenfield_as_an_explicit_gear_choice():
    from office import cli
    assert cli._parser().parse_args(["start", "goal", "--gear", "greenfield"]).gear == "greenfield"
    assert cli._parser().parse_args(["start", "goal"]).gear is None


def test_phase_revision_is_acceptance_change_and_never_silently_reuses_prior_approval(env):
    from office import db, plans
    env.trust()
    start_inline(env, plan=PLAN, gear="greenfield")
    con, run = _run(env)
    original = state.get_task(con, run["id"], "T1")
    new_body = PLAN.replace("Integrate and validate the end-to-end user flow",
                            "Integrate, document and verify an end-to-end user flow")
    with db.transaction(con):
        changed = plans.sync_tasks(con, run, planfile.parse(new_body).tasks, run["plan_version"] + 1)
    after = state.get_task(con, run["id"], "T1")
    assert changed["acceptance"] == ["T1"]
    assert after["acceptance_version"] == run["plan_version"] + 1
    assert after["contract_version"] == run["plan_version"] + 1
    assert original["acceptance_version"] == run["plan_version"]


def test_phases_only_edit_is_not_acceptance_outside_greenfield(env):
    from office import db, plans
    env.trust()
    start_inline(env, plan=PLAN, gear="XL")
    con, run = _run(env)
    original = state.get_task(con, run["id"], "T1")
    new_body = PLAN.replace("Integrate and validate the end-to-end user flow",
                            "Integrate, document and verify an end-to-end user flow")
    with db.transaction(con):
        changed = plans.sync_tasks(con, run, planfile.parse(new_body).tasks, run["plan_version"] + 1)
    after = state.get_task(con, run["id"], "T1")
    assert "T1" not in changed["acceptance"], changed
    assert after["acceptance_version"] == original["acceptance_version"]


def test_greenfield_does_not_allow_old_per_task_review_contract():
    cfg = config.load_yaml(config.default_config_path())
    cfg["review"]["contract"] = "v3.1"
    with pytest.raises(ValueError, match="requires the convergence-v1"):
        config.resolve_gates("greenfield", config.resolve_risk(cfg, "repo", None, False), cfg)


def test_gates_and_model_size_are_pinned_not_retroactively_applied_to_legacy():
    run = {"gear": "greenfield", "gates": {"gear_model_version": "legacy-pinned"}}
    assert not greenfield.enabled(run)
    assert greenfield.plan_problems(run, []) == []
    assert not self_review_subagents.funded(run)


def test_plan_amendment_cannot_drop_phases_after_initial_approval(env):
    env.trust()
    start_inline(env, plan=PLAN, gear="greenfield")
    con, run = _run(env)
    env.write_plan(PLAN.replace(PHASES, ""))
    code, out = env.office("amend", "plan", "--", "revise the greenfield phase breakdown")
    assert code != 0 and "greenfield plan has problems" in out and "2-3 `phases:`" in out, out
    assert state.current_plan(con, run["id"])["version"] == 1


def test_contract_amendment_cannot_drop_greenfield_phases(env):
    env.trust()
    start_inline(env, plan=PLAN, gear="greenfield")
    con, run = _run(env)
    env.write_plan(PLAN.replace(PHASES, ""))
    code, out = env.office("amend", "T1", "--contract", "--", "drop the phase breakdown")
    assert code != 0 and "plan has problems" in out and "2-3 `phases:`" in out, out
    assert state.current_plan(con, run["id"])["version"] == 1


def test_greenfield_task_cannot_depend_on_another_task():
    task = {"id": "T1", "depends": ["T0"], "phases": ["foundation", "working core"]}
    assert greenfield.plan_problems(GREENFIELD_RUN, [task]) == [
        "greenfield task cannot depend on another task; all phases belong to the same build"]


def _executor_candidate(model, effort, harness):
    return {"harness": harness, "harness_version": "1", "model_id": model, "invocation_model_id": model,
            "effort": effort, "capabilities": ["builder"], "route_status": "available",
            "quota": {"status": "ok", "tightest_remaining_percent": 80},
            "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1},
            "benchmark_indexes": {executor_slates.POLICY["benchmark_index"]: 60}}


XL_CANDIDATES = [_executor_candidate("claude-opus-5-5", "medium", "claude"),
                 _executor_candidate("gpt-6.1-sol", "medium", "codex"),
                 _executor_candidate("gemini-4-argon", "high", "gemini")]
GREENFIELD_RUN = {"gear": "greenfield", "gates": {"gear_model_version": "run-gear-1"}, "risk": {}}
OPUS_DENY = {"role_rules": [{"role": "executor", "route": "claude/claude-opus-5-5@medium", "state": "deny"}],
             "denied": [], "overkill": [], "ceilings": {}, "sources": {"role_rules": "user"}}


@pytest.mark.parametrize("unavailable", ["denied", "catalog-disabled"])
@pytest.mark.parametrize("slate", ["full", "only-entry"])
def test_xl_slate_entry_denied_or_disabled_falls_back_to_next_eligible_route_and_is_named(unavailable, slate):
    sized = task_sizing.classify(planfile.parse(PLAN).tasks[0], run_gear="greenfield")
    seed = executor_slates.ideal_seed(sized["task_size"])
    if slate == "only-entry":
        seed = seed[:1]
    denied = unavailable == "denied"
    pool = XL_CANDIDATES if denied else XL_CANDIDATES[1:]
    result = routing.route({"role": "executor", "candidates": pool, "preferred_seed": seed,
                            "policy": {"floor": None, "strict_seed": False, "required_capabilities": ["builder"]},
                            "user_policies": [OPUS_DENY] if denied else [], "runs_db": None,
                            "context": {"gear": candidates._slate_gear(GREENFIELD_RUN)},
                            "size_class": sized["task_size"]})
    assert result["status"] == "selected", result
    assert result["selected"] == "codex@1/gpt-6.1-sol@medium", result["selected"]
    skipped = executor_slates.skip_lines(executor_slates.route_summary(seed, pool, result["rejected"], result["slate"]))
    first = [line for line in skipped if line.startswith("#1 claude/claude-opus-5-5@medium")]
    assert len(first) == 1, skipped
    assert ("deny" if denied else "not-in-catalog") in first[0], first[0]


def test_greenfield_executor_gets_the_xl_size_slate_and_floor_in_route_context(env):
    env.trust()
    start_inline(env, plan=PLAN, gear="greenfield")
    con, run = _run(env)
    assert candidates._uses_size_slate({}, run, "XL", "executor")
    assert candidates._preferred_seed({}, run, "XL", "executor") == executor_slates.ideal_seed("XL")
    # The route request is built by adaptive_inputs: its context must carry the XL slate gear, not "greenfield".
    cfg = config.load_yaml(config.default_config_path())
    request = candidates.adaptive_inputs(con, cfg, run, "executor", [], task_id="T1", dispatch_kind="executor")
    assert request["context"]["gear"] == "XL", request["context"]
    assert executor_slates.new_gear_size({"context": request["context"]}) == "XL"
    legacy = {"gear": "greenfield", "gates": {"gear_model_version": "legacy-pinned"}, "risk": {}}
    assert not candidates._uses_size_slate({}, legacy, "XL", "executor")
    assert candidates._slate_gear(legacy) == "greenfield"
