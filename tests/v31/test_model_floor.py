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


def test_visual_reviewer_prefers_latest_gemini_flash():
    cfg = config.load_yaml(config.default_config_path())
    seed = cfg["roles"]["visual_reviewer"]["preferred_seed"]
    # Low is the default visual reviewer (user, 2026-09-28); medium is the fallback.
    assert seed[:2] == [{"model_id": "gemini-3.8-flash", "harness": "agy", "effort": "low"},
                        {"model_id": "gemini-3.8-flash", "harness": "agy", "effort": "medium"}]
    rows = {(r["invocation_harness"], r["invocation_model_id"]): r for r in candidates.catalog_rows()}
    for slug in ("gemini-3.8-flash-medium", "gemini-3.8-flash-low"):
        assert rows[("agy", slug)].get("dispatchable") is not False
    assert not [k for k in rows if (k[1] or "").startswith("gemini-3.1")]
