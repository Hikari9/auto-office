"""#809 final executor slate, versioned task floors, and missing catalog qualifications."""
import pytest
from office import executor_slates as slates
from office import routing


@pytest.mark.parametrize("size,floor", list(slates.FLOORS.items()))
def test_pinned_benchmark_per_real_subtask_size(size, floor):
    candidate = {"benchmark_indexes": {slates.INDEX: floor}}
    assert slates.benchmark_gate(candidate, size) is None
    candidate["benchmark_indexes"][slates.INDEX] = floor - 1
    assert slates.benchmark_gate(candidate, size) is not None
    candidate["benchmark_indexes"] = {"Artificial Analysis Intelligence Index v4.3.1": floor + 50}
    assert "missing" in slates.benchmark_gate(candidate, size)
    candidate["benchmark_indexes"] = {slates.INDEX: None}
    assert slates.benchmark_gate(candidate, size)


@pytest.mark.parametrize("effort,score,largest", [
    ("none", 33, "XS"), ("low", 36, "XS"), ("medium", 41, "M"),
    ("high", 44, "L"), ("xhigh", 45, "L"), ("max", 48, "XL"),
])
def test_terra_effort_thresholds_do_not_claim_qualification(effort, score, largest):
    c = {"model_id": "gpt-6.1-terra", "effort": effort,
         "benchmark_indexes": {slates.INDEX: score}, "route_status": "untried"}
    for size, floor in slates.FLOORS.items():
        assert (slates.benchmark_gate(c, size) is None) == (score >= floor)
    assert c["route_status"] != "available"
    assert largest in slates.FLOORS


def test_sonnet_self_fallback_order_and_same_effort():
    for size, effort in (("L", "high"),):
        seed = slates.ideal_seed(size)
        assert seed[:2] == [
            {"model_id": "claude-sonnet-5-5", "effort": effort, "harness": "claude"},
            {"model_id": "claude-sonnet-5-5", "effort": effort, "harness": "agy"}]
        assert all(x["model_id"] != "claude-sonnet-5-5" for x in seed[2:])


def test_l_argon_medium_remains_conditional_and_xl_high_distinct():
    assert slates.ideal_seed("L")[-1] == {"model_id": "gemini-4-argon", "effort": "medium"}
    assert slates.ideal_seed("XL")[-1] == {"model_id": "gemini-4-argon", "effort": "high"}
    assert slates.benchmark_gate({"benchmark_indexes": {slates.INDEX: 53}, "effort": "high"}, "L") is None
    assert slates.benchmark_gate({"benchmark_indexes": {}, "effort": "medium"}, "L")


def test_opus_is_xl_only_and_sonnet_harness_is_limited():
    for size in ("XS", "S", "M", "L"):
        assert slates.model_restriction({"model_id": "claude-opus-5-5"}, size)
        assert slates.model_restriction({"model_id": "opus", "alias_resolved_to": "claude-opus-5-5"}, size)
    assert slates.model_restriction({"model_id": "claude-opus-5-5"}, "XL") is None
    assert slates.model_restriction({"model_id": "claude-sonnet-5-5", "harness": "codex"}, "M")


def test_conditional_missing_and_unqualified_ideal_routes_are_audited():
    pref = slates.ideal_seed("M")
    c = {"model_id": "claude-haiku-5-5", "harness": "claude", "effort": "high",
         "harness_version": "1", "route_status": "available"}
    cid = routing.candidate_id(c)
    summary = slates.route_summary(pref, [c], [], [{"route": cid, "rank": "PRIMARY"}])
    assert [entry["status"] for entry in summary] == [
        "selected", "not-in-catalog", "not-in-catalog"]
    assert summary[0]["qualified_routes"] == [cid]


def test_old_pinned_gears_do_not_inherit_new_task_size_restrictions():
    for gear in ("direct", "direct+review", "light", "quick", "express", "full"):
        req = {"context": {"gear": gear, "size_class": "S"}}
        assert slates.new_gear_size(req) is None
    assert slates.new_gear_size({"context": {"gear": "XL", "size_class": "S"}}) == "S"
    assert slates.new_gear_size({"context": {"gear": "M", "size_class": "XL"}}) == "XL"


def test_missing_gear_is_not_silently_upgraded_from_task_size():
    assert slates.new_gear_size({"context": {"size_class": "L"}}) is None
    assert slates.new_gear_size({"size_class": "XL"}) is None


def test_issue861_m_slate_and_exact_floor_exception():
    assert slates.ideal_seed("M") == [
        {"model_id": "claude-haiku-5-5", "effort": "high", "harness": "claude"},
        {"model_id": "gpt-6.1-terra", "effort": "medium", "harness": "codex"},
        {"model_id": "glm-5.3-flash", "effort": "max", "harness": "opencode"},
    ]
    route = {"model_id": "claude-haiku-5-5", "harness": "claude", "effort": "high",
             "benchmark_indexes": {slates.INDEX: 38}}
    assert slates.benchmark_gate(route, "M")
    waiver = slates.floor_exception(route, "M", "executor")
    assert waiver and waiver["observed_score"] == 38
    for candidate, size, role in (
        (route, "L", "executor"), (route, "S", "executor"), (route, "M", "planner"),
        ({**route, "harness": "agy"}, "M", "executor"),
        ({**route, "effort": "medium"}, "M", "executor"),
        ({**route, "model_id": "claude-sonnet-5-5"}, "M", "executor"),
        ({**route, "benchmark_indexes": {slates.INDEX: 40}}, "M", "executor"),
    ):
        assert slates.floor_exception(candidate, size, role) is None
