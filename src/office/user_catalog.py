"""The user-level catalog overlay: rows `office model add` appends and rows
`office model disable` turns off, kept beside the user config file.

The packaged seed (`catalog/seed.yaml`) is never written. The overlay holds
two lists, `models` (catalog rows in the seed's shape) and `disabled`
(`{harness, model_id, effort?, reason?}` entries that set `dispatchable:
false` on matching rows). A row added here carries no trust: routing still
derives trust from recorded trust acts only.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from office import paths


def path() -> Path:
    raw = os.environ.get("OFFICE_USER_CATALOG")
    return Path(raw).expanduser() if raw else paths.user_config_path().parent / "catalog.yaml"


def load() -> dict:
    """The overlay as `{models: [...], disabled: [...]}`; absent or unreadable is empty."""
    try:
        data = yaml.safe_load(path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"models": [], "disabled": []}
    if not isinstance(data, dict):
        return {"models": [], "disabled": []}
    models = [r for r in data.get("models") or [] if isinstance(r, dict)]
    disabled = [d for d in data.get("disabled") or [] if isinstance(d, dict)]
    return {"models": models, "disabled": disabled}


def save(data: dict) -> Path:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {"kind": "user-catalog-overlay", "models": data.get("models") or [], "disabled": data.get("disabled") or []}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


def matches(entry: dict, row: dict) -> bool:
    return (entry.get("harness") == row.get("invocation_harness") and entry.get("model_id") == row.get("model_id")
            and (not entry.get("effort") or entry.get("effort") == row.get("effort")))


def apply(rows: list[dict], overlay: dict | None = None) -> list[dict]:
    """Seed rows followed by the overlay's rows, with disabled rows marked
    `dispatchable: false`. With no overlay the seed rows come back unchanged."""
    overlay = load() if overlay is None else overlay
    if not overlay["models"] and not overlay["disabled"]:
        return rows
    out = []
    for row in list(rows) + [dict(r) for r in overlay["models"]]:
        if any(matches(d, row) for d in overlay["disabled"]):
            row = {**row, "dispatchable": False}
        out.append(row)
    return out


def raw() -> dict | None:
    """The overlay file's parsed content for run pinning, or None when absent."""
    try:
        return yaml.safe_load(path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
