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


class OverlayUnreadable(Exception):
    pass


def load(*, strict: bool = False) -> dict:
    """The overlay as `{models: [...], disabled: [...]}`. Absent is empty.
    Unparseable reads as empty for routing, but with `strict` (every writer)
    raises OverlayUnreadable so a save never overwrites the user's rows."""
    target = path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"models": [], "disabled": []}
    except OSError as e:
        if strict:
            raise OverlayUnreadable(f"{target}: {e}")
        return {"models": [], "disabled": []}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        if strict:
            raise OverlayUnreadable(f"{target} is not valid YAML: {str(e).splitlines()[0]}")
        return {"models": [], "disabled": []}
    if not isinstance(data, dict):
        if strict:
            raise OverlayUnreadable(f"{target} is not a YAML mapping")
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
    return merge(rows, overlay)[0]


def _key(row: dict) -> tuple:
    return (row.get("invocation_harness"), row.get("model_id"), row.get("effort"))


def merge(rows: list[dict], overlay: dict | None = None) -> tuple[list[dict], list[str]]:
    """(rows, warnings). An overlay row whose harness/model/effort is already a
    row never adds a second one: `dispatchable: false` on it disables the
    existing row, anything else is skipped with a warning."""
    overlay = load() if overlay is None else overlay
    if not overlay["models"] and not overlay["disabled"]:
        return rows, []
    out = list(rows)
    index = {_key(r): i for i, r in enumerate(out)}
    warnings = []
    for raw_row in overlay["models"]:
        key = _key(raw_row)
        if key in index:
            label = f"{key[0]}/{key[1]}@{key[2]}"
            if raw_row.get("dispatchable") is False:
                out[index[key]] = {**out[index[key]], "dispatchable": False}
            else:
                warnings.append(f"user catalog row {label} duplicates an existing row; skipped "
                                "(only dispatchable: false may override one)")
            continue
        index[key] = len(out)
        out.append(dict(raw_row))
    out = [{**r, "dispatchable": False} if any(matches(d, r) for d in overlay["disabled"]) else r for r in out]
    return out, warnings


def raw() -> dict | None:
    """The overlay file's parsed content for run pinning, or None when absent."""
    try:
        text = path().read_bytes()
    except OSError:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        import hashlib
        return {"unparseable_sha256": hashlib.sha256(text).hexdigest()}
