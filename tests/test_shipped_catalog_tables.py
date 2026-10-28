"""#852: lossless TSV+YAML shipped catalog and strict corruption diagnostics."""
from __future__ import annotations

from pathlib import Path

import pytest

from office import shipped_catalog
from office.util import sha256_obj


def _fixture() -> dict:
    def route(name, effort, benchmark, **extras):
        return {
            "model_id": name, "invocation_harness": "codex", "effort": effort,
            "invocation_model_id": None, "invocation_source": "documented:example",
            "source_identifier": "", "source_name": "fixture",
            "source_effort": "Low", "effort_confidence": "mapped",
            "price_fields": {"input_per_mtok": 0, "output_per_mtok": 0.0},
            "speed_fields": None, "source_snapshot_metadata": {"kind": "test", "notes": ""},
            "release_date": "", "dispatchable": False, "content_hash": None,
            "supersession_metadata": {}, **({"benchmark_indexes": benchmark} if benchmark is not ... else {}), **extras,
        }
    return {
        "schema_version": 3, "kind": "shipped-seed-catalog", "notes": "test",
        "benchmark_index_version": "index v1", "benchmark_index_notes": "no real scores",
        "models": [
            route("alpha", "low", {"index v1": 0, "index v2": None},
                  alias_family=r"^gpt-(?P<version>\\d+)-alpha$", discovery="eligible", free_model=False),
            route("beta", "medium", {}, discovery=None, discovery_reason=""),
            route("gamma", "none", None, source_identifier=None, dispatchable=None),
            route("delta", "high", ..., invocation_source=r"\N", source_name=r"\M"),
        ],
    }


@pytest.fixture
def table_dir(tmp_path):
    target = tmp_path / "catalog"
    shipped_catalog.export_legacy(_fixture(), target)
    return target


def _replace(path: Path, old: str, new: str):
    s = path.read_text(encoding="utf-8")
    assert old in s
    path.write_text(s.replace(old, new, 1), encoding="utf-8")


def test_shipped_identity_and_benchmark_index_are_complete():
    # No frozen snapshot hash: legitimate model additions and score refreshes
    # must not require editing a historic hash constant in this test.
    live = shipped_catalog.load()
    keys = [shipped_catalog.key(row) for row in live["models"]]
    assert keys and len(keys) == len(set(keys))
    assert all(row.get("invocation_source") for row in live["models"])
    assert isinstance(live["benchmark_index_version"], str)



def test_roundtrip_all_seed_types_without_collapsing_absent_null_or_zero(table_dir):
    live = shipped_catalog.load(table_dir.parent)
    assert live == _fixture()
    alpha, beta, gamma, delta = live["models"]
    assert alpha["benchmark_indexes"]["index v1"] == 0
    assert alpha["benchmark_indexes"]["index v2"] is None
    assert alpha["free_model"] is False and alpha["dispatchable"] is False
    assert beta["benchmark_indexes"] == {} and beta["discovery"] is None
    assert gamma["benchmark_indexes"] is None and gamma["source_identifier"] is None
    assert "benchmark_indexes" not in delta
    assert "dispatchable" in gamma and gamma["dispatchable"] is None
    assert delta["invocation_source"] == r"\N" and delta["source_name"] == r"\M"
    assert alpha["price_fields"]["output_per_mtok"] == 0.0
    assert type(alpha["price_fields"]["output_per_mtok"]) is float
    assert delta["supersession_metadata"] == {}


def test_canonical_hash_not_affected_by_yaml_comments_or_formatting(table_dir):
    before = sha256_obj(shipped_catalog.load(table_dir.parent))
    p = table_dir / "manifest.yaml"
    p.write_text("# change not data\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert sha256_obj(shipped_catalog.load(table_dir.parent)) == before


@pytest.mark.parametrize("file,old,new,needle", [
    ("routes.tsv", "codex/alpha@low", "codex/wrong@low", "route_key"),
    ("routes.tsv", "\tlow\t", "\tinvalid\t", "unknown effort"),
    ("routes.tsv", "\tfalse\t", "\tmaybe\t", "dispatchable must be"),
    ("benchmarks.tsv", "index v1\t0", "index v1\tnan", "finite"),
    ("benchmarks.tsv", "index v1\t0", "index v1\tnope", "finite number"),
    ("benchmarks.tsv", "codex/alpha@low", "codex/unknown@low", "unknown route_key"),
    ("benchmarks.tsv", "index v1\t0", "index v1\t\\M", "missing score"),
    ("routes.tsv", "\tmap\n", "\tinvalid\n", "benchmark_shape"),
])
def test_bad_table_fails_closed_with_file_and_line(table_dir, file, old, new, needle):
    _replace(table_dir / file, old, new)
    with pytest.raises(shipped_catalog.CatalogError) as exc:
        shipped_catalog.load(table_dir.parent)
    assert needle in str(exc.value)
    assert file in str(exc.value)
    assert ":" in str(exc.value)



def test_duplicate_route_key_rejected(table_dir):
    p = table_dir / "routes.tsv"
    lines = p.read_text().splitlines()
    p.write_text("\n".join([*lines, lines[1]]) + "\n")
    with pytest.raises(shipped_catalog.CatalogError, match="duplicate route_key"):
        shipped_catalog.load(table_dir.parent)

def test_duplicate_benchmark_rejected(table_dir):
    p = table_dir / "benchmarks.tsv"
    lines = p.read_text().splitlines()
    p.write_text("\n".join([*lines, lines[1]]) + "\n")
    with pytest.raises(shipped_catalog.CatalogError, match="duplicate benchmark"):
        shipped_catalog.load(table_dir.parent)


def test_benchmark_without_map_rejected(table_dir):
    p = table_dir / "benchmarks.tsv"
    p.write_text(p.read_text() + "codex/gamma@none\tindex v1\t4\n")
    with pytest.raises(shipped_catalog.CatalogError, match="no benchmark map"):
        shipped_catalog.load(table_dir.parent)


def test_missing_provenance_reference_rejected(table_dir):
    p = table_dir / "provenance.yaml"
    p.write_text(p.read_text().replace("  codex/alpha@low:", "  codex/unknown@low:", 1))
    with pytest.raises(shipped_catalog.CatalogError, match="references"):
        shipped_catalog.load(table_dir.parent)


def test_invalid_tsv_shape_rejected(table_dir):
    p = table_dir / "benchmarks.tsv"
    p.write_text(p.read_text() + "codex/alpha@low\tindex v3\t10\textra\n")
    with pytest.raises(shipped_catalog.CatalogError, match="tab-separated columns"):
        shipped_catalog.load(table_dir.parent)


def test_legacy_converter_does_not_drop_unrecognized_metadata(table_dir):
    seed = _fixture()
    seed["models"][0]["task_benchmarks"] = {"code": 0, "vision": None}
    seed["models"][0]["new_nested_field"] = {"present": False, "unknown": ""}
    shipped_catalog.export_legacy(seed, table_dir)
    assert shipped_catalog.load(table_dir.parent) == seed


def test_legacy_converter_accepts_output_directory_not_named_catalog(tmp_path):
    unusual = tmp_path / "custom-output"
    seed = _fixture()
    shipped_catalog.export_legacy(seed, unusual)
    assert all((unusual / name).is_file() for name in shipped_catalog.FILES)
    assert shipped_catalog._load_directory(unusual) == seed


def test_legacy_converter_rejects_non_mapping_seed(tmp_path):
    with pytest.raises(shipped_catalog.CatalogError, match="mapping"):
        shipped_catalog.export_legacy([], tmp_path / "custom-output")


def test_shipped_files_are_the_only_registry():
    assert not (shipped_catalog.files()[0].parent / "seed.yaml").exists()
    assert all(p.is_file() for p in shipped_catalog.files())
