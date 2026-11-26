"""Executor scoreboard (#1356): the XS/S/L slates and the executor role rules that followed it.
Literal expectations, not derived from the YAML."""
import pytest

from office import executor_slates, route_policy

MAJOR = {"claude": "2.1.311", "agy": "1.0.0", "codex": "0.157.1", "hermes": None, "opencode": None}


def state(harness, model, effort, size, role="executor"):
    cand = {"harness": harness, "harness_version": MAJOR[harness], "model_id": model,
            "invocation_model_id": model, "effort": effort}
    return route_policy.effective_role_state(cand, role, [], size)["state"]


def slate(size):
    return [(r["rung"], r["harness"], r["model_id"], r["effort"], bool(r.get("conditional")))
            for r in executor_slates.load_policy()["executor_preferred"][size]]


def test_xs_slate_is_haiku_then_terra_then_conditional_hydrogen():
    assert slate("XS") == [(1, "claude", "claude-haiku-5-5", "high", False), (2, "codex", "gpt-6.1-terra", "low", False),
                           (3, "agy", "gemini-4-hydrogen", "low", True)]


def test_s_slate_is_sonnet_then_terra_then_conditional_hydrogen():
    assert slate("S") == [(1, "claude", "claude-sonnet-5-5", "medium", False), (2, "codex", "gpt-6.1-terra", "medium", False),
                          (3, "agy", "gemini-4-hydrogen", "medium", True)]


def test_m_slate_is_unchanged():
    assert slate("M") == [(1, "claude", "claude-sonnet-5-5", "high", False), (2, "agy", "gemini-4-hydrogen", "high", True),
                          (3, "codex", "gpt-6.1-terra", "high", False)]


def test_l_slate_is_sonnet_high_on_claude_hermes_agy_then_terra_with_no_opus():
    rows = slate("L")
    assert rows == [(1, "claude", "claude-sonnet-5-5", "high", False), (2, "hermes", "claude-sonnet-5-5", "high", False),
                    (3, "agy", "claude-sonnet-5-5", "high", False), (4, "codex", "gpt-6.1-terra", "high", False)]
    assert not any("opus" in model or "sol" in model or "neon" in model for _, _, model, _, _ in rows)


def test_xl_slate_is_the_previous_opus_sol_neon_ladder():
    assert slate("XL") == [(1, "claude", "claude-opus-5-5", "high", False), (2, "codex", "gpt-6.2-sol", "high", False),
                           (3, "agy", "gemini-4-neon", "high", False)]
    assert executor_slates.ideal_seed("XL") == [
        {"model_id": "claude-opus-5-5", "effort": "high", "harness": "claude", "rung": 1},
        {"model_id": "gpt-6.2-sol", "effort": "high", "harness": "codex", "rung": 2},
        {"model_id": "gemini-4-neon", "effort": "high", "harness": "agy", "rung": 3}]


def test_sonnet_may_run_on_claude_agy_and_hermes_only():
    [rule] = [r for r in executor_slates.load_policy()["model_restrictions"] if r["model_id"] == "claude-sonnet-5-5"]
    assert rule["allowed_harnesses"] == ["claude", "agy", "hermes"]
    assert "Hermes" in rule["reason"]


@pytest.mark.parametrize("harness, model, effort, size, expected", [
    ("codex", "gpt-6.1-terra", "medium", "S", "allow"),
    ("codex", "gpt-6.1-terra", "medium", "M", "deny"),
    ("codex", "gpt-6.1-terra", "medium", "L", "deny"),
    ("codex", "gpt-6-luna", "high", "M", "deny"),
    ("codex", "gpt-6-luna", "high", "L", "deny"),
    ("opencode", "glm-5.3-flash", "max", "XS", "allow"),
    ("opencode", "glm-5.3-flash", "max", "M", "deny"),
    ("opencode", "glm-5.3-flash", "max", "L", "deny"),
    ("hermes", "claude-sonnet-5-5", "high", "M", "allow"),
    ("hermes", "claude-sonnet-5-5", "high", "L", "allow"),
    ("hermes", "claude-sonnet-5-5", "medium", "M", "deny"),
    ("claude", "claude-sonnet-5-5", "high", "L", "allow"),
    ("claude", "claude-sonnet-5-5", "medium", "L", "deny"),
    ("agy", "claude-sonnet-5-5", "high", "L", "allow"),
    ("agy", "claude-sonnet-5-5", "medium", "M", "deny"),
    ("claude", "claude-haiku-5-5", "high", "XS", "allow"),
    ("claude", "claude-haiku-5-5", "high", "M", "deny"),
    ("claude", "claude-haiku-5-5", "high", "L", "deny"),
    ("claude", "claude-haiku-5-5", "medium", "M", "deny"),
    ("claude", "claude-opus-5-5", "medium", "L", "deny"),
    ("claude", "claude-opus-5-5", "high", "L", "deny"),
    ("claude", "claude-opus-5-5", "high", "XL", "allow"),
])
def test_executor_role_state_by_size(harness, model, effort, size, expected):
    assert state(harness, model, effort, size) == expected


def test_luna_stays_a_reviewer_while_denied_as_an_m_l_executor():
    assert state("codex", "gpt-6-luna", "high", "M", role="code_reviewer") == "allow"
    assert state("codex", "gpt-6-luna", "high", "M", role="plan_reviewer") == "allow"
