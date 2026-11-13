"""Owner-approved advisory XS-XL candidate seeds remain gated pending exact Hydrogen qualification."""
from pathlib import Path
from office import shipped_catalog, executor_slates

EXPECTED = {
    "XS": [("claude-haiku-5-5", "medium", "claude"), ("gpt-6.1-terra", "low", "codex"), ("gemini-4-hydrogen", "low", "agy")],
    "S": [("gpt-6.1-terra", "medium", "codex"), ("gemini-4-hydrogen", "medium", "agy"), ("claude-sonnet-5-5", "medium", "claude")],
    "M": [("claude-sonnet-5-5", "medium", "claude"), ("gemini-4-hydrogen", "high", "agy"), ("gpt-6.1-terra", "high", "codex")],
    "L": [("claude-sonnet-5-5", "high", "claude"), ("gemini-4-hydrogen", "high", "agy")],
    "XL": [("claude-opus-5-5", "medium", "claude"), ("gpt-6.1-sol", "medium", "codex"), ("gemini-4-argon", "high", "agy")],
}

def test_owner_preference_intent_and_non_dispatchable_hydrogen():
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
    assert policy["benchmark_index"] == index
    for size, expected in EXPECTED.items():
        actual = [(r["model_id"], r["effort"], r.get("harness")) for r in policy["executor_preferred"][size]]
        assert actual == expected
        assert not any(item["model_id"] == "gemini-3.8-flash" for item in policy["executor_preferred"][size])
    assert policy["model_restrictions"][0]["allowed_sizes"] == ["XL"]
    assert routes["agy/gemini-3.8-flash@medium"]["dispatchable"] is True  # historical and visual review untouched

def test_packaged_hydrogen_catalog_parity():
    root = Path(__file__).resolve().parents[2]
    for name in ("routes.tsv", "benchmarks.tsv", "provenance.yaml", "lineages.yaml", "executor-policy.yaml"):
        assert (root/"catalog"/name).read_bytes() == (root/"plugin-dist"/"catalog"/name).read_bytes()
