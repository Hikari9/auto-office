"""Hydrogen XS/S seed registration stays disabled and does not use AA v5 scores as v4."""
from pathlib import Path
from office import shipped_catalog, executor_slates

def test_hydrogen_medium_replaces_flash_in_xs_s_preferred_seeds():
    catalog = shipped_catalog.load()
    routes = {shipped_catalog.key(row): row for row in catalog["models"]}
    index = catalog["benchmark_index_version"]
    assert index == "Artificial Analysis Intelligence Index v4.3.2"
    for effort in ("low", "medium", "high", "xhigh"):
        row = routes[f"agy/gemini-4-hydrogen@{effort}"]
        assert row["dispatchable"] is False
        assert row["discovery"] == "eligible"
        assert (row.get("benchmark_indexes") or {}).get(index) is None
    policy = executor_slates.load_policy()
    for size in ("XS", "S"):
        seeds = policy["executor_preferred"][size]
        assert any(x["model_id"] == "gemini-4-hydrogen" and x["effort"] == "medium" and x.get("harness") == "agy" for x in seeds)
        assert not any(x["model_id"] == "gemini-3.8-flash" for x in seeds)
    assert routes["agy/gemini-3.8-flash@medium"]["dispatchable"] is True

def test_packaged_hydrogen_catalog_parity():
    root = Path(__file__).resolve().parents[2]
    for name in ("routes.tsv", "benchmarks.tsv", "provenance.yaml", "lineages.yaml", "executor-policy.yaml"):
        assert (root/"catalog"/name).read_bytes() == (root/"plugin-dist"/"catalog"/name).read_bytes()
