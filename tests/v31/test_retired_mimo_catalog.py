"""Pi/Kilo MiMo 2.6 retirement: no shipped routes, preserved evidence."""
from pathlib import Path

import pytest
import yaml

from office import adapters, candidates, configcmd, model_lineages

ROOT = Path(__file__).resolve().parents[2]
RETIRED = {"mimo-v2.6-pro", "mimo-v2.6-flash"}
HARNESSES = {"pi", "kilo"}


@pytest.fixture(autouse=True)
def clean_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CATALOG", str(tmp_path / "empty-overlay.yaml"))


def test_retired_mimo_not_shipped_or_offered(monkeypatch):
    from office import shipped_catalog
    seed = shipped_catalog.load(ROOT)
    retired = lambda row: (row.get("model_id") in RETIRED
                           and row.get("invocation_harness") in HARNESSES)
    assert not [r for r in seed["models"] if retired(r)]
    assert not [r for r in candidates.catalog_rows() if retired(r)]
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "1.1.0")
    routed, _ = candidates.build_candidates(None, "executor", probe=False)
    assert not [r for r in routed if retired(r)]
    known = configcmd.known_routes(None)
    assert not [r for r in known if any(f"{h}/{m}@" in r for h in HARNESSES for m in RETIRED)]


def test_retired_mimo_evidence_is_nonrouting_archive():
    archived = yaml.safe_load((ROOT / "catalog" / "archive" /
                    "retired-mimo-pi-kilo-2026-10-24.yaml").read_text(encoding="utf-8"))
    rows = archived["models"]
    assert {(r["invocation_harness"], r["model_id"], r["effort"]) for r in rows} == {
        (h, m, "medium") for h in HARNESSES for m in RETIRED}
    assert all(r["dispatchable"] is False for r in rows)
    # No benchmark/history loss: every archived row keeps its invocation slug and benchmark evidence.
    slugs = {(r["invocation_harness"], r["model_id"]): r["invocation_model_id"] for r in rows}
    assert slugs == {
        ("pi", "mimo-v2.6-pro"): "xiaomi-token-plan-sgp/mimo-v2.6-pro",
        ("pi", "mimo-v2.6-flash"): "xiaomi-token-plan-sgp/mimo-v2.6-flash",
        ("kilo", "mimo-v2.6-pro"): "kilo/xiaomi/mimo-v2.6-pro",
        ("kilo", "mimo-v2.6-flash"): "kilo/xiaomi/mimo-v2.6-flash"}
    for r in rows:
        assert r["invocation_source"].startswith("local-evidence:"), r["model_id"]
        assert r["benchmark_indexes"] and all(isinstance(v, (int, float)) for v in r["benchmark_indexes"].values())
        assert r["price_fields"] and r["speed_fields"] and r["source_snapshot_metadata"]["url"]
    assert all("owner-requested retirement" in r["supersession_metadata"]["reason"]
               for r in rows)


def test_pi_kilo_remain_and_unrelated_models_survive():
    assert HARNESSES <= adapters.load_all().keys()
    rows = candidates.catalog_rows()
    assert any(r["invocation_harness"] == "kilo" and
               r["model_id"] == "step-5-preview-free" for r in rows)
    assert any(r["invocation_harness"] == "pi" and
               r["model_id"] == "gpt-5-codex" and r["dispatchable"] is False for r in rows)
    assert RETIRED.isdisjoint(model_lineages.load())
