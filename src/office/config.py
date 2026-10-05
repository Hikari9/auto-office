"""Effective configuration and gate resolution.

Precedence is prompt/CLI > repo > user > plugin default, unchanged from v3.
A run pins the resolved result in runs.db at start; later reads come from the
pinned snapshot, never from the live files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from office import paths
from office.util import sha256_file, sha256_obj

NON_CONFIGURABLE_KEYS = {"schema_version", "config_precedence", "hard_invariants"}
GEARS = ("direct", "direct+review", "light", "quick", "express", "full")
PLAYBOOKS = ("Change", "Restructure", "Investigate", "Prototype", "Visual")
BLAST_RADIUS = ("local", "repo", "production", "production-data")
SIZE_CLASSES = ("S", "M", "L", "XL")


def default_config_path() -> Path:
    return paths.resources_root() / "config" / "config.default.yaml"


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def deep_merge(base: Any, over: Any, tier: str, warnings: list, path: str = "") -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            loc = f"{path}.{k}" if path else k
            out[k] = deep_merge(base[k], v, tier, warnings, loc) if k in base else v
        return out
    if base is not None and over is not None and not _same_shape(base, over):
        warnings.append({"tier": tier, "key": path, "reason": "type-mismatch-ignored",
                         "expected": type(base).__name__, "got": type(over).__name__})
        return base
    return over


def _same_shape(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    return type(a) is type(b)


def read_files(repo_root: Path | None) -> dict[str, str | None]:
    """One snapshot of the user and repo config files: each file's text, read
    once, or None when absent. Values derived together (a run's pinned policy
    and its drift baseline) must come from the same snapshot, or an edit
    between two reads pins one value and records another."""
    return {tier: (path.read_text(encoding="utf-8") if path.is_file() else None)
            for tier, path in config_paths(repo_root).items()}


def config_paths(repo_root: Path | None) -> dict[str, Path]:
    """The user and repo config file paths, by tier."""
    default = load_yaml(default_config_path()) or {}
    files = {"user": paths.user_config_path()}
    if repo_root is not None:
        files["repo"] = Path(repo_root) / (default.get("paths") or {}).get("repo", ".auto-office/config.yaml")
    return files


def resolve(repo_root: Path | None, sets: list[str] | None = None,
            files: dict[str, str | None] | None = None) -> tuple[dict, list]:
    """Return (effective config, warnings). `files` is a read_files() snapshot;
    without one the files are read now."""
    default = load_yaml(default_config_path()) or {}
    allowed = set(default) - NON_CONFIGURABLE_KEYS
    files = read_files(repo_root) if files is None else files
    layers: list[tuple[str, Any]] = [(tier, yaml.safe_load(files[tier]) or {})
                                     for tier in ("user", "repo") if files.get(tier) is not None]
    cli: dict = {}
    for expr in sets or []:
        if "=" not in expr:
            raise ValueError(f"--set expects key.path=value, got {expr!r}")
        key, raw = expr.split("=", 1)
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = cli
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    if cli:
        layers.append(("prompt_cli", cli))
    warnings: list = []
    effective = default
    for tier, data in layers:
        kept = {}
        for k, v in (data or {}).items():
            if k == "schema_version":
                if v != default.get("schema_version"):
                    warnings.append({"tier": tier, "key": k, "reason": "schema-version-mismatch-ignored"})
            elif k in NON_CONFIGURABLE_KEYS:
                warnings.append({"tier": tier, "key": k, "reason": "not-configurable-ignored"})
            elif k not in allowed:
                warnings.append({"tier": tier, "key": k, "reason": "unknown-key-ignored"})
            elif tier == "repo" and k == "paths" and isinstance(v, dict) and "runs_db" in v:
                # The authority is machine-level; a repo may not move it.
                warnings.append({"tier": tier, "key": "paths.runs_db", "reason": "authority-path-not-repo-configurable"})
                kept[k] = {pk: pv for pk, pv in v.items() if pk != "runs_db"}
            else:
                kept[k] = v
        effective = deep_merge(effective, kept, tier, warnings)
    from office import adaptive
    problems = adaptive.validate(effective)
    if problems:
        raise ValueError("; ".join(problems))
    return effective, warnings


DRIFT_BLOCKS = ("quota", "roles")
FILE_BLOCKS_KEY = "_file_blocks_at_start"


def file_blocks(repo_root: Path | None, files: dict[str, str | None] | None = None) -> dict:
    """The raw quota/roles blocks of the user and repo config files, without
    defaults or --set: what a person edited, independent of the runtime.
    `files` is a read_files() snapshot; without one the files are read now."""
    files = read_files(repo_root) if files is None else files
    out: dict = {}
    for tier, text in files.items():
        try:
            data = yaml.safe_load(text) if text is not None else None
        except yaml.YAMLError:
            data = None
        data = data if isinstance(data, dict) else {}
        out[tier] = {b: data[b] for b in DRIFT_BLOCKS if b in data}
    return out


def _leaf_diffs(pinned: Any, live: Any, path: str) -> list[tuple[str, Any, Any]]:
    """(dotted key, pinned, live) for each differing leaf; a key missing on
    either side counts as a difference."""
    if isinstance(pinned, dict) and isinstance(live, dict):
        return [d for k in sorted(set(pinned) | set(live), key=str)
                for d in _leaf_diffs(pinned.get(k), live.get(k), f"{path}.{k}")]
    return [] if pinned == live else [(path, pinned, live)]


def _describe(diffs: list[tuple[str, Any, Any]], limit: int = 3) -> str:
    def show(v: Any) -> str:
        return "unset" if v is None else (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v))
    parts = [f"{k} pinned {show(a)}, live {show(b)}" for k, a, b in diffs[:limit]]
    if len(diffs) > limit:
        parts.append(f"{len(diffs) - limit} more")
    return "; ".join(parts)


def _unreadable(short: str, where: Any, err: Exception) -> str:
    detail = " ".join(str(getattr(err, "strerror", None) or err).split())[:200]
    return (f"live config could not be read or parsed ({where}: {type(err).__name__}: {detail}); drift is unknown, "
            f"and run {short} stays on its pinned values")


def config_drift(run: dict) -> str | None:
    """A notice when live config differs from what a running run pinned; the
    live values never reach that run.

    A run that recorded its files' raw quota/roles blocks at start compares
    files to files, so only a real file edit warns (a --set at start or a
    changed shipped default does not). An older run has no such baseline: its
    pinned quota/roles are compared to the live resolved values, and the
    notice does not claim a cause."""
    pinned = run.get("policy") or {}
    root = run.get("repo_root")
    repo = Path(root) if root and Path(root).is_dir() else None
    short = run["id"][:8]
    try:
        where = config_paths(repo)
        files = read_files(repo)
        for tier, text in files.items():
            try:
                yaml.safe_load(text or "")
            except yaml.YAMLError as e:
                return _unreadable(short, where[tier], e)
        live, _ = resolve(repo, files=files)
        diffs = [d for b in DRIFT_BLOCKS for d in _leaf_diffs(pinned.get(b), live.get(b), b)]
        recorded = pinned.get(FILE_BLOCKS_KEY)
        if recorded is not None:
            now = file_blocks(repo, files)
            differ = [b for b in DRIFT_BLOCKS
                      if any((now.get(t) or {}).get(b) != (recorded.get(t) or {}).get(b) for t in ("user", "repo"))]
            if not differ:
                return None
            diffs = [d for d in diffs if d[0].split(".", 1)[0] in differ]
            return (f"config edited since run start; not applied to running run {short} "
                    f"({', '.join(differ)} differ{': ' + _describe(diffs) if diffs else ''})")
    except OSError as e:
        return _unreadable(short, e.filename or "the config files", e)
    except (ValueError, yaml.YAMLError) as e:
        return _unreadable(short, "the config files", e)
    if not diffs:
        return None
    return (f"config differs from run {short}'s pinned values (pinned before Office recorded a baseline, so the "
            "cause is unknown: a file edit, a --set at start, or a changed default); "
            f"not applied to this run ({_describe(diffs)})")


def snapshot_hashes() -> dict:
    root = paths.resources_root()
    adapters = {p.name: load_yaml(p) for p in sorted((root / "adapters" / "seed").glob("*.yaml"))}
    return {
        "policy_hash": sha256_file(default_config_path()),
        "catalog_hash": sha256_obj(load_yaml(root / "catalog" / "seed.yaml")),
        "adapter_hash": sha256_obj(adapters),
    }


def resolve_risk(config: dict, blast_radius: str | None, size_class: str | None,
                 irreversible: bool) -> dict:
    """Absence is never risk, and never low risk either: unknown stays unknown."""
    signals = config.get("risk_signals") or {}
    high_blast = set(signals.get("high_blast_radius") or ["production", "production-data"])
    high_size = set(signals.get("high_size_class") or ["L", "XL"])
    high = bool(irreversible) or blast_radius in high_blast or size_class in high_size
    return {"blast_radius": blast_radius, "size_class": size_class,
            "irreversible": bool(irreversible), "high": high}


def fit_gear(requested: str | None, risk: dict, volume: bool = False, interview: bool = False,
             adversarial: bool = False) -> str:
    """The v3 fit test, unchanged: mode-preset selection is #128's scope."""
    if requested:
        return requested
    if risk.get("irreversible"):
        return "full"
    base = "express" if sum((volume, interview, adversarial)) >= 2 else "direct"
    if risk.get("high") and base == "direct":
        return "express"
    return base


def resolve_gates(gear: str, risk_high: bool, config: dict) -> dict:
    preset = (config.get("gear_presets") or {}).get(gear, {})
    ad_hoc = config.get("ad_hoc_review_max_rounds")

    def value(v):
        return bool(risk_high) if v == "risk_forced" else v

    plan_review = value(preset.get("plan_review", False))
    code_review = value(preset.get("independent_code_review", False))
    plan_rounds = preset.get("plan_review_max_rounds")
    code_rounds = preset.get("code_review_max_rounds")
    if plan_review and plan_rounds is None:
        plan_rounds = ad_hoc
    if code_review and code_rounds is None:
        code_rounds = ad_hoc
    planner = preset.get("dedicated_planner", False)
    verification = config.get("verification") or {}
    return {
        # `policy_optional` / `shallow` / `cost_bounded` fund the gate; they
        # shape its depth, they do not remove it.
        "plan_review": bool(plan_review),
        "plan_review_depth": plan_review if isinstance(plan_review, str) else "full",
        "code_review": bool(code_review),
        "code_review_depth": code_review if isinstance(code_review, str) else "full",
        "visual": value(preset.get("funded_browser_verification", False)),
        "plan_review_max_rounds": plan_rounds or 1,
        "code_review_max_rounds": code_rounds or 1,
        "visual_review_max_rounds": verification.get("visual_review_max_rounds", code_rounds or 1),
        "environment_retry_max": int(verification.get("environment_retry_max", 2)),
        "recapture_max": int(verification.get("recapture_max", 1)),
        "review_reprompt_max": int(verification.get("review_reprompt_max", 3)),
        "dedicated_planner": planner is True,
    }


def quota_reserve_percent(config: dict) -> float:
    return float(((config.get("quota") or {}).get("reserve_percent", 5)))


def balanced_money_band_percent(config: dict) -> float:
    return float(((config.get("cost_policy") or {}).get("balanced_money_band_percent", 20)))
