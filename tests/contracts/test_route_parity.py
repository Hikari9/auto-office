"""Pin the AA v5 executor table (Rico, 2026-11-19) and preserve main planner policy."""
from __future__ import annotations

import pytest

from office import candidates, executor_slates as slates, routing


# Literal independent expected owner-approved executor slates (AA v5 table),
# not derived from policy YAML. Planner rows remain unchanged from main.
MAIN_ROUTE_MATRIX = {
    ("executor", "XS"): [("claude", "claude-haiku-5-5", "high"),
                         ("codex", "gpt-6.1-terra", "low"),
                         ("agy", "gemini-4-hydrogen", "low")],
    ("executor", "S"): [("claude", "claude-haiku-5-5", "xhigh"),
                        ("hermes", "claude-haiku-5-5", "xhigh"),
                        ("codex", "gpt-6.1-terra", "medium"),
                        ("agy", "gemini-4-hydrogen", "medium")],
    ("executor", "M"): [("claude", "claude-sonnet-5-5", "high"),
                        ("agy", "gemini-4-hydrogen", "high"),
                        ("codex", "gpt-6.1-terra", "high")],
    ("executor", "L"): [("claude", "claude-sonnet-5-5", "high"),
                        ("hermes", "claude-sonnet-5-5", "high"),
                        ("agy", "claude-sonnet-5-5", "high"),
                        ("codex", "gpt-6.1-terra", "high")],
    ("executor", "XL"): [("claude", "claude-opus-5-5", "high"),
                         ("codex", "gpt-6.2-sol", "high"),
                         ("agy", "gemini-4-neon", "high")],
    ("planner", "XS"): [(None, "opus", "medium"), (None, "astra", "low")],
    ("planner", "S"): [(None, "opus", "medium"), (None, "astra", "low")],
    ("planner", "M"): [
        ("claude", "claude-opus-5-5", "low"),
        ("codex", "gpt-6-astra", "low"),
        (None, "gemini-4-argon", "low"),
    ],
    ("planner", "L"): [
        ("claude", "claude-opus-5-5", "medium"),
        ("codex", "gpt-6-astra", "medium"),
        (None, "gemini-4-argon", "medium"),
    ],
    ("planner", "XL"): [
        ("claude", "claude-opus-5-5", "high"),
        ("codex", "gpt-6-astra", "high"),
        (None, "gemini-4-argon", "high"),
    ],
}


def _route_rows(seed):
    return [(row.get("harness"), row["model_id"], row["effort"]) for row in seed]


def _candidate(model, effort, harness, score=55):
    return {
        "model_id": model,
        "harness": harness,
        "effort": effort,
        "invocation_model_id": model,
        "harness_version": "1.0",
        "capabilities": ["builder"],
        "invocation_source": "local-evidence: contract fixture",
        "benchmark_indexes": {slates.INDEX: score},
        "quota": {"status": "ok", "tightest_remaining_percent": 100},
        "route_status": "available",
        "price_fields": {"input_per_mtok": 1, "output_per_mtok": 2},
    }


@pytest.mark.parametrize("key, expected", MAIN_ROUTE_MATRIX.items())
def test_effective_shipped_routes_match_owner_executor_and_main_planner(key, expected):
    """Test finalized executor seed preferences while retaining planner parity."""
    role, size = key
    run = {"gear": size}
    # Main's planner flat preference is still the fallback below M; the size
    # policy takes over for M/L/XL. Supplying it here avoids reading user config.
    policy_cfg = {"preferred_seed": [{"model_id": "opus", "effort": "medium"},
                                    {"model_id": "astra", "effort": "low"}]} if role == "planner" else {}
    actual = candidates._preferred_seed(policy_cfg, run, size, role=role, executor_policy=slates.POLICY)
    assert _route_rows(actual) == expected

    # Entries without a harness are wildcard preferences; the candidate pool
    # uses one fixture harness so the seed's wildcard matching is exercised.
    pool = [_candidate(model, effort, harness or "claude")
            for harness, model, effort in reversed(expected)]
    decision = routing.route({
        "role": role,
        "playbook": "Change",
        "preferred_seed": actual,
        "slate_anchor": True,
        "allow_free_models": True,
        "policy": {"required_capabilities": ["builder"], "floor": {
            "min_effort": "none", "allowed_sources": ["local-evidence:"]}},
        "context": {"gear": size, "size_class": size},
        "candidates": pool,
    })
    assert decision["status"] == "selected", decision
    expected_harness, expected_model, expected_effort = expected[0]
    assert routing.candidate_id(decision["candidate"]) == (
        f"{expected_harness or 'claude'}@1/{expected_model}@{expected_effort}"
    ), decision


def _haiku_high(score):
    return {"model_id": "claude-haiku-5-5", "harness": "claude", "effort": "high",
            "invocation_model_id": "claude-haiku-5-5", "harness_version": "1.0",
            "capabilities": ["builder"], "invocation_source": "local-evidence: contract fixture",
            "benchmark_indexes": {slates.INDEX: score}, "quota": {"status": "ok"},
            "route_status": "available", "price_fields": {"input_per_mtok": 1, "output_per_mtok": 2}}


def test_haiku_high_has_no_m_floor_exception_but_clears_xs():
    """AA v5: haiku-high-m-owner-authorized-floor is retired. Haiku high is the XS executor on its own score."""
    assert "haiku-high-m-owner-authorized-floor" not in {
        e["name"] for e in slates.POLICY["floor_exceptions"]}
    below_m = _haiku_high(38)
    assert "requires" in slates.benchmark_gate(below_m, "M", slates.POLICY)
    assert slates.floor_exception(below_m, "M", "executor", slates.POLICY) is None
    assert slates.benchmark_gate(_haiku_high(36.5), "XS", slates.POLICY) is None


def test_conditional_hydrogen_m_and_planner_routes_remain_unqualified():
    executor = slates.ideal_seed("M", slates.POLICY)
    planner = slates.planner_seed("M", slates.POLICY)
    assert executor[1] == {"model_id": "gemini-4-hydrogen", "effort": "high", "harness": "agy", "rung": 2}
    assert planner[-1] == {"model_id": "gemini-4-argon", "effort": "low"}
    assert slates.POLICY["executor_preferred"]["M"][1]["_resolved_routes"]
    assert slates.POLICY["executor_preferred"]["M"][1]["_dispatchable_routes"] == []
    # #690: the Argon low row is registered but not dispatchable until its exact conformance qualifies it.
    assert slates.POLICY["planner_preferred"]["M"][-1]["_resolved_routes"] == ["agy/gemini-4-argon@low"]
    assert slates.POLICY["planner_preferred"]["M"][-1]["_dispatchable_routes"] == []
    executor_audit = slates.route_summary(executor, [], [], [])
    planner_audit = slates.route_summary(planner, [], [], [])
    # No runtime candidates were passed, distinct from static catalog resolution.
    assert executor_audit[1]["status"] == "not-in-catalog"
    assert planner_audit[-1]["status"] == "not-in-catalog"


def test_haiku_high_and_medium_are_executors_at_xs_only_under_the_shipped_role_policy():
    """AA v5: Haiku 5.5 high has no executor allowance above XS (role-policy.yaml); XS is allowed, M and L are denied
    (scoreboard #1356: Haiku high M 28% first-try accept), S and XL stay neutral."""
    from office import route_policy
    for effort in ("medium", "high"):
        cand = {"harness": "claude", "model_id": "claude-haiku-5-5", "effort": effort,
                "invocation_model_id": "claude-haiku-5-5", "harness_version": "2.1.329"}
        states = {size: route_policy.effective_role_state(cand, "executor", [], size)["state"] for size in route_policy.SIZES}
        assert states == {"XS": "allow", "S": "neutral", "M": "deny", "L": "deny", "XL": "neutral"}, (effort, states)
    cand = {"harness": "claude", "model_id": "claude-haiku-5-5", "effort": "max",
            "invocation_model_id": "claude-haiku-5-5", "harness_version": "2.1.329"}
    assert {route_policy.effective_role_state(cand, "executor", [], size)["state"]
            for size in route_policy.SIZES} == {"deny"}
    # S canary (#1360): xhigh is allowed at S only.
    cand = {**cand, "effort": "xhigh"}
    assert {size: route_policy.effective_role_state(cand, "executor", [], size)["state"] for size in route_policy.SIZES} == {
        "XS": "deny", "S": "allow", "M": "deny", "L": "deny", "XL": "deny"}
