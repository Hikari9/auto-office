"""Default routing holds each model family to its configured version floor."""
from office import candidates, config


def test_gemini_floor_is_3_7_by_default():
    floors = config.load_yaml(config.default_config_path())["model_family_floors"]
    assert floors == {"gemini": "3.7"}
    assert candidates.below_family_floor("gemini-3.6-flash", floors) == "3.7"
    assert candidates.below_family_floor("gemini-3.1-pro", floors) == "3.7"
    assert candidates.below_family_floor("gemini-3.7-flash", floors) is None
    assert candidates.below_family_floor("gemini-3.8-flash", floors) is None
    assert candidates.below_family_floor("gemini-3.10-flash", floors) is None
    assert candidates.below_family_floor("gpt-5.6-luna", floors) is None


def test_visual_reviewer_prefers_gemini_hydrogen_over_retired_flash():
    cfg = config.load_yaml(config.default_config_path())
    seed = cfg["roles"]["visual_reviewer"]["preferred_seed"]
    # Rico 2026-11-19 (AA v5 table): Hydrogen@low leads (conditional on the user's exact allow), Terra low is next,
    # Opus low last; Flash is retired.
    assert seed == [{"model_id": "gemini-4-hydrogen", "harness": "agy", "effort": "low", "conditional": True},
                    {"model_id": "gpt-6.1-terra", "harness": "codex", "effort": "low"},
                    {"model_id": "claude-opus-5-5", "harness": "claude", "effort": "low"}]
    assert not [e for e in seed if e["model_id"] == "gemini-3.8-flash"]
    rows = {(r["invocation_harness"], r["invocation_model_id"]): r for r in candidates.catalog_rows()}
    assert not [k for k in rows if (k[1] or "").startswith("gemini-3.1")]
