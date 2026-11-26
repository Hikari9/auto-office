"""S executor canary (#1360): Haiku 5.5 xhigh on claude and hermes at S only.
Literal expectations, not derived from the YAML."""
import pytest

from office import executor_slates as slates, route_policy, shipped_catalog

MAJOR = {"claude": "2.1.332", "hermes": None, "codex": "0.170.0", "agy": "1.5.18"}


def state(harness, model, effort, size):
    cand = {"harness": harness, "harness_version": MAJOR[harness], "model_id": model,
            "invocation_model_id": model, "effort": effort}
    return route_policy.effective_role_state(cand, "executor", [], size)["state"]


@pytest.mark.parametrize("harness, model, effort, size, expected", [
    ("claude", "claude-haiku-5-5", "xhigh", "S", "allow"),
    ("hermes", "claude-haiku-5-5", "xhigh", "S", "allow"),
    ("claude", "claude-haiku-5-5", "xhigh", "XS", "deny"),
    ("hermes", "claude-haiku-5-5", "xhigh", "XS", "deny"),
    ("claude", "claude-haiku-5-5", "xhigh", "M", "deny"),
    ("claude", "claude-haiku-5-5", "xhigh", "L", "deny"),
    ("hermes", "claude-haiku-5-5", "xhigh", "M", "deny"),
    ("hermes", "claude-haiku-5-5", "xhigh", "L", "deny"),
    ("claude", "claude-haiku-5-5", "xhigh", "XL", "deny"),
    ("claude", "claude-haiku-5-5", "max", "S", "deny"),
    ("hermes", "claude-haiku-5-5", "max", "S", "deny"),
    ("claude", "claude-haiku-5-5", "high", "XS", "allow"),
    ("claude", "claude-haiku-5-5", "high", "M", "deny"),
    ("claude", "claude-haiku-5-5", "high", "L", "deny"),
    # Sonnet medium stays allowed at S so it can be pinned when speed matters more than score.
    ("claude", "claude-sonnet-5-5", "medium", "S", "allow"),
])
def test_s_canary_executor_role_states(harness, model, effort, size, expected):
    assert state(harness, model, effort, size) == expected


def _haiku(effort, harness="claude", scores=None):
    return {"model_id": "claude-haiku-5-5", "harness": harness, "effort": effort,
            "invocation_model_id": "claude-haiku-5-5", "benchmark_indexes": scores or {}}


@pytest.mark.parametrize("harness", ["claude", "hermes"])
def test_haiku_xhigh_clears_the_s_floor_only_through_the_canary_exception(harness):
    cand = _haiku("xhigh", harness)
    assert slates.benchmark_gate(cand, "S", slates.POLICY) is not None, "no published score: the gate alone fails closed"
    waiver = slates.floor_exception(cand, "S", "executor", slates.POLICY)
    assert waiver is not None and waiver["exception"] == "haiku-xhigh-s-canary-1360"
    for size in ("XS", "M", "L", "XL"):
        assert slates.floor_exception(cand, size, "executor", slates.POLICY) is None, size
    assert slates.floor_exception(cand, "S", "code_reviewer", slates.POLICY) is None


def test_canary_exception_does_not_cover_other_haiku_efforts_or_a_published_score():
    assert slates.floor_exception(_haiku("high"), "S", "executor", slates.POLICY) is None
    scored = _haiku("xhigh", scores={slates.INDEX: 30})
    assert slates.floor_exception(scored, "S", "executor", slates.POLICY) is None


def test_argon_stays_the_first_missing_score_exception():
    assert slates.FLOOR_EXCEPTION == {"model_id": "gemini-4-argon", "effort": "medium", "role": "executor", "size": "L"}
    names = [x["name"] for x in slates.POLICY["floor_exceptions"]]
    assert names.index("argon-medium-l-intelligence-floor") < names.index("haiku-xhigh-s-canary-1360")


def test_haiku_xhigh_catalog_rows():
    routes = {shipped_catalog.key(row): row for row in shipped_catalog.load()["models"]}
    assert routes["claude/claude-haiku-5-5@xhigh"]["dispatchable"] is True
    hermes = routes["hermes/claude-haiku-5-5@xhigh"]
    assert hermes["model_id"] == "claude-haiku-5-5" and hermes["effort"] == "xhigh"
    assert hermes["dispatchable"] == routes["hermes/claude-haiku-5-5@high"]["dispatchable"]
