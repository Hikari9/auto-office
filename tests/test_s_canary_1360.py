"""S executor canary (#1360): Haiku 5.5 xhigh on claude and hermes at S only.
Literal expectations, not derived from the YAML."""
from pathlib import Path

import pytest

from office import executor_slates as slates, route_policy, routing, shipped_catalog

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
    ("hermes", "claude-haiku-5-5", "xhigh", "XL", "deny"),
    ("claude", "claude-haiku-5-5", "max", "S", "deny"),
    ("hermes", "claude-haiku-5-5", "max", "S", "deny"),
    ("claude", "claude-haiku-5-5", "high", "XS", "allow"),
    ("claude", "claude-haiku-5-5", "high", "M", "deny"),
    ("claude", "claude-haiku-5-5", "high", "L", "deny"),
    # Sonnet medium stays allowed at S so it can be pinned when speed matters more than score.
    ("claude", "claude-sonnet-5-5", "medium", "S", "allow"),
    # An unsized task keeps the xhigh deny.
    ("claude", "claude-haiku-5-5", "xhigh", None, "deny"),
    ("hermes", "claude-haiku-5-5", "xhigh", None, "deny"),
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
    # Like every hermes Haiku row, it stays inert until a hermes conformance probe lands; the claude peer dispatches.
    assert hermes["dispatchable"] is False
    assert routes["hermes/claude-haiku-5-5@high"]["dispatchable"] is False


def _pool_candidate(model, effort, harness, score):
    return {"model_id": model, "harness": harness, "effort": effort, "invocation_model_id": model,
            "harness_version": {"claude": "2.1.332", "codex": "0.170.0", "agy": "1.5.18"}.get(harness),
            "capabilities": ["builder"], "invocation_source": "local-evidence: contract fixture",
            "benchmark_indexes": {} if score is None else {slates.INDEX: score},
            "quota": {"status": "ok", "tightest_remaining_percent": 100}, "route_status": "available",
            "price_fields": {"input_per_mtok": 1, "output_per_mtok": 2}}


def _route(size, seed=None):
    seed = slates.ideal_seed(size, slates.POLICY) if seed is None else seed
    pool = [_pool_candidate("claude-haiku-5-5", "xhigh", "claude", None),
            _pool_candidate("gpt-6.1-terra", "medium", "codex", 44),
            _pool_candidate("gpt-6.1-terra", "high", "codex", 50)]
    return routing.route({
        "role": "executor", "playbook": "Change", "preferred_seed": seed, "slate_anchor": True,
        "allow_free_models": True,
        "policy": {"required_capabilities": ["builder"], "floor": {"min_effort": "none", "allowed_sources": ["local-evidence:"]}},
        "context": {"gear": size, "size_class": size}, "candidates": pool})


def test_s_routing_selects_unscored_haiku_xhigh_first_through_the_canary_exception():
    decision = _route("S")
    assert decision["status"] == "selected", decision
    assert routing.candidate_id(decision["candidate"]) == "claude@2/claude-haiku-5-5@xhigh"


def test_m_routing_never_selects_haiku_xhigh():
    decision = _route("M")
    assert decision["status"] == "selected", decision
    assert "claude-haiku-5-5" not in routing.candidate_id(decision["candidate"])


def test_s_routing_records_the_executor_canary():
    decision = _route("S")
    assert decision["executor_canary"] == {"name": "haiku-xhigh-s-1360", "route": "claude@2/claude-haiku-5-5@xhigh", "size": "S"}
    assert decision["routing"]["slate"][0]["reason"] == "executor canary haiku-xhigh-s-1360"


def test_a_seed_not_headed_by_the_canary_row_is_ordinary_scoring():
    """A frontend-biased task, or a run pinned to the pre-canary policy, keeps ordinary scoring."""
    biased = slates.frontend_bias(slates.ideal_seed("S", slates.POLICY), {"domain": "ui"}, [])
    pre_canary = [{"model_id": "claude-sonnet-5-5", "effort": "medium", "harness": "claude", "rung": 1},
                  {"model_id": "gpt-6.1-terra", "effort": "medium", "harness": "codex", "rung": 2}]
    for seed in (biased, pre_canary):
        decision = _route("S", seed)
        assert "executor_canary" not in decision
        assert routing.candidate_id(decision["candidate"]) == "codex@0/gpt-6.1-terra@medium"


@pytest.mark.parametrize("mutate", [
    lambda d: d["executor_preferred"]["S"][3].update(canary="x"),  # the conditional Hydrogen row
    lambda d: d["executor_preferred"]["S"][0].update(canary=""),
    lambda d: d["executor_preferred"]["S"][0].update(canary=True),
    lambda d: d["planner_preferred"]["M"][0].update(canary="x"),
])
def test_canary_key_is_a_nonempty_name_on_unconditional_executor_rows_only(tmp_path, mutate):
    import shutil
    import yaml
    from office.executor_slates import ExecutorPolicyError
    shutil.copytree(Path(slates.__file__).resolve().parents[2] / "catalog", tmp_path / "catalog")
    policy = tmp_path / "catalog" / "executor-policy.yaml"
    assert slates.load_policy(tmp_path)["executor_preferred"]["S"][0]["canary"] == "haiku-xhigh-s-1360"
    data = yaml.safe_load(policy.read_text())
    mutate(data)
    policy.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ExecutorPolicyError):
        slates.load_policy(tmp_path)
