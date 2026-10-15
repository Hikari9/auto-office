"""Issue #610: deterministic named lineages and user-owned ceilings.

All tests use seed metadata and synthetic models; none invoke a real harness.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from office import candidates, config, harnesscmd, model_lineages as lineage, route_policy, user_catalog


def test_shipped_rows_have_explicit_release_identities_without_guessing():
    rows = candidates.catalog_rows()
    assert all(r.get("lineage_key") and r.get("generation_version")
               for r in rows if not r.get("alias_family"))
    assert lineage.identify("gpt-6.1-sol")["lineage_key"] == "openai/gpt-sol"
    assert lineage.identify("gpt-6.1-sol")["generation_version"] == "6.1"
    assert lineage.identify("potentially-new-model") is None
    assert candidates.model_family("gpt-6.1-sol") == "gpt"  # old review-independence contract stays


def test_numeric_ordering_not_string_sort_and_harness_effort_do_not_consume_slots():
    manifest = {
        model: {"lineage_key": "openai/gpt-luna", "generation_version": version,
                "release_status": "released", "provenance": "fixture"}
        for model, version in (("luna-old", "5.6"), ("luna-a", "6"), ("luna-new", "6.1"))
    }
    manifest["luna-preview"] = {"lineage_key": "openai/gpt-luna", "generation_version": "7",
                                "release_status": "preview", "provenance": "fixture"}
    rows = [
        {"model_id": model, "invocation_harness": harness, "effort": effort}
        for model in manifest for harness in ("codex", "agy") for effort in ("low", "high")
    ]
    rows += [{"model_id": "luna-unknown", "invocation_harness": "codex", "effort": "high"}]
    p = lineage.partition(rows, manifest=manifest)
    assert {r["model_id"] for r in p["active"]} == {"luna-a", "luna-new"}
    assert len(p["active"]) == 8  # two generations * 2 harnesses * 2 efforts
    assert {r["model_id"] for r in p["archived"]} == {"luna-old"}
    assert {r["model_id"] for r in p["pending"]} == {"luna-unknown", "luna-preview"}
    assert p["pending"][0]["catalog_status_reason"] == "generation not released"


def test_alias_in_concrete_partition_does_not_take_slot():
    manifest = lineage.load()
    rows = candidates.catalog_rows()
    part = lineage.partition(rows)
    assert {r["model_id"] for r in part["active"]} >= {"gpt-5.6-sol", "gpt-6.1-sol"}
    assert "sol" not in {r["model_id"] for r in part["active"]}
    assert part["aliases"]
    assert not part["archived"]  # shipped source has at most two released versions per lineage
    assert all(r["lineage_key"] == "openai/gpt-sol" for r in part["active"]
               if r["model_id"] in ("gpt-5.6-sol", "gpt-6.1-sol"))
    assert manifest["gpt-6.1-sol"]["provenance"] == "shipped-catalog"


@pytest.mark.parametrize("version, expected", [("5.6", (5, 6)), ("5-5", (5, 5)), ("6", (6,)),
                                             ("6.0", (6,)), ("6.1", (6, 1))])
def test_numeric_normalization(version, expected):
    assert lineage.version_parts(version) == expected


@pytest.mark.parametrize("version", ["5.6-preview", "6..1", "six", "6.1.?", " 6.1", ""])
def test_unverifiable_versions_are_rejected(version):
    with pytest.raises(lineage.LineageError):
        lineage.version_parts(version)


def test_global_user_ceiling_denies_older_generation_but_not_newer():
    policy = {"denied": [], "overkill": [], "ceilings": {"openai/gpt-sol": "5.6"},
              "ceiling_sources": {"openai/gpt-sol": "user"}, "sources": {}}
    sol_rows = [r for r in candidates.catalog_rows() if r["model_id"] in ("gpt-5.6-sol", "gpt-6.1-sol")]
    assert len(sol_rows) > 1
    for row in sol_rows:
        reason = route_policy.is_denied({"harness": row["invocation_harness"], **row}, policy)
        assert bool(reason) is (row["model_id"] == "gpt-5.6-sol")
        if reason:
            assert "by user" in reason and "version_ceilings" in reason
    alias = {"model_id": "sol", "alias_resolved_to": "gpt-6.1-sol"}
    assert route_policy.is_denied(alias, policy) is None
    alias["alias_resolved_to"] = "gpt-5.6-sol"
    assert route_policy.is_denied(alias, policy) is not None
    # Caller-supplied fields can never spoof the generation that policy checks.
    forged = {"model_id": "gpt-5.6-sol", "lineage_key": "openai/gpt-sol",
              "generation_version": "99"}
    assert route_policy.is_denied(forged, policy) is not None


def test_ceiling_policy_changes_are_global_user_owned_and_reversible(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(path))
    initial = route_policy.policy_digest(config.resolve(None)[0])
    result = harnesscmd.model_ceiling("openai/gpt-sol", through="5.6")
    assert result.data["changed"] and path.exists()
    raw = yaml.safe_load(path.read_text())
    assert raw["routing"]["user_policy"]["version_ceilings"] == {"openai/gpt-sol": "5.6"}
    effective = config.resolve(None)[0]
    assert route_policy.policy_digest(effective) != initial
    policy = route_policy.user_policy(effective)
    assert policy["ceiling_sources"]["openai/gpt-sol"] == "user"
    assert route_policy.is_denied({"model_id": "gpt-5.6-sol"}, policy)
    assert not route_policy.is_denied({"model_id": "gpt-6.1-sol"}, policy)
    assert not harnesscmd.model_ceiling("openai/gpt-sol", through="5.6").data["changed"]
    assert harnesscmd.model_ceiling("openai/gpt-sol", through=None).data["changed"]
    assert yaml.safe_load(path.read_text())["routing"]["user_policy"]["version_ceilings"] == {}
    assert route_policy.policy_digest(config.resolve(None)[0]) == initial


def test_ceiling_validation_rejects_unknown_versions_without_writing(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(path))
    with pytest.raises(Exception):
        harnesscmd.model_ceiling("openai/gpt-sol", through="6-preview")
    with pytest.raises(Exception):
        harnesscmd.model_ceiling("openai/gpt-unknown", through="6")
    assert not path.exists()
    with pytest.raises(ValueError, match="quoted numeric generation"):
        config.resolve(None, files={"user": yaml.safe_dump({"routing": {"user_policy": {
            "version_ceilings": {"openai/gpt-sol": 5.6}}}}), "repo": None})


def test_enable_removes_legacy_user_overlay_gate_without_toggling_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CATALOG", str(tmp_path / "catalog.yaml"))
    user_catalog.save({"models": [], "disabled": [{"harness": "codex", "model_id": "luna", "at": "fixture"}]})
    assert harnesscmd.model_enable("codex/luna").data["removed"] == 1
    assert not user_catalog.load()["disabled"]
    assert not harnesscmd.model_enable("codex/luna").data["changed"]
    assert any(r["model_id"] == "luna" and r.get("dispatchable") is not False
               for r in candidates.catalog_rows())


def test_cli_registry_and_policy_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "config.yaml"))
    rows = harnesscmd.model_registry().data
    assert rows["counts"]["active"] >= 25
    assert rows["counts"]["archived"] == 0
    assert {"lineage": "openai/gpt-luna", "version": "6"} in rows["generations"]
    assert "ceilings" in harnesscmd.model_policy().data["policy"]
