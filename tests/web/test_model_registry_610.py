"""The Model Registry Workstation is a read-only projection of real Office facts."""
from __future__ import annotations

from office import config
from office.web import model_registry


def test_registry_does_not_claim_quota_or_trust_from_a_seed_score(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    out = model_registry.view()
    assert not out["quota_probed"] and out["telemetry_opt_in"] is False
    assert out["counts"]["historical"] >= 26
    assert any(r.get("archived_source") == "legacy-nonrouting-2026-10-08.yaml"
               for r in out["archive"])
    assert any(g["model_id"] == "gpt-6.1-sol" for g in out["generations"])
    for row in out["generations"]:
        assert row["quota"] == {"status": "unknown", "value": None}
        assert row["efforts"]
        assert all(e["technical_state"] in (
            "not-installed", "available", "discovered-unconfirmed", "probe-pending",
            "probe-passed", "temporarily-unavailable", "confirmed-unsupported")
            for e in row["efforts"])
        assert all(e["benchmark_index"] == out["index"] for e in row["efforts"])


def test_registry_global_version_ceiling_discloses_independent_policy(tmp_path, monkeypatch):
    target = tmp_path / "user-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(target))
    target.write_text("routing:\n  user_policy:\n    version_ceilings:\n      openai/gpt-sol: '5.6'\n")
    out = model_registry.view()
    sol = {g["version"]: g for g in out["generations"] if g["lineage"] == "openai/gpt-sol"}
    assert sol["5.6"]["user_denied"] and not sol["6.1"]["user_denied"]
    assert "version_ceilings" in sol["5.6"]["user_denial_reason"]
    assert sol["5.6"]["ceiling_source"] == "user"
    assert out["policy"]["version_ceilings"] == {"openai/gpt-sol": "5.6"}


def test_registry_shipped_efforts_are_not_compressed_into_a_fabricated_single_score(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    out = model_registry.view()
    sol = next(g for g in out["generations"] if g["model_id"] == "gpt-6.1-sol")
    assert {e["effort"] for e in sol["efforts"]} == {"low", "medium", "high", "xhigh", "max"}
    assert all(e["source_metadata"] is not None for e in sol["efforts"])
    assert len({e["source_identifier"] for e in sol["efforts"]}) == 5


def test_registry_never_misstates_legacy_user_overlay_as_a_technical_disable(tmp_path, monkeypatch):
    from office import user_catalog
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    monkeypatch.setenv("OFFICE_USER_CATALOG", str(tmp_path / "legacy.yaml"))
    user_catalog.save({"models": [], "disabled": [{"harness": "codex", "model_id": "gpt-6.1-sol"}]})
    out = model_registry.view()
    g = next(g for g in out["generations"] if g["model_id"] == "gpt-6.1-sol" and g["harness"] == "codex")
    assert g["legacy_overlay_denied"] and g["user_denied"]
    assert "legacy" in g["user_denial_reason"]
    assert "legacy" in g["user_policy_source"]


def test_registry_telemetry_flag_reflects_actual_opt_in(tmp_path, monkeypatch):
    p = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(p))
    p.write_text("economics:\n  collect: true\n")
    assert model_registry.view()["telemetry_opt_in"] is True
