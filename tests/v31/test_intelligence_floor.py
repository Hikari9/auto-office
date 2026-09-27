"""Role floors gate on intelligence, aliases carry scores, and seeds can vary by size (#183)."""
from office import candidates, config, paths, scoring

INDEX = "Artificial Analysis Intelligence Index v4.3.2"


def _seed_rows():
    return {m["model_id"]: m for m in config.load_yaml(paths.resources_root() / "catalog" / "seed.yaml")["models"]
            if m["model_id"] in {"sonnet", "opus", "astra", "luna"}}


def test_aliases_invoke_latest_and_carry_scores():
    rows = _seed_rows()
    assert rows["opus"]["invocation_model_id"] == "claude-opus-5-5"
    assert rows["luna"]["invocation_model_id"] == "gpt-6-luna"
    for row in rows.values():
        assert INDEX in row["benchmark_indexes"], row["model_id"]


def test_executor_and_reviewer_floors_use_intelligence():
    roles = config.load_yaml(config.default_config_path())["roles"]
    for role in ("executor", "plan_reviewer", "code_reviewer"):
        floor = roles[role]["floor"]
        assert floor["min_effort"] == "none"
        assert floor["min_benchmark_index"] == {"index_name": INDEX, "min_score": 31}


def test_low_effort_high_intelligence_passes_executor_floor():
    floor = config.load_yaml(config.default_config_path())["roles"]["executor"]["floor"]
    ok, _ = scoring.evaluate_capability_floor(
        {"effort": "low", "benchmark_indexes": {INDEX: 42}, "invocation_source": "documented: x"}, floor)
    assert ok
    ok, reason = scoring.evaluate_capability_floor(
        {"effort": "xhigh", "benchmark_indexes": {INDEX: 30}, "invocation_source": "documented: x"}, floor)
    assert not ok


def test_seed_by_size_overrides_default_seed():
    xl = [{"model_id": "claude-opus-5-5", "effort": "low"}]
    base = [{"model_id": "gemini-3.8-flash", "effort": "medium"}]
    policy = {"preferred_seed": base, "preferred_seed_by_size": {"XL": xl}}
    assert candidates._preferred_seed(policy, {"risk": {"size_class": "XL"}}) == xl
    assert candidates._preferred_seed(policy, {"risk": {"size_class": "L"}}) == base
    assert candidates._preferred_seed(policy, {"risk": {}}) == base
    assert candidates._preferred_seed({"preferred_seed": base}, {"risk": {"size_class": "XL"}}) == base
