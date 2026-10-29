"""#871: external policy data is resolved once and controls real routing."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import shutil

import pytest
import yaml

from office import candidates, executor_slates as slates, routing, shipped_catalog


@pytest.fixture
def policy_root(tmp_path):
    source = Path(slates.POLICY["_provenance"]["source"]).parent
    target = tmp_path / "catalog"
    target.mkdir()
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    return tmp_path


def _write_policy(root: Path, data: dict) -> dict:
    (root / "catalog" / "executor-policy.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return slates.load_policy(root)


def _candidate(model, effort, harness, score):
    return {"model_id": model, "invocation_model_id": model, "harness": harness,
            "harness_version": "1.0", "effort": effort, "capabilities": ["builder"],
            "invocation_source": "local-evidence: contract fixture",
            "benchmark_indexes": {slates.INDEX: score},
            "quota": {"status": "ok", "tightest_remaining_percent": 100},
            "route_status": "available", "price_fields": {"input_per_mtok": 1, "output_per_mtok": 2}}


def test_shipped_policy_is_resolved_against_catalog_facts():
    source = Path(slates.POLICY["_provenance"]["source"])
    assert slates.POLICY["_provenance"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    facts = {shipped_catalog.key(row): row for row in slates.POLICY["_catalog"]["models"]}
    for size, entries in slates.POLICY["executor_preferred"].items():
        for entry in entries:
            assert entry["_resolved_routes"]
            assert set(entry["_dispatchable_routes"]) <= set(entry["_resolved_routes"])
            assert all(facts[key].get("dispatchable") is not False for key in entry["_dispatchable_routes"])
        assert slates.ideal_seed(size) == [
            {key: value for key, value in row.items() if key in ("model_id", "effort", "harness") and value is not None}
            for row in entries
        ]
    for rule in slates.POLICY["floor_exceptions"]:
        assert rule["_resolved_routes"]
        assert set(rule["_dispatchable_routes"]) <= set(rule["_resolved_routes"])


def test_policy_rejects_nonexistent_but_retains_known_disabled_tuple_as_fact(policy_root):
    data = yaml.safe_load((policy_root / "catalog" / "executor-policy.yaml").read_text())
    data["executor_preferred"]["XS"][0].update(model_id="not-shipped")
    (policy_root / "catalog" / "executor-policy.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(slates.ExecutorPolicyError, match="nonexistent catalog tuple"):
        slates.load_policy(policy_root)
    data["executor_preferred"]["XS"][0].update(model_id="claude-haiku-5-5", effort="medium", harness="claude")
    policy = _write_policy(policy_root, data)
    route = next(row for row in policy["executor_preferred"]["XS"] if row["model_id"] == "claude-haiku-5-5")
    assert route["_resolved_routes"]
    assert not route["_dispatchable_routes"]


def test_temporary_policy_changes_actual_seed_and_runtime_selection(policy_root):
    source = yaml.safe_load((policy_root / "catalog" / "executor-policy.yaml").read_text())
    source["executor_preferred"]["XS"] = [{"model_id": "gemini-3.8-flash", "effort": "medium"}]
    updated = _write_policy(policy_root, source)
    seed = candidates._preferred_seed({}, {"gear": "XS"}, "XS", executor_policy=updated)
    assert seed == [{"model_id": "gemini-3.8-flash", "effort": "medium"}]
    pool = [_candidate("gpt-6.1-terra", "low", "codex", 55),
            _candidate("gemini-3.8-flash", "medium", "agy", 40)]

    def select(policy, preference):
        return routing.route({"role": "worker", "playbook": "Change", "preferred_seed": preference,
                              "slate_anchor": True, "allow_free_models": True,
                              "policy": {"required_capabilities": ["builder"], "floor": {"min_effort": "none",
                              "allowed_sources": ["local-evidence:"]}},
                              "context": {"gear": "XS", "size_class": "XS"},
                              "_executor_policy": policy, "candidates": pool})

    original = select(slates.POLICY, candidates._preferred_seed({}, {"gear": "XS"}, "XS"))
    changed = select(updated, seed)
    assert original["status"] == changed["status"] == "selected", (original, changed)
    assert (original["candidate"]["harness"], original["candidate"]["model_id"], original["candidate"]["effort"]) == (
        "codex", "gpt-6.1-terra", "low")
    assert (changed["candidate"]["harness"], changed["candidate"]["model_id"], changed["candidate"]["effort"]) == (
        "agy", "gemini-3.8-flash", "medium")


def test_temporary_floor_policy_changes_runtime_eligibility(policy_root):
    source = yaml.safe_load((policy_root / "catalog" / "executor-policy.yaml").read_text())
    source["floors"]["XS"] = 45
    updated = _write_policy(policy_root, source)
    out = routing.route({"role": "worker", "playbook": "Change", "preferred_seed": slates.ideal_seed("XS", updated),
                         "slate_anchor": True, "allow_free_models": True,
                         "policy": {"required_capabilities": ["builder"], "floor": {"min_effort": "none",
                         "allowed_sources": ["local-evidence:"]}}, "context": {"gear": "XS", "size_class": "XS"},
                         "_executor_policy": updated,
                         "candidates": [_candidate("claude-haiku-5-5", "medium", "claude", 40)]})
    assert out["status"] == "no_qualifying_candidate", out
    assert any(r.get("category") == "subtask-intelligence-floor" for r in out["rejected"])


@pytest.mark.parametrize("mutation", [
    lambda d: d["floors"].pop("XS"),
    lambda d: d["executor_preferred"]["XS"].append(copy.deepcopy(d["executor_preferred"]["XS"][0])),
    lambda d: d["executor_preferred"]["XS"][0].__setitem__("effort", "invalid"),
    lambda d: d["executor_preferred"]["XS"][0].__setitem__("effort", ["high"]),
    lambda d: d["planner_preferred"].__setitem__("XS", []),
    lambda d: d["model_restrictions"][0].__setitem__("reason", "reserved for {missing}"),
    lambda d: d["model_restrictions"][0].__setitem__("reason", "reserved for {"),
    lambda d: d["model_restrictions"][0].__setitem__("model_id", "claude-opus-55"),
    lambda d: d["floor_exceptions"][0].__setitem__("model_id", "not-shipped"),
    lambda d: d.__setitem__("schema_version", 999),
])
def test_malformed_policy_fails_closed(policy_root, mutation):
    data = yaml.safe_load((policy_root / "catalog" / "executor-policy.yaml").read_text())
    mutation(data)
    (policy_root / "catalog" / "executor-policy.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(slates.ExecutorPolicyError):
        slates.load_policy(policy_root)
