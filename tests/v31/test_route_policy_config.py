"""Config tier provenance, pinned historical behavior and policy editing."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess

import pytest
import yaml

from office import config, configcmd, db, route_policy as policy, state
from office.state import OfficeError


def resolve(user=None, repo=None, sets=None):
    return config.resolve(None, sets=sets, files={"user": yaml.safe_dump(user) if user is not None else None,
                                               "repo": yaml.safe_dump(repo) if repo is not None else None})[0]


def layer(value):
    return {"routing": {"adaptive": {"budget_ceiling_usd": value}}}


def test_new_defaults_have_no_hard_ceiling_or_user_policy():
    cfg = resolve()
    assert policy.budget_ceiling(cfg) == {"usd": None, "source": "shipped"}
    assert policy.user_policy(cfg) == {"denied": [], "overkill": [],
                                       "sources": {"denied_models": "shipped", "overkill_rules": "shipped"}}
    settings = policy.discovery_settings(cfg)
    # shipped config turns discovery on (#494 T6); the code constant stays off, the safe default for a pre-#494 pin
    assert settings == {**policy.DISCOVERY_DEFAULTS, "enabled": True}
    assert policy.DISCOVERY_DEFAULTS["enabled"] is False
    assert cfg["routing"]["adaptive"]["cost_scale_usd"] == 25
    assert cfg[policy.DIGEST_KEY] == policy.policy_digest(cfg)


@pytest.mark.parametrize("user,repo,sets,source,value", [
    (None, None, None, "shipped", None),
    (layer(30), None, None, "user", 30),
    (layer(30), layer(40), None, "repo", 40),
    (layer(30), layer(40), ["routing.adaptive.budget_ceiling_usd=50"], "run", 50),
    (layer(30), layer(None), None, "repo", None),
    (layer(30), layer(40), ["routing.adaptive.budget_ceiling_usd=null"], "run", None),
])
def test_ceiling_precedence_and_provenance(user, repo, sets, source, value):
    cfg = resolve(user, repo, sets)
    assert policy.budget_ceiling(cfg) == {"usd": value, "source": source}
    assert cfg[policy.PROVENANCE_KEY][policy.CEILING_KEY] == source


def test_shipped_numeric_ceiling_is_never_a_hard_gate_for_new_policy():
    cfg = resolve()
    cfg["routing"]["adaptive"]["budget_ceiling_usd"] = 25
    assert policy.budget_ceiling(cfg) == {"usd": None, "source": "shipped"}


def test_historical_pin_keeps_its_ceiling_and_discovery_off():
    old = layer(25)
    old["routing"]["discovery"] = {"enabled": True}
    pinned = state.pinned_config({"policy": json.loads(json.dumps(old))})
    assert policy.budget_ceiling(pinned) == {"usd": 25, "source": "run"}
    assert policy.discovery_settings(pinned)["enabled"] is False
    assert pinned == old  # pure helpers never rewrite the historical snapshot


def test_each_policy_leaf_records_the_winning_tier_even_when_value_equals_default():
    cfg = resolve({"routing": {"discovery": {"enabled": True, "max_probes_per_run": 2},
                              "user_policy": {"denied_models": ["harness:codex"]}}},
                  {"routing": {"discovery": {"max_trials_per_run": 1},
                               "user_policy": {"overkill_rules": [{"route": "new-model", "roles": ["worker"]}]}}},
                  ["routing.discovery.enabled=false"])
    provenance = cfg[policy.PROVENANCE_KEY]
    assert provenance["routing.discovery.enabled"] == "run"
    assert provenance["routing.discovery.max_probes_per_run"] == "user"
    assert provenance["routing.discovery.max_trials_per_run"] == "repo"
    assert provenance["routing.discovery.roles"] == "shipped"
    user = policy.user_policy(cfg)
    assert user["denied"] == ["harness:codex"]
    assert user["sources"] == {"denied_models": "user", "overkill_rules": "repo"}
    assert user["overkill"][0]["roles"] == ["worker"]


def test_ignored_type_mismatch_does_not_claim_provenance():
    cfg, warnings = config.resolve(None, files={"user": "routing:\n  discovery:\n    enabled: wrong\n", "repo": None})
    assert cfg[policy.PROVENANCE_KEY]["routing.discovery.enabled"] == "shipped"
    assert any(w["reason"] == "type-mismatch-ignored" for w in warnings)


def test_files_and_prompt_cannot_forge_provenance_or_digest():
    cfg, warnings = config.resolve(None, sets=["_provenance.routing.adaptive.budget_ceiling_usd=shipped"],
                                   files={"user": yaml.safe_dump({**layer(30), "_route_policy_digest": "forged",
                                                                 "_provenance": {policy.CEILING_KEY: "shipped"}})})
    assert policy.budget_ceiling(cfg) == {"usd": 30, "source": "user"}
    assert cfg[policy.DIGEST_KEY] == policy.policy_digest(cfg)
    assert len([w for w in warnings if w["reason"] == "not-configurable-ignored"]) == 3


def test_policy_digest_covers_settings_policy_and_ceiling_source_only():
    cfg = resolve()
    digest = policy.policy_digest(cfg)
    assert digest.startswith("sha256:") and len(digest) == 71
    same = deepcopy(cfg)
    same["routing"]["adaptive"]["cost_scale_usd"] = 99
    same["irrelevant"] = "changed"
    assert policy.policy_digest(same) == digest
    for edited in (resolve(sets=["routing.discovery.enabled=false"]),
                   resolve(sets=["routing.user_policy.denied_models=[new-model]"]),
                   resolve(layer(30)), resolve(sets=["routing.adaptive.budget_ceiling_usd=null"])):
        assert policy.policy_digest(edited) != digest


def test_helpers_return_independent_copies():
    cfg = resolve({"routing": {"user_policy": {"overkill_rules": [{"route": "new-model", "roles": ["worker"]}]}}})
    before = deepcopy(cfg)
    policy.discovery_settings(cfg)["roles"].append("planner")
    policy.user_policy(cfg)["overkill"][0]["roles"].append("planner")
    assert cfg == before and policy.DISCOVERY_DEFAULTS["roles"] == ["executor", "worker"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return user, repo / ".auto-office/config.yaml"


def command(**kw):
    return configcmd.config(**{"key": None, "value": None, "tier": None, **kw})


@pytest.mark.parametrize("tier", ["user", "repo"])
def test_config_command_edits_discovery_and_user_policy_at_existing_tiers(home, tier):
    command(key="routing.discovery.enabled", value="true", tier=tier)
    command(key="routing.user_policy.denied_models", value="[codex/new-model@high, harness:agy]", tier=tier)
    command(key="routing.user_policy.overkill_rules", value="[{route: new-model, roles: [worker], size_classes: [S]}]", tier=tier)
    assert command(key="routing.discovery.enabled", origin=True).lines == [f"{tier}\ttrue"]
    target = home[0 if tier == "user" else 1]
    saved = yaml.safe_load(target.read_text())
    assert set(saved) == {"routing"} and policy.PROVENANCE_KEY not in saved
    effective = config.resolve(target.parent if tier == "user" else home[1].parent.parent)[0]
    assert policy.discovery_settings(effective)["enabled"] is True
    assert policy.is_denied({"harness": "agy", "model_id": "model"}, policy.user_policy(effective))


@pytest.mark.parametrize("key,value", [
    ("roles", "[planner]"), ("roles", "[executor, code_reviewer]"),
    ("max_probes_per_run", "-1"), ("max_probes_per_run", "21"), ("max_probes_per_run", "true"),
    ("max_trials_per_run", "21"), ("max_trials_per_run", "1.5"),
    ("max_trial_percent_rolling_20", "101"), ("max_trial_percent_rolling_20", ".nan"),
    ("probe_timeout_s", "0"), ("probe_timeout_s", "901"), ("probe_ttl_days", "366"),
    ("trial_size_classes", "[L]"), ("trial_blast_radius", "[production]"),
    ("enabled", "null"), ("require_known_fallback", "null"),
])
def test_discovery_invalid_values_rejected_without_writing(home, key, value):
    with pytest.raises(OfficeError) as exc:
        command(key=f"routing.discovery.{key}", value=value)
    assert "routing.discovery" in exc.value.message
    assert not home[0].exists()


@pytest.mark.parametrize("key,value", [
    ("denied_models", "[null]"), ("denied_models", "[codex/model@high/extra]"),
    ("overkill_rules", "[{}]"), ("overkill_rules", "[{route: model, roles: worker}]"),
    ("overkill_rules", "[{route: model, size_classes: [null]}]"),
])
def test_invalid_user_policy_rejected(home, key, value):
    with pytest.raises(OfficeError):
        command(key=f"routing.user_policy.{key}", value=value)
    assert not home[0].exists()


@pytest.mark.parametrize("key", [policy.PROVENANCE_KEY, policy.DIGEST_KEY])
def test_config_command_protects_metadata_even_with_force(home, key):
    with pytest.raises(OfficeError, match="not configurable"):
        command(key=key, value="forged", force=True)
    assert not home[0].exists()


def test_explicit_repin_updates_source_when_values_are_unchanged(home, monkeypatch):
    command(key="routing.adaptive.budget_ceiling_usd", value="30", tier="user")
    repo = home[1].parent.parent
    pinned = config.resolve(repo)[0]
    command(key="routing.adaptive.budget_ceiling_usd", value="30", tier="repo")
    run = {"id": "run", "repo_root": str(repo), "policy": pinned, "phase": "executing"}
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    monkeypatch.delenv("OFFICE_ROLE", raising=False)
    monkeypatch.setattr(state, "find_run", lambda *a: run)
    recorded = {}
    monkeypatch.setattr(state, "update_run", lambda con, run_id, **kw: recorded.update(kw))
    monkeypatch.setattr(state, "emit", lambda *a, **kw: None)
    con = db.connect(home[0].parent / "runs.db")
    result = configcmd.apply_run_routing(con, "run", "use the current config")
    assert result.data["changed"] == ["routing"]
    assert policy.budget_ceiling(recorded["policy"]) == {"usd": 30, "source": "repo"}
    assert recorded["policy"][policy.DIGEST_KEY] == policy.policy_digest(recorded["policy"])
    assert policy.budget_ceiling(pinned) == {"usd": 30, "source": "user"}
    con.close()


@pytest.mark.parametrize("raw", ["true", ".nan", ".inf", "-.inf"])
def test_invalid_ceiling_cannot_become_a_hard_user_budget(home, raw):
    with pytest.raises(OfficeError, match="routing.adaptive.budget_ceiling_usd"):
        command(key=policy.CEILING_KEY, value=raw)
    assert not home[0].exists()


def test_huge_rolling_cap_is_a_validation_error():
    with pytest.raises(ValueError, match="max_trial_percent_rolling_20"):
        resolve({"routing": {"discovery": {"max_trial_percent_rolling_20": 10 ** 400}}})
