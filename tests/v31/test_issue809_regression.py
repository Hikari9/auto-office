"""#809 regression suite: XS-XL gears, size floors, executor slates, inline ownership,
self-review funding, amendments and legacy resume, one assertion per documented gear row."""
import pytest

from conftest import PLAN_ONE, start_inline
from office import config, executor_slates as slates, self_review_subagents as sub, state, task_sizing, xl_review

GEARS = ("XS", "S", "M", "L", "XL")
LOW = {"classification": "low"}
ELEVATED = {"classification": "elevated"}
ESTIMATE = "inline=10 dedicated=20 minutes: the loaded session skips an eight minute brief"


# ------------------------------------------------------------------ gears and funded gates

@pytest.mark.parametrize("gear,plan_review,code_review,planner,goldfish", [
    ("XS", False, False, False, False),
    ("S", False, True, False, False),
    ("M", False, True, False, False),
    ("L", True, True, True, False),
    ("XL", True, True, True, True),
])
def test_every_gear_row_matches_its_funded_gates(gear, plan_review, code_review, planner, goldfish):
    cfg = config.load_yaml(config.default_config_path())
    risk = config.resolve_risk(cfg, "repo", None, False)
    risk.update(size_class=gear, complexity_only=True)
    gates = config.resolve_gates(gear, risk, cfg)
    assert (gates["plan_review"], gates["code_review"], gates["dedicated_planner"]) == (plan_review, code_review, planner)
    assert gates["gear_model_version"] == "run-gear-1" and gates["run_gear"] == gear
    assert bool(cfg["gear_presets"][gear].get("xl_goldfish")) is goldfish


def test_xl_goldfish_is_three_mandatory_and_at_most_two_hazard_audits():
    assert [name for name, _ in xl_review.MANDATORY] == ["requirements", "architecture", "readiness"]
    assert xl_review.MAX_HAZARDS == 2


# ------------------------------------------------------------------ self-review subagents

def _funded(gear, **risk):
    return {"gear": gear, "gates": {"gear_model_version": "run-gear-1"}, "risk": risk}


@pytest.mark.parametrize("gear,expected", [
    ("XS", ()), ("S", ("four-lens",)), ("M", ("four-lens",)),
    ("L", ("edge-cases", "test-strength")), ("XL", ("edge-cases", "test-strength")),
])
def test_subagent_funding_per_gear(gear, expected):
    assert sub.rubrics(_funded(gear), {"scope": ["src/a.py"]}) == expected


@pytest.mark.parametrize("gear", ["L", "XL"])
def test_l_and_xl_fund_up_to_four_specialists(gear):
    run = _funded(gear, classification="elevated")
    task = {"scope": ["scripts/build.sh"], "descriptor": {}}
    assert sorted(sub.rubrics(run, task)) == ["edge-cases", "platform", "security", "test-strength"]
    assert len(sub.rubrics(run, task)) == 4
    assert sub.rubrics(_funded(gear), {"scope": ["src/auth/login.py"]})[0] == "security"
    assert "platform" in sub.rubrics(_funded(gear), {"scope": ["src/a.py"], "descriptor": {"domain": "infrastructure"}})


def test_legacy_pinned_runs_fund_no_subagents():
    legacy = {"gear": "quick", "gates": {"gear_model_version": "legacy-pinned"}, "risk": {}}
    assert sub.rubrics(legacy, {"scope": []}) == ()
    assert not sub.funded({"gear": "L", "gates": {}})


# ------------------------------------------------------------------ size floors and slates

@pytest.mark.parametrize("size,floor", [("XS", 31), ("S", 38), ("M", 40), ("L", 44), ("XL", 47)])
def test_size_floors_are_pinned_and_an_equal_score_passes(size, floor):
    assert slates.FLOORS[size] == floor
    assert slates.benchmark_gate({"benchmark_indexes": {slates.INDEX: floor}}, size) is None
    assert slates.benchmark_gate({"benchmark_indexes": {slates.INDEX: floor - 0.1}}, size)
    assert slates.benchmark_gate({"benchmark_indexes": {}}, size)
    assert slates.benchmark_gate({"benchmark_indexes": {slates.INDEX: floor}}, None) is None


@pytest.mark.parametrize("size,first", [
    ("XS", ("claude-haiku-5-5", "medium")), ("S", ("gemini-3.8-flash", "medium")),
    ("M", ("claude-sonnet-5-5", "medium")), ("L", ("claude-sonnet-5-5", "high")),
    ("XL", ("claude-opus-5-5", "medium")),
])
def test_each_gear_slate_leads_with_its_documented_route(size, first):
    seed = slates.ideal_seed(size)
    assert (seed[0]["model_id"], seed[0]["effort"]) == first
    assert len(seed) == (4 if size in ("M", "L") else 3)


@pytest.mark.parametrize("size,effort", [("M", "medium"), ("L", "high")])
def test_sonnet_falls_back_claude_to_agy_at_the_same_effort_before_any_other_model(size, effort):
    seed = slates.ideal_seed(size)
    assert [(s["model_id"], s["harness"], s["effort"]) for s in seed[:2]] == [
        ("claude-sonnet-5-5", "claude", effort), ("claude-sonnet-5-5", "agy", effort)]
    assert all(s["model_id"] != "claude-sonnet-5-5" for s in seed[2:])
    assert slates.model_restriction({"model_id": "claude-sonnet-5-5", "harness": "agy"}, size) is None
    assert slates.model_restriction({"model_id": "claude-sonnet-5-5", "harness": "opencode"}, size)


@pytest.mark.parametrize("size", ["XS", "S", "M", "L"])
def test_opus_is_reserved_for_xl_subtasks(size):
    assert slates.model_restriction({"model_id": "claude-opus-5-5"}, size)
    assert slates.model_restriction({"model_id": "claude-opus-5-5"}, "XL") is None
    assert [s["model_id"] for s in slates.ideal_seed(size)].count("claude-opus-5-5") == 0


def test_argon_medium_l_exception_is_exact_and_audited():
    medium = {"model_id": "gemini-4-argon", "effort": "medium", "benchmark_indexes": {}}
    audit = slates.floor_exception(medium, "L", "executor")
    assert audit and audit["exception"] == "argon-medium-l-intelligence-floor" and audit["size"] == "L"
    assert slates.floor_exception(medium, "L", "worker")
    for other, size, role in (({**medium, "effort": "high"}, "L", "executor"), (medium, "XL", "executor"),
                              (medium, "M", "executor"), (medium, "L", "code_reviewer"),
                              ({**medium, "model_id": "gpt-6.1-terra"}, "L", "executor"),
                              ({**medium, "benchmark_indexes": {slates.INDEX: 50}}, "L", "executor")):
        assert slates.floor_exception(other, size, role) is None
    assert slates.FLOORS["L"] == 44, "the exception never lowers the floor"


@pytest.mark.parametrize("effort,score", [("none", 33), ("low", 36), ("medium", 41),
                                          ("high", 44), ("xhigh", 45), ("max", 48)])
def test_terra_effort_boundaries_pass_exactly_the_sizes_whose_floor_they_meet(effort, score):
    terra = {"model_id": "gpt-6.1-terra", "effort": effort, "benchmark_indexes": {slates.INDEX: score}}
    passing = [s for s in GEARS if slates.benchmark_gate(terra, s) is None]
    assert passing == [s for s in GEARS if score >= slates.FLOORS[s]]
    assert slates.floor_exception(terra, "L", "executor") is None


# ------------------------------------------------------------------ inline ownership

@pytest.mark.parametrize("gear,size,kwargs,mode", [
    ("XS", "XS", {}, "inline"), ("XS", "S", {}, "inline"), ("XS", "M", {}, "delegated"),
    ("S", "S", {}, "delegated"), ("S", "S", {"inline_advantage": ESTIMATE}, "inline"),
    ("M", "M", {"task_count": 1, "inline_advantage": ESTIMATE}, "inline"),
    ("M", "M", {"task_count": 1}, "delegated"),
    ("M", "M", {"task_count": 2, "inline_advantage": ESTIMATE}, "delegated"),
    ("M", "M", {"task_count": 2}, "delegated"),
    ("L", "XS", {"task_count": 1, "inline_advantage": ESTIMATE}, "delegated"),
    ("XL", "XS", {"task_count": 1, "inline_advantage": ESTIMATE}, "delegated"),
])
def test_inline_ownership_by_gear(gear, size, kwargs, mode):
    descriptor = {"task_size": size}
    if "inline_advantage" in kwargs:
        descriptor["inline_advantage"] = kwargs.pop("inline_advantage")
    result = task_sizing.execution_mode(gear, descriptor, LOW, **kwargs)
    assert result["mode"] == mode and result["owner"] == ("orchestrator" if mode == "inline" else "executor")


@pytest.mark.parametrize("gear", ["XS", "S", "M"])
def test_elevated_risk_never_runs_inline(gear):
    descriptor = {"task_size": "XS", "inline_advantage": ESTIMATE}
    assert task_sizing.execution_mode(gear, descriptor, ELEVATED, task_count=1)["mode"] == "delegated"


def test_an_inline_estimate_that_does_not_beat_dedicated_dispatch_delegates():
    worse = {"task_size": "S", "inline_advantage": "inline=30 dedicated=20 minutes: retries dominate"}
    assert task_sizing.execution_mode("S", worse, LOW)["mode"] == "delegated"


# ------------------------------------------------------------------ amendments and legacy resume

def _run(env):
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    return con, state.get_run(con, rid)


def test_a_contract_amendment_keeps_the_pinned_gear_and_task_size(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE, gear="M")
    env.office("approve", "plan", "--quote", "approved", check=0)
    con, before = _run(env)
    size = state.get_task(con, before["id"], "T1")["descriptor"].get("task_size")
    env.write_plan(PLAN_ONE.replace("done:\n", "done:\n- amended after approval\n", 1))
    env.office("amend", "plan", "--contract", "--", "tighten the done list", check=0)
    con, after = _run(env)
    assert after["gear"] == "M" and after["gates"]["gear_model_version"] == "run-gear-1"
    assert after["gates"]["run_gear"] == "M"
    assert state.get_task(con, after["id"], "T1")["descriptor"].get("task_size") == size


def test_legacy_pinned_run_resumes_without_xs_xl_gear_or_size(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE, gear="express")
    env.office("approve", "plan", "--quote", "approved", check=0)
    con, run = _run(env)
    gates = {k: v for k, v in run["gates"].items() if k not in ("gear_model_version", "run_gear", "run_gear_rationale")}
    state.update_run(con, run["id"], gates=gates)
    con.commit()
    env.office("resume", check=0)
    _, resumed = _run(env)
    assert resumed["gear"] == "express" and resumed["gates"] == gates
    assert slates.new_gear_size({"context": {"gear": resumed["gear"], "size_class": "L"}}) is None
    assert sub.rubrics(resumed, {"scope": ["src/a.py"]}) == ()
    assert task_sizing.execution_mode(resumed["gear"], {"task_size": "XS"}, LOW)["mode"] == "delegated"
