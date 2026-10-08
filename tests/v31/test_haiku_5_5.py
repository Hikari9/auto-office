"""Claude Haiku 5.5 benchmark rows preserve effort scores and two-tier pricing."""
from office import candidates, config, scoring

INDEX = "Artificial Analysis Intelligence Index v4.3.2"


def _rows(model_id, effort):
    return [
        r
        for r in candidates.catalog_rows()
        if r.get("model_id") == model_id and r.get("effort") == effort
    ]


def test_haiku_5_5_rows_capture_all_effort_scores_and_long_context_pricing():
    expected = {"low": 29, "medium": 34, "high": 38, "xhigh": 41, "max": 43}
    for effort, score in expected.items():
        [row] = _rows("claude-haiku-5-5", effort)
        assert row["benchmark_indexes"][INDEX] == score
        price = row["price_fields"]
        assert price["input_per_mtok"] == 0.1
        assert price["output_per_mtok"] == 0.5
        assert price["prompt_token_threshold"] == 100_000
        assert price["above_threshold_input_per_mtok"] == 0.5
        assert price["above_threshold_output_per_mtok"] == 2.5
        assert price["cache_read_per_mtok"] == 0.01
        assert price["above_threshold_cache_read_per_mtok"] == 0.05
        assert row["release_date"] == "2026-10-07"


def test_haiku_alias_tracks_5_5_and_medium_is_the_vendor_default_cold_start():
    [row] = _rows("haiku", "medium")
    assert row["alias_resolved_to"] == "claude-haiku-5-5"
    assert row["invocation_model_id"] == "claude-haiku-5-5"
    assert str(row["invocation_source"]).startswith("documented:")
    assert row["benchmark_indexes"][INDEX] == 34
    assert row["price_fields"]["prompt_token_threshold"] == 100_000


def test_haiku_executor_floor_starts_at_medium_not_low():
    floor = config.load_yaml(config.default_config_path())["roles"]["executor"]["floor"]

    [low] = _rows("haiku", "low")
    low_ok, _ = scoring.evaluate_capability_floor(low, floor)
    assert not low_ok

    for effort in ("medium", "high", "xhigh", "max"):
        [row] = _rows("haiku", effort)
        ok, reason = scoring.evaluate_capability_floor(row, floor)
        assert ok, reason


def test_benchmark_rows_do_not_claim_local_dispatch_conformance():
    for effort in ("low", "medium", "high", "xhigh", "max"):
        [row] = _rows("claude-haiku-5-5", effort)
        assert row["dispatchable"] is False
        assert str(row["invocation_source"]).startswith("documented:")
