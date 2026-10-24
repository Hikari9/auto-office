"""Reusable fake MiMo user-catalog overlay for legacy Pi/Kilo adapter tests.

Archived MiMo records are not part of the shipped catalog. Regression tests opt
them into disposable per-test user catalogs solely to exercise harness mechanics.
"""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def isolated_retired_mimo_catalog(root: Path, harness: str) -> Path:
    archive = ROOT / "catalog" / "archive" / "retired-mimo-pi-kilo-2026-10-24.yaml"
    snapshot = yaml.safe_load(archive.read_text(encoding="utf-8"))
    rows = [{**r, "dispatchable": True} for r in snapshot["models"]
            if r.get("invocation_harness") == harness]
    assert len(rows) == 2 and {r["model_id"] for r in rows} == {
        "mimo-v2.6-pro", "mimo-v2.6-flash"}
    target = root / f"{harness}-mimo-test-only.yaml"
    target.write_text(yaml.safe_dump({"models": rows, "disabled": []}, sort_keys=False),
                      encoding="utf-8")
    return target
