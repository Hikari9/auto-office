"""Pin shipped effective route preferences to the behavior on main (#871/#861)."""
from __future__ import annotations

import pytest

from office import candidates, executor_slates as slates


# This is the route matrix from main's #809/#850 policy plus #861's M executor
# amendment. Keep it literal: reading it from the shipped policy would make this
# comparison tautological.
MAIN_ROUTE_MATRIX = {
    ("executor", "XS"): [
        ("claude", "claude-haiku-5-5", "medium"),
        (None, "gemini-3.8-flash", "low"),
        ("codex", "gpt-6.1-terra", "low"),
    ],
    ("executor", "S"): [
        (None, "gemini-3.8-flash", "medium"),
        ("claude", "claude-haiku-5-5", "high"),
        ("codex", "gpt-6.1-terra", "medium"),
    ],
    ("executor", "M"): [
        ("claude", "claude-haiku-5-5", "high"),
        ("opencode", "glm-5.3-flash", "max"),
        ("codex", "gpt-6.1-terra", "medium"),
    ],
    ("executor", "L"): [
        ("claude", "claude-sonnet-5-5", "high"),
        ("agy", "claude-sonnet-5-5", "high"),
        ("codex", "gpt-6.1-terra", "high"),
        (None, "gemini-4-argon", "medium"),
    ],
    ("executor", "XL"): [
        ("claude", "claude-opus-5-5", "medium"),
        ("codex", "gpt-6.1-sol", "medium"),
        (None, "gemini-4-argon", "high"),
    ],
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


@pytest.mark.parametrize("key, expected", MAIN_ROUTE_MATRIX.items())
def test_effective_shipped_routes_match_main(key, expected):
    """The policy refactor preserves main's effective route matrix, including conditionals."""
    role, size = key
    run = {"gear": size}
    # Main's planner flat preference is still the fallback below M; the size
    # policy takes over for M/L/XL. Supplying it here avoids reading user config.
    policy_cfg = {"preferred_seed": [{"model_id": "opus", "effort": "medium"},
                                    {"model_id": "astra", "effort": "low"}]} if role == "planner" else {}
    actual = candidates._preferred_seed(policy_cfg, run, size, role=role, executor_policy=slates.POLICY)
    assert _route_rows(actual) == expected


def test_m_executor_haiku_high_keeps_its_floor_exception():
    candidate = {"model_id": "claude-haiku-5-5", "harness": "claude", "effort": "high",
                 "benchmark_indexes": {slates.INDEX: 38}}
    assert slates.benchmark_gate(candidate, "M", slates.POLICY)
    waiver = slates.floor_exception(candidate, "M", "executor", slates.POLICY)
    assert waiver and waiver["observed_score"] == 38
    assert waiver["model_id"] == "claude-haiku-5-5"


def test_conditional_m_routes_match_main_even_when_absent_from_catalog():
    executor = slates.ideal_seed("M", slates.POLICY)
    planner = slates.planner_seed("M", slates.POLICY)
    assert executor[1] == {"model_id": "glm-5.3-flash", "effort": "max", "harness": "opencode"}
    assert planner[-1] == {"model_id": "gemini-4-argon", "effort": "low"}
    assert slates.POLICY["executor_preferred"]["M"][1]["_resolved_routes"] == []
    assert slates.POLICY["planner_preferred"]["M"][-1]["_resolved_routes"] == []
