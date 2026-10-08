"""Historical benchmark catalog separation and evidence-backed invocation identities."""
from office import candidates, paths
import yaml


ARCHIVE = "legacy-nonrouting-2026-10-08.yaml"
INDEX = "Artificial Analysis Intelligence Index v4.3.2"
ARCHIVED_FAMILIES = {
    "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5",
    "claude-haiku-4-5-20251001", "gpt-6-sol", "gemini-3.6-flash",
    "gemini-3.7-flash",
}


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_legacy_benchmarks_are_preserved_only_in_the_nonrouting_archive():
    root = paths.resources_root()
    archive = _read(root / "catalog" / "archive" / ARCHIVE)
    seed = _read(root / "catalog" / "seed.yaml")
    archived = archive["models"]
    active = seed["models"]
    assert archive["benchmark_index_version"] == seed["benchmark_index_version"] == INDEX
    assert len(archived) == 26
    assert {r["model_id"] for r in archived} == ARCHIVED_FAMILIES
    assert all(r["dispatchable"] is False for r in archived)
    keys = lambda rows: {(r["model_id"], r["invocation_harness"], r["effort"]) for r in rows}
    assert not keys(archived) & keys(active)
    assert not keys(archived) & keys(candidates.catalog_rows())
    assert len(keys(active)) == len(active)


def test_trust_for_archived_models_is_kept_only_as_nonrouting_history():
    root = paths.resources_root()
    historical = _read(root / "catalog" / "archive" / ARCHIVE)
    current = _read(root / "catalog" / "trust-baseline.yaml")
    old = {r["triple"] for r in historical["historical_trust_grants"]}
    active = {r["triple"] for r in current["routes"]}
    assert old == {"claude@2/claude-sonnet-5@high",
                   "claude@2/claude-sonnet-5@medium"}
    assert not old & active


def test_sol_uses_official_invocation_id_without_enabling_unproved_efforts():
    seed = _read(paths.resources_root() / "catalog" / "seed.yaml")
    sol = [r for r in seed["models"] if r["model_id"] == "gpt-6.1-sol"]
    assert len(sol) == 5
    assert {r["effort"] for r in sol} == {"low", "medium", "high", "xhigh", "max"}
    assert all(r["invocation_model_id"] == "gpt-6.1-sol" for r in sol)
    assert all(r["dispatchable"] is False for r in sol)
    assert all(r["source_identifier"].startswith("gpt-6-1-sol") for r in sol)
    assert all(r["benchmark_indexes"][INDEX] > 0 for r in sol)
