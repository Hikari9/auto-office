"""#809 Gemini 4 Argon catalog registration and qualification gates."""
from office import paths, executor_slates
import yaml

INDEX = "Artificial Analysis Intelligence Index v4.3.2"

def test_argon_catalog_rows_exact_and_not_yet_dispatchable():
    seed = yaml.safe_load((paths.resources_root() / "catalog/seed.yaml").read_text())
    assert seed["benchmark_index_version"] == INDEX
    rows = [r for r in seed["models"] if r["model_id"] == "gemini-4-argon"]
    assert len(rows) == 2
    by_effort = {r["effort"]: r for r in rows}
    assert set(by_effort) == {"medium", "high"}
    for effort, row in by_effort.items():
        assert row["invocation_harness"] == "agy"
        assert row["invocation_model_id"] == "gemini-4-argon-" + effort
        assert row["invocation_source"] == "local-evidence:agy models"
        assert row["dispatchable"] is False
        assert row["discovery"] == "eligible"
    assert by_effort["medium"]["benchmark_indexes"] == {INDEX: None}
    assert by_effort["medium"]["source_snapshot_metadata"]["url"].endswith("#issuecomment-6433994337")
    assert by_effort["high"]["benchmark_indexes"] == {INDEX: 53}
    assert by_effort["high"]["source_snapshot_metadata"]["url"] == "https://artificialanalysis.ai/models/gemini-4-argon"

def test_argon_exception_remains_l_executor_only_and_high_is_xl():
    assert executor_slates.FLOOR_EXCEPTION == {
        "model_id": "gemini-4-argon", "effort": "medium", "role": "executor", "size": "L"
    }
    assert executor_slates.ideal_seed("L")[-1] == {"model_id": "gemini-4-argon", "effort": "medium"}
    assert executor_slates.ideal_seed("XL")[-1] == {"model_id": "gemini-4-argon", "effort": "high"}
