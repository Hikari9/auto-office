"""One authoritative, losslessly normalized shipped catalog (#852).

The Git-tracked tables are the only shipped model registry. Legacy seed-shaped
objects are assembled in memory for existing callers, snapshots and hashes.
There is no fallback to catalog/seed.yaml at runtime.
"""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Any

import yaml

from office import paths

FILES = ("manifest.yaml", "routes.tsv", "benchmarks.tsv", "provenance.yaml")
# All scalar seed fields live in routes.tsv. A missing field is different from
# an explicitly null field; nested or additional fields live in provenance.yaml.
SCALARS = (
    "model_id", "invocation_harness", "effort", "invocation_model_id",
    "invocation_source", "source_identifier", "source_name", "source_effort",
    "effort_confidence", "alias_family", "dispatchable", "discovery",
    "discovery_reason", "free_model", "release_date", "content_hash",
)
ROUTE_HEADER = ("route_key", *SCALARS, "benchmark_shape")
BENCHMARK_HEADER = ("route_key", "index_version", "score")
BOOL_FIELDS = frozenset(("dispatchable", "free_model"))
EFFORTS = frozenset(("none", "low", "medium", "high", "xhigh", "max"))
REQUIRED = ("model_id", "invocation_harness", "effort", "invocation_source")
_MISSING = object()


class CatalogError(ValueError):
    """Invalid shipped catalog, with a file and physical line when available."""


def files(root: Path | None = None) -> tuple[Path, ...]:
    directory = (Path(root) if root is not None else paths.resources_root()) / "catalog"
    return tuple(directory / name for name in FILES)


def key(row: dict[str, Any]) -> str:
    return f"{row['invocation_harness']}/{row['model_id']}@{row['effort']}"


def _encode(value: Any) -> str:
    if value is _MISSING:
        return r"\M"
    if value is None:
        return r"\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "\\" + value if value.startswith("\\") else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise CatalogError(f"non-scalar value cannot fit in TSV: {value!r}")


def _decode(value: str, field: str, location: str) -> Any:
    if value == r"\M":
        return _MISSING
    if value == r"\N":
        return None
    if value.startswith("\\\\"):
        value = value[1:]  # literal leading backslash, including literal \\N or \\M
    if field in BOOL_FIELDS:
        if value not in ("true", "false"):
            raise CatalogError(f"{location}: {field} must be true, false, \\N or \\M, got {value!r}")
        return value == "true"
    return value


def _rows(path: Path, header: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source, delimiter="\t", strict=True)
            try:
                actual = next(reader)
            except StopIteration:
                raise CatalogError(f"{path}:1: missing TSV header") from None
            if actual != list(header):
                raise CatalogError(f"{path}:1: expected TSV columns {list(header)!r}, got {actual!r}")
            out = []
            for record in reader:
                lineno = reader.line_num
                if len(record) != len(header):
                    raise CatalogError(f"{path}:{lineno}: expected {len(header)} tab-separated columns, got {len(record)}")
                out.append((lineno, dict(zip(header, record))))
            return out
    except csv.Error as exc:
        raise CatalogError(f"{path}: malformed TSV: {exc}") from exc
    except UnicodeError as exc:
        raise CatalogError(f"{path}: invalid UTF-8: {exc}") from exc


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: expected a YAML mapping")
    return data


def _identity(row: dict, location: str) -> None:
    for name in REQUIRED:
        if not isinstance(row.get(name), str) or not row[name].strip():
            raise CatalogError(f"{location}: {name} must be a nonempty string")
    if not re.fullmatch(r"[a-z0-9-]+", row["invocation_harness"]):
        raise CatalogError(f"{location}: invalid invocation_harness {row['invocation_harness']!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", row["model_id"]):
        raise CatalogError(f"{location}: invalid model_id {row['model_id']!r}")
    if row["effort"] not in EFFORTS:
        raise CatalogError(f"{location}: unknown effort {row['effort']!r}")


def _score(value: str, location: str) -> int | float | None:
    if value == r"\N":
        return None
    if value == r"\M":
        raise CatalogError(f"{location}: a missing score is not a benchmark observation")
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        return int(value)
    try:
        number = float(value)
    except ValueError:
        raise CatalogError(f"{location}: score must be a finite number or \\N, got {value!r}") from None
    if not math.isfinite(number):
        raise CatalogError(f"{location}: score must be finite, got {value!r}")
    return number


def load(root: Path | None = None) -> dict[str, Any]:
    """Build the legacy-shaped catalog from the canonical shipped directory."""
    base = Path(root) if root is not None else paths.resources_root()
    return _load_directory(base / "catalog")


def _load_directory(directory: Path) -> dict[str, Any]:
    """Read one explicit catalog directory, preserving normalized seed hashes.

    Fail closed on malformed tables, broken joins, invalid scalar types, or
    extra/missing provenance. The row order is defined by routes.tsv.
    """
    manifest_path, route_path, bench_path, provenance_path = (
        directory / name for name in FILES
    )
    manifest = _yaml(manifest_path)
    if "models" in manifest:
        raise CatalogError(f"{manifest_path}: models belong only in routes.tsv")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 3:
        raise CatalogError(f"{manifest_path}: expected legacy schema_version 3")
    if not isinstance(manifest.get("benchmark_index_version"), str):
        raise CatalogError(f"{manifest_path}: benchmark_index_version must be a string")
    if manifest.get("kind") != "shipped-seed-catalog":
        raise CatalogError(f"{manifest_path}: expected kind shipped-seed-catalog")

    rows: dict[str, dict] = {}
    shapes: dict[str, str] = {}
    for lineno, entry in _rows(route_path, ROUTE_HEADER):
        location = f"{route_path}:{lineno}"
        row: dict[str, Any] = {}
        for name in SCALARS:
            value = _decode(entry[name], name, location)
            if value is not _MISSING:
                row[name] = value
        _identity(row, location)
        route_key = key(row)
        if entry["route_key"] != route_key:
            raise CatalogError(f"{location}: route_key {entry['route_key']!r} must equal {route_key!r}")
        if route_key in rows:
            raise CatalogError(f"{location}: duplicate route_key {route_key!r}")
        shape = entry["benchmark_shape"]
        if shape not in ("map", "null", "missing"):
            raise CatalogError(f"{location}: benchmark_shape must be map, null or missing")
        for name in SCALARS:
            if name not in row or row[name] is None:
                continue
            if name not in BOOL_FIELDS and not isinstance(row[name], str):
                raise CatalogError(f"{location}: {name} must be a string")
        rows[route_key] = row
        shapes[route_key] = shape

    for route_key, shape in shapes.items():
        if shape == "map":
            rows[route_key]["benchmark_indexes"] = {}
        elif shape == "null":
            rows[route_key]["benchmark_indexes"] = None

    known_observations: set[tuple[str, str]] = set()
    for lineno, entry in _rows(bench_path, BENCHMARK_HEADER):
        location = f"{bench_path}:{lineno}"
        route_key, version = entry["route_key"], entry["index_version"]
        if route_key not in rows:
            raise CatalogError(f"{location}: unknown route_key {route_key!r}")
        if shapes[route_key] != "map":
            raise CatalogError(f"{location}: route {route_key!r} has no benchmark map")
        if not version.strip():
            raise CatalogError(f"{location}: index_version must be nonempty")
        obs_key = (route_key, version)
        if obs_key in known_observations:
            raise CatalogError(f"{location}: duplicate benchmark {obs_key!r}")
        known_observations.add(obs_key)
        rows[route_key]["benchmark_indexes"][version] = _score(entry["score"], location)

    provenance = _yaml(provenance_path)
    if set(provenance) != {"rows"} or not isinstance(provenance["rows"], dict):
        raise CatalogError(f"{provenance_path}: expected only a rows mapping")
    if set(provenance["rows"]) != set(rows):
        missing = sorted(set(rows) - set(provenance["rows"]))
        unknown = sorted(set(provenance["rows"]) - set(rows))
        raise CatalogError(f"{provenance_path}: missing route references {missing}; unknown references {unknown}")
    for route_key, fields in provenance["rows"].items():
        if not isinstance(fields, dict):
            raise CatalogError(f"{provenance_path}: {route_key} must map to fields")
        collisions = set(fields) & (set(SCALARS) | {"benchmark_indexes"})
        if collisions:
            raise CatalogError(f"{provenance_path}: {route_key} duplicates TSV fields {sorted(collisions)}")
        rows[route_key].update(fields)
    return {**manifest, "models": list(rows.values())}


def export_legacy(seed: dict, directory: Path) -> None:
    """One-time *explicit* converter; never invoked by runtime/CLI writers."""
    directory = Path(directory)
    if not isinstance(seed, dict):
        raise CatalogError("legacy seed must be a mapping")
    models = seed.get("models")
    if not isinstance(models, list):
        raise CatalogError("legacy seed must have a models list")
    seen: set[str] = set()
    routes = []
    benchmarks = []
    provenance: dict[str, dict] = {}
    for number, row in enumerate(models, 1):
        if not isinstance(row, dict):
            raise CatalogError(f"legacy model row {number} is not a mapping")
        _identity(row, f"legacy model row {number}")
        route_key = key(row)
        if route_key in seen:
            raise CatalogError(f"legacy model row {number}: duplicate {route_key!r}")
        seen.add(route_key)
        idx = row.get("benchmark_indexes", _MISSING)
        if idx is _MISSING:
            shape = "missing"
        elif idx is None:
            shape = "null"
        elif isinstance(idx, dict):
            shape = "map"
            for version, score in idx.items():
                if not isinstance(version, str) or score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
                    raise CatalogError(f"legacy model row {number}: malformed benchmark indexes")
                benchmarks.append((route_key, version, _encode(score)))
        else:
            raise CatalogError(f"legacy model row {number}: benchmark_indexes must be a map or null")
        routes.append((route_key, *(_encode(row.get(name, _MISSING)) for name in SCALARS), shape))
        provenance[route_key] = {k: v for k, v in row.items()
                                 if k not in SCALARS and k != "benchmark_indexes"}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump({k: v for k, v in seed.items() if k != "models"},
                       sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    for filename, header, data in (("routes.tsv", ROUTE_HEADER, routes),
                                   ("benchmarks.tsv", BENCHMARK_HEADER, benchmarks)):
        with (directory / filename).open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, delimiter="\t", lineterminator="\n")
            writer.writerow(header)
            writer.writerows(data)
    (directory / "provenance.yaml").write_text(
        yaml.safe_dump({"rows": provenance}, sort_keys=False,
                       allow_unicode=True, width=120), encoding="utf-8")
    # Enforce a complete, typed field-by-field round trip before claiming parity.
    actual = _load_directory(directory)
    if actual != seed:
        raise CatalogError("legacy conversion changed normalized model catalog data")
