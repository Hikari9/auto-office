"""#809 actual offline route() decisions, not only sizing helper assertions."""
import pytest

from office import executor_slates, routing


def candidate(model, effort, score, *, harness="codex", available=True, remaining=100):
    return {"model_id": model, "invocation_model_id": model,
            "harness": harness, "harness_version": "1.0",
            "effort": effort, "capabilities": ["builder"],
            "invocation_source": "local-evidence: test fixture",
            "benchmark_indexes": ({executor_slates.INDEX: score} if score is not None else {}),
            "quota": {"status": "ok", "tightest_remaining_percent": remaining},
            "route_status": "available" if available else "untried",
            "price_fields": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}


def decide(size, models, *, gear="XL", preference=None):
    return routing.route({
        "role": "worker",
        "playbook": "Change",
        "policy": {"required_capabilities": ["builder"],
                   "floor": {"min_effort": "none",
                             "allowed_sources": ["local-evidence:"],
                             "min_benchmark_index": {
                                 "index_name": executor_slates.INDEX, "min_score": 31}},
                   "quota_reserve_percent": 5},
        "context": {"gear": gear, "size_class": size},
        "allow_free_models": True,
        "cost_policy": "balanced",
        "routing_seed": "issue-809-fixture",
        "adaptive_config": {"exploration": {"rate": 0.0}, "competitive_band": 0.0},
        "preferred_seed": preference or executor_slates.ideal_seed(size),
        "candidates": models,
    })


@pytest.mark.parametrize("size,score,allowed", [
    ("XS", 33, True), ("S", 33, False), ("S", 38, True),
    ("M", 39, False), ("M", 40, True), ("L", 42, False),
    ("L", 44, True), ("XL", 46, False), ("XL", 47, True),
])
def test_real_routing_enforces_index_floor_on_subtask_not_run(size, score, allowed):
    c = candidate("gpt-6.1-terra", "high", score)
    result = decide(size, [c])
    assert (result["status"] == "selected") is allowed, result
    assert bool([r for r in result["rejected"]
                 if r.get("category") == "subtask-intelligence-floor"]) is not allowed


def test_opus_xl_only_even_within_xl_parent_run_and_alias():
    opus = candidate("claude-opus-5-5", "medium", 51, harness="claude")
    terra = candidate("gpt-6.1-terra", "high", 44)
    out = decide("L", [opus, terra])
    assert out["candidate"]["model_id"] == "gpt-6.1-terra", out
    assert any("reserved for XL" in r["reason"] for r in out["rejected"])
    assert decide("XL", [opus])["status"] == "selected"


@pytest.mark.parametrize("size,effort", [("M", "medium"), ("L", "high")])
def test_real_sonnet_harness_self_fallback_and_unavailable_first_route(size, effort):
    score = 41 if size == "M" else 47
    claude = candidate("claude-sonnet-5-5", effort, score, harness="claude")
    agy = candidate("claude-sonnet-5-5", effort, score, harness="agy")
    terra = candidate("gpt-6.1-terra", "medium" if size == "M" else "high",
                      41 if size == "M" else 44)
    preferred = executor_slates.ideal_seed(size)
    selected = decide(size, [claude, agy, terra], preference=preferred)
    if selected["candidate"]["harness"] == "claude":
        assert selected["selection_disclosure"]["slate"][1]["route"].startswith("agy@"), selected
    fallback = decide(size, [dict(claude, route_status="untried"), agy, terra], preference=preferred)
    assert fallback["candidate"]["harness"] == "agy", fallback
    quota = decide(size, [dict(claude, quota={"status": "ok", "tightest_remaining_percent": 0}),
                          agy, terra], preference=preferred)
    assert quota["candidate"]["harness"] == "agy", quota
    retried = decide(size, [agy, terra], preference=preferred)
    assert retried["candidate"]["harness"] == "agy", retried


def test_argon_medium_unpublished_score_passes_the_l_floor_by_explicit_exception():
    argon = candidate("gemini-4-argon", "medium", None, harness="agy")
    out = decide("L", [argon], preference=executor_slates.ideal_seed("L"))
    assert out["status"] == "selected" and out["candidate"]["model_id"] == "gemini-4-argon", out
    assert out["candidate"]["benchmark_indexes"] == {}, "no score is fabricated"
    exceptions = out["routing"]["floor_exceptions"]
    assert [(e["model_id"], e["effort"], e["size"]) for e in exceptions] == [("gemini-4-argon", "medium", "L")]
    assert "registration" in exceptions[0]["not_waived"]


@pytest.mark.parametrize("model,effort,size", [
    ("gemini-4-argon", "high", "L"), ("gemini-4-argon", "medium", "XL"),
    ("gemini-4-argon", "medium", "M"), ("gemini-4-argon", "low", "L"),
    ("future-model", "medium", "L")])
def test_argon_exception_is_exactly_model_effort_and_size_scoped(model, effort, size):
    unscored = candidate(model, effort, None, harness="agy")
    terra = candidate("gpt-6.1-terra", "high", 44 if size == "L" else 48)
    out = decide(size, [unscored, terra])
    assert out["candidate"]["model_id"] == "gpt-6.1-terra", out
    assert any(r.get("stage") == 4 and model in r["candidate"] for r in out["rejected"]), out
    assert "floor_exceptions" not in out["routing"]


def test_argon_exception_never_waives_a_published_below_floor_score_or_other_gates():
    low = candidate("gemini-4-argon", "medium", 40, harness="agy")
    terra = candidate("gpt-6.1-terra", "high", 44)
    out = decide("L", [low, terra])
    assert out["candidate"]["model_id"] == "gpt-6.1-terra", out
    assert any(r.get("stage") == 4 and "gemini" in r["candidate"] for r in out["rejected"]), out
    untried = candidate("gemini-4-argon", "medium", None, harness="agy", available=False)
    assert decide("L", [untried])["status"] == "no_qualifying_candidate"


def test_discovery_never_trials_a_known_below_size_floor_or_non_xl_opus():
    known_low = candidate("gpt-6.1-terra", "low", 36, available=False)
    reason = routing._pool_gate(known_low, {"builder"}, None, "Change", set(),
                                policy_size="S")
    assert reason and reason[0] == 4 and ">= 38" in reason[1]
    blocked_opus = candidate("claude-opus-5-5", "medium", 51, harness="claude",
                              available=False)
    reason = routing._pool_gate(blocked_opus, {"builder"}, None, "Change", set(),
                                policy_size="S")
    assert reason and "reserved for XL" in reason[1]
    qualified = candidate("gpt-6.1-terra", "medium", 41, available=False)
    assert routing._pool_gate(qualified, {"builder"}, None, "Change", set(),
                              policy_size="S") is None
    unknown = candidate("future-model", "medium", None, available=False)
    # Explicit discovery can probe unknown index provenance, but cannot grant
    # ordinary dispatch eligibility: normal route() still fails closed.
    assert routing._pool_gate(unknown, {"builder"}, None, "Change", set(),
                              policy_size="S") is None
    assert decide("S", [unknown])["status"] == "no_qualifying_candidate"


def test_m_l_planner_preserves_all_three_fallbacks_when_slate_has_four():
    from office import adaptive
    routes = ["claude@2/claude-sonnet-5-5@medium",
              "agy@1/claude-sonnet-5-5@medium",
              "codex@0/gpt-6.1-terra@medium",
              "opencode@1/glm-5.3-flash@max"]
    audit = {"slate": [{"route": route} for route in routes],
             "candidates": [{"route": route, "label": route, "utility": 0.80} for route in routes]}
    automatic = adaptive.apply_planner_choice(audit, None)
    assert automatic["primary"] == routes[0] and automatic["fallbacks"] == routes[1:]
    explicit = adaptive.apply_planner_choice(audit, {"routes": routes, "why": "qualified exact routes"})
    assert explicit["fallbacks"] == routes[1:]
    assert len(explicit["fallbacks"]) == 3
