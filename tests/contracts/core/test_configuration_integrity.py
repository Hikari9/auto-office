"""Configuration is validated as data, without freezing today's policy values."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from office import config, executor_slates, route_policy, shipped_catalog
from test_helpers import policy_root


def test_executor_policy_provenance_matches_the_source_bytes():
    policy = executor_slates.POLICY
    source = Path(policy["_provenance"]["source"])
    assert policy["_provenance"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_policy_preferences_resolve_only_to_catalog_facts():
    facts = {shipped_catalog.key(row): row for row in shipped_catalog.load()["models"]}
    for entries in executor_slates.POLICY["executor_preferred"].values():
        for entry in entries:
            assert entry["_resolved_routes"] or entry.get("conditional") is True
            assert set(entry["_dispatchable_routes"]) <= set(entry["_resolved_routes"])
            assert all(facts[key].get("dispatchable") is not False for key in entry["_dispatchable_routes"])


def test_unknown_policy_schema_fails_closed(policy_root):
    policy_path = policy_root / "catalog" / Path(executor_slates.POLICY["_provenance"]["source"]).name
    data = yaml.safe_load(policy_path.read_text())
    data["schema_version"] = 999
    policy_path.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(executor_slates.ExecutorPolicyError):
        executor_slates.load_policy(policy_root)


def test_duplicate_yaml_policy_keys_are_rejected(policy_root):
    path = policy_root / "catalog" / Path(executor_slates.POLICY["_provenance"]["source"]).name
    text = path.read_text()
    path.write_text(text.replace("floors: {XS:", "floors: {XS: 1, XS:", 1))
    with pytest.raises(executor_slates.ExecutorPolicyError, match="duplicate key"):
        executor_slates.load_policy(policy_root)


@pytest.mark.parametrize("bad", [
    {"role": "executor", "route": "fake/fixture-a@low", "state": "sometimes"},
    {"role": "unknown-role", "route": "fake/fixture-a@low", "state": "deny"},
    {"role": "executor", "route": None, "state": "deny"},
    {"role": "executor", "route": "fake/fixture-a@low", "state": "deny", "wildcard": "yes"},
])
def test_malformed_user_role_rules_are_rejected(bad):
    assert route_policy.validate_user_policy({"routing": {"user_policy": {"role_rules": [bad]}}})


def test_repository_tier_cannot_claim_user_authority():
    with pytest.raises(ValueError, match="repository routing.user_policy.role_rules"):
        config.resolve(None, files={"user": None, "repo":
            "routing:\n  user_policy:\n    role_rules:\n      - {role: executor, route: fake/fixture-a@low, state: allow}\n"})


@pytest.mark.parametrize("bad", [
    {"routing": {"discovery": {"enabled": "yes"}}},
    {"routing": {"discovery": {"max_trials_per_run": -1}}},
    {"routing": {"discovery": {"roles": ["executor", "root"]}}},
])
def test_invalid_discovery_configuration_is_reported(bad):
    assert route_policy.validate_discovery(bad)
