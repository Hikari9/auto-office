"""Gated, version-isolated Claude Fable 5.2 registration."""
from pathlib import Path

from office import candidates, paths, shipped_catalog


PINNED = "Artificial Analysis Intelligence Index v4.3.2"
V5 = "Artificial Analysis Intelligence Index v5.0.1"
MODEL = "claude-fable-5-2"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def test_fable_52_is_registered_but_not_routable_or_scored_in_v4():
    shipped = shipped_catalog.load()
    assert shipped["benchmark_index_version"] == PINNED
    rows = [r for r in shipped["models"] if r["model_id"] == MODEL]
    assert len(rows) == 5
    assert {r["effort"] for r in rows} == EFFORTS
    for row in rows:
        assert row["invocation_harness"] == "claude"
        assert row["invocation_model_id"] == MODEL
        assert row["dispatchable"] is False
        assert row["discovery"] == "eligible"
        assert not row.get("alias_family")
        assert row["benchmark_indexes"] == {PINNED: None}
        assert row["price_fields"]["input_per_mtok"] == 8.0
        assert row["price_fields"]["output_per_mtok"] == 40.0
        source = row["source_snapshot_metadata"]
        assert source["benchmark_reference_index"] == V5
        assert source["url"].startswith("https://platform.claude.com/")

    effective = [r for r in candidates.catalog_rows() if r["model_id"] == MODEL]
    assert len(effective) == 5
    assert {r["lineage_key"] for r in effective} == {"anthropic/claude-fable"}
    assert {r["generation_version"] for r in effective} == {"5.2"}
    assert all(r["dispatchable"] is False for r in effective)


def test_fable_catalog_source_and_plugin_mirror_do_not_drift():
    root = Path(__file__).resolve().parents[2]
    for name in ("routes.tsv", "benchmarks.tsv", "provenance.yaml", "lineages.yaml"):
        assert (root / "catalog" / name).read_bytes() == (
            root / "plugin-dist" / "catalog" / name
        ).read_bytes()
