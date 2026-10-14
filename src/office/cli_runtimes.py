"""Minimal CLI harness lookup index (#633).

The TSV maps harness keys to skill paths and *derived* model-list paths. It
never replaces adapters, model/catalog policy, conformance or Office authority.
`office harness sync` materializes a machine-local copy and read-only catalog
projections. Nothing executes a CLI, installs hooks or grants trust.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from office import adapters, paths
from office.util import sha256_obj

COLUMNS = ("key", "skill_path", "models_path")
KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MODEL_KIND = "office-derived-harness-model-list"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    key: str
    skill_path: str
    models_path: str


def location() -> Path:
    raw = os.environ.get("OFFICE_CLI_RUNTIMES")
    return Path(raw).expanduser() if raw else paths.user_config_path().parent / "cli-runtimes.tsv"


def packaged_default() -> Path:
    return paths.resources_root() / "config" / "cli-runtimes.tsv"


def parse(text: str) -> list[Entry]:
    """Strict three-field TSV; no shell syntax, quoted fields or extra columns."""
    lines = [(n, raw) for n, raw in enumerate(text.splitlines(), 1)
             if raw.strip() and not raw.lstrip().startswith("#")]
    if not lines or lines[0][1].split("\t") != list(COLUMNS):
        raise ManifestError("first row must be: key<TAB>skill_path<TAB>models_path")
    found: set[str] = set()
    result = []
    for number, raw in lines[1:]:
        columns = raw.split("\t")
        if len(columns) != 3:
            raise ManifestError(f"line {number}: expected exactly three tab-separated fields")
        key, skill, models = columns
        if not KEY.fullmatch(key):
            raise ManifestError(f"line {number}: invalid harness key {key!r}")
        if key in found:
            raise ManifestError(f"line {number}: duplicate harness key {key!r}")
        if not skill or not models or any(c in skill + models for c in ('\x00', '\r', '\n')):
            raise ManifestError(f"line {number}: paths must be nonempty, single-line file paths")
        if skill.startswith("skills/") and ".." in Path(skill).parts:
            raise ManifestError(f"line {number}: bundled skills path cannot escape resources/skills")
        result.append(Entry(key, skill, models))
        found.add(key)
    return result


def render(entries: list[Entry]) -> str:
    return "\t".join(COLUMNS) + "\n" + "".join(
        f"{e.key}\t{e.skill_path}\t{e.models_path}\n" for e in entries)


def load() -> tuple[list[Entry], bool]:
    """(entries, saved). Never silently reset an invalid user manifest."""
    target = location()
    source = target if target.exists() else packaged_default()
    try:
        return parse(source.read_text(encoding="utf-8")), source == target
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"cannot read {source}: {exc}") from exc


def resolve(entry: Entry, field: str) -> Path:
    value = entry.skill_path if field == "skill" else entry.models_path
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if field == "skill" and value.startswith("skills/"):
        # Bundled skills follow the source checkout *or* wheel resources root.
        target = (paths.resources_root() / candidate).resolve()
        if not target.is_relative_to((paths.resources_root() / "skills").resolve()):
            raise ManifestError(f"{entry.key}: bundled skill escapes resources/skills")
        return target
    return (location().parent / candidate).resolve()


def _projection_rows(rows: list[dict], harness: str) -> list[dict]:
    keep = ("model_id", "invocation_model_id", "effort", "dispatchable", "lineage_key",
            "generation_version", "alias_resolved_to", "source_identifier")
    mine = [{k: r.get(k) for k in keep} for r in rows if r.get("invocation_harness") == harness]
    return sorted(mine, key=lambda r: (str(r["model_id"]), str(r["effort"]), str(r["invocation_model_id"])))


def projection(rows: list[dict], harness: str) -> dict:
    """View of the *existing* catalog, never a new source for routing/policy."""
    mine = _projection_rows(rows, harness)
    return {"kind": MODEL_KIND, "schema_version": 1, "harness": harness,
            "source": "office canonical catalog (seed, lineage and user overlay)",
            "content_hash": sha256_obj(mine), "models": mine}


def _managed(entry: Entry) -> bool:
    """Only these default names are safe to regenerate. Never clobber a custom path."""
    return entry.models_path == f"models/{entry.key}.json"


def _atomic_text(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except (OSError, UnicodeError):
            pass
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as f:
        temp = Path(f.name)
        try:
            os.chmod(temp, 0o600)
            f.write(text)
            f.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return True


def _model_json(rows: list[dict], harness: str) -> str:
    return json.dumps(projection(rows, harness), sort_keys=True, indent=2) + "\n"


def _write_models(entries: list[Entry], rows: list[dict]) -> tuple[int, list[str]]:
    written = 0
    warnings = []
    for entry in entries:
        if not _managed(entry):
            warnings.append(f"{entry.key}: custom models_path left unchanged (Office only writes models/<key>.json)")
            continue
        # A symlinked models directory could redirect our write into arbitrary
        # user files; resolve only after enforcing the parent is not a symlink.
        models_dir = location().parent / "models"
        if models_dir.is_symlink():
            raise ManifestError(f"{models_dir}: refusing symlinked generated-model directory")
        if _atomic_text(models_dir / f"{entry.key}.json", _model_json(rows, entry.key)):
            written += 1
    return written, warnings


def sync() -> tuple[Path, int, int, list[str]]:
    """Create/preserve editable mapping; derive model files from canonical rows.

    Existing user entries keep their exact paths/order. Newly loaded adapter
    ids append deterministically, so new harnesses need no Python tuple edit.
    """
    entries, saved = load()
    ids = {e.key for e in entries}
    added = []
    for aid in sorted(set(adapters.load_all()) - ids):
        if KEY.fullmatch(aid):
            entry = Entry(aid, f"skills/{aid}-cli/SKILL.md", f"models/{aid}.json")
            entries.append(entry)
            added.append(entry)
    # Check before touching any user file (including when the manifest is new).
    for entry in entries:
        if entry.skill_path.startswith("skills/"):
            resolve(entry, "skill")
    models_dir = location().parent / "models"
    if models_dir.is_symlink():
        raise ManifestError(f"{models_dir}: refusing symlinked generated-model directory")
    from office import candidates
    rows = candidates.catalog_rows()
    target = location()
    if not saved:
        _atomic_text(target, render(entries))
    elif added:
        # Leave existing rows, whitespace and user comments untouched.
        existing = target.read_text(encoding="utf-8")
        text = existing + ("" if existing.endswith("\n") else "\n")
        text += "".join(f"{e.key}\t{e.skill_path}\t{e.models_path}\n" for e in added)
        _atomic_text(target, text)
    count, warnings = _write_models(entries, rows)
    return target, len(entries), count, warnings


def refresh_if_saved() -> str | None:
    """After an explicit model edit, refresh only Office-managed projections."""
    if not location().is_file():
        return None
    try:
        entries, _ = load()
        from office import candidates
        count, warnings = _write_models(entries, candidates.catalog_rows())
        return (f"CLI model projections refreshed: {count}" +
                (f" ({len(warnings)} custom paths not overwritten)" if warnings else ""))
    except (ManifestError, OSError) as exc:
        return f"CLI runtime model projection refresh skipped: {exc} (office harness sync)"


def statuses() -> tuple[list[dict], bool]:
    entries, saved = load()
    from office import candidates
    rows = candidates.catalog_rows()
    adapters_loaded = adapters.load_sources()
    out = []
    for entry in entries:
        skill = resolve(entry, "skill")
        model_file = resolve(entry, "models")
        expected = projection(rows, entry.key)
        if not model_file.is_file():
            model_state = "missing"
        else:
            try:
                actual = json.loads(model_file.read_text(encoding="utf-8"))
                model_state = "ok" if actual == expected else "stale"
            except (OSError, UnicodeError, ValueError):
                model_state = "invalid"
        skill_ok = skill.is_file()
        adapter_ok = entry.key in adapters_loaded and bool(adapters_loaded[entry.key][0].get("office_profiles"))
        out.append({"key": entry.key, "skill_path": str(skill), "models_path": str(model_file),
                    "skill": "ok" if skill_ok else "missing", "models": model_state,
                    "adapter": "registered" if adapter_ok else "missing", "model_count": len(expected["models"]),
                    "configured": skill_ok and model_state == "ok" and adapter_ok})
    return out, saved
