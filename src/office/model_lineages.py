"""Explicit model lineage/generation identity and a pure latest-two window.

The canonical identity map is not inferred from model names. Unknown/preview
generations are pending, never allowed to evict an established active release.
This module does *not* make a route trusted, available or user-permitted.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from office import paths

LINEAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")
NUMERIC_VERSION_RE = re.compile(r"^[0-9]+(?:[.-][0-9]+)*$")


class LineageError(ValueError):
    """A catalog entry cannot safely enter the released-generation window."""


def version_parts(value: str) -> tuple[int, ...]:
    """Normalize numerically, including 5-5 == 5.5; never compare as strings."""
    if not isinstance(value, str) or not NUMERIC_VERSION_RE.fullmatch(value):
        raise LineageError(f"invalid numeric generation version: {value!r}")
    parts = tuple(int(x) for x in re.split(r"[.-]", value))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def load(path: Path | None = None) -> dict[str, dict]:
    """Load and validate every explicit named-lineage generation declaration."""
    path = path or paths.resources_root() / "catalog" / "lineages.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("generations"), dict):
        raise LineageError("lineages.yaml requires schema_version 1 and generations mapping")
    entries: dict[str, dict] = {}
    by_version: dict[tuple[str, tuple[int, ...]], str] = {}
    for model, raw in data["generations"].items():
        if not isinstance(model, str) or not isinstance(raw, dict):
            raise LineageError("generation entries need string model IDs and mappings")
        lineage, version = raw.get("lineage_key"), raw.get("generation_version")
        if not isinstance(lineage, str) or not LINEAGE_KEY_RE.fullmatch(lineage):
            raise LineageError(f"{model}: lineage_key must be provider/series")
        normalized = version_parts(version)
        if raw.get("release_status") not in ("released", "preview", "pending"):
            raise LineageError(f"{model}: unknown release_status")
        if not isinstance(raw.get("provenance"), str) or not raw["provenance"].strip():
            raise LineageError(f"{model}: provenance is required")
        if raw["release_status"] == "released":
            key = (lineage, normalized)
            if key in by_version:
                raise LineageError(f"same lineage release {lineage}@{version} has two canonical models: {by_version[key]}, {model}")
            by_version[key] = model
        entries[model] = {**raw, "canonical_generation_id": model}
    return entries


def alias_lineages(path: Path | None = None) -> dict[str, str]:
    """Explicit series aliases; a regex or a vendor-named preview is never release proof."""
    path = path or paths.resources_root() / "catalog" / "lineages.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    aliases = data.get("aliases") or {}
    if not isinstance(aliases, dict) or any(
        not isinstance(alias, str) or not isinstance(lineage, str)
        or not LINEAGE_KEY_RE.fullmatch(lineage) for alias, lineage in aliases.items()
    ):
        raise LineageError("lineages.yaml aliases must map model names to provider/lineage keys")
    released = {v["lineage_key"] for v in load(path).values()}
    if any(lineage not in released for lineage in aliases.values()):
        raise LineageError("an alias refers to a lineage without released canonical generations")
    return aliases


def identify(model_id: str | None, *, aliases: dict | None = None,
             manifest: dict[str, dict] | None = None) -> dict | None:
    """Return known *concrete* identity, or None. Never guess from spelling."""
    target = (aliases or {}).get("alias_resolved_to") or model_id
    entry = (manifest if manifest is not None else load()).get(target)
    return {**entry, "canonical_generation_id": target} if entry else None


def annotate(rows: list[dict], *, manifest: dict[str, dict] | None = None) -> list[dict]:
    """Copy rows with explicit lineage metadata; don't mutate shipped/user YAML."""
    manifest = load() if manifest is None else manifest
    out: list[dict] = []
    for row in rows:
        identity = identify(row.get("model_id"), aliases=row, manifest=manifest)
        out.append({**row, **{
            key: identity[key] for key in ("lineage_key", "generation_version", "canonical_generation_id")
        }} if identity else dict(row))
    return out


def partition(rows: list[dict], *, manifest: dict[str, dict] | None = None) -> dict[str, list[dict]]:
    """Partition concrete variants using two released generations per named lineage.

    Alias rows remain separate, and pending/unknown versions do not displace a
    known release. This is a *staging* view: callers must publish a complete,
    hash-pinned catalog transaction before changing live routing.
    """
    manifest = load() if manifest is None else manifest
    annotated = annotate(rows, manifest=manifest)
    released: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    for row in annotated:
        if row.get("alias_family"):
            continue
        meta = identify(row.get("model_id"), manifest=manifest)
        if meta and meta["release_status"] == "released":
            released[meta["lineage_key"]].add(version_parts(meta["generation_version"]))
    windows = {lineage: set(sorted(versions)[-2:]) for lineage, versions in released.items()}
    result: dict[str, list[dict]] = {"active": [], "archived": [], "pending": [], "aliases": []}
    for row in annotated:
        if row.get("alias_family"):
            result["aliases"].append(row)
            continue
        meta = identify(row.get("model_id"), manifest=manifest)
        if not meta or meta["release_status"] != "released":
            result["pending"].append({**row, "catalog_status_reason": (
                "generation identity unknown" if not meta else "generation not released")})
            continue
        destination = "active" if version_parts(meta["generation_version"]) in windows[meta["lineage_key"]] else "archived"
        result[destination].append(row)
    return result
