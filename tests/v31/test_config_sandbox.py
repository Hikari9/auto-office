"""The suite must not inherit route policy from the invoking user's config."""
from __future__ import annotations

from pathlib import Path

from office import config, route_policy


def test_user_config_is_sandboxed_and_test_override_still_applies(tmp_path, monkeypatch):
    sandbox = config.config_paths(None)["user"]
    assert sandbox.is_file() and sandbox.read_text(encoding="utf-8") == ""
    effective, _ = config.resolve(None)
    assert route_policy.user_policy(effective)["denied"] == []

    override = tmp_path / "user-config.yaml"
    override.write_text(
        "routing:\n  user_policy:\n    denied_models: [harness:pi]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(override))
    effective, _ = config.resolve(None)
    assert route_policy.user_policy(effective)["denied"] == ["harness:pi"]
