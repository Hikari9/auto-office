"""Effective configuration and gate resolution.

Precedence is prompt/CLI > repo > user > plugin default, unchanged from v3.
A run pins the resolved result in runs.db at start; later reads come from the
pinned snapshot, never from the live files.
"""
from __future__ import annotations

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


def resolve(repo_root: Path | None, sets: list[str] | None = None) -> tuple[dict, list]:
    """Return (effective config, warnings)."""
    default = load_yaml(default_config_path()) or {}
    allowed = set(default) - NON_CONFIGURABLE_KEYS
    layers: list[tuple[str, Any]] = []
    user = paths.user_config_path()
    if user.is_file():
        layers.append(("user", load_yaml(user) or {}))
    if repo_root is not None:
        rel = (default.get("paths") or {}).get("repo", ".auto-office/config.yaml")
        repo_cfg = Path(repo_root) / rel
        if repo_cfg.is_file():
            layers.append(("repo", load_yaml(repo_cfg) or {}))
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
    return effective, warnings


DRIFT_BLOCKS = ("quota", "roles")
FILE_BLOCKS_KEY = "_file_blocks_at_start"


def file_blocks(repo_root: Path | None) -> dict:
    """The raw quota/roles blocks of the user and repo config files, without
    defaults or --set: what a person edited, independent of the runtime."""
    default = load_yaml(default_config_path()) or {}
    files = {"user": paths.user_config_path()}
    if repo_root is not None:
        files["repo"] = Path(repo_root) / (default.get("paths") or {}).get("repo", ".auto-office/config.yaml")
    out: dict = {}
    for tier, path in files.items():
        try:
            data = load_yaml(path) if path.is_file() else None
        except (OSError, yaml.YAMLError):
            data = None
        data = data if isinstance(data, dict) else {}
        out[tier] = {b: data[b] for b in DRIFT_BLOCKS if b in data}
    return out


def _differs(raw: Any, pinned: Any) -> bool:
    """Whether any key in `raw` has a different value in `pinned`."""
    if isinstance(raw, dict):
        return any(_differs(v, pinned.get(k) if isinstance(pinned, dict) else None) for k, v in raw.items())
    return raw != pinned


def config_drift(run: dict) -> str | None:
    """A notice when the config files' quota or roles blocks differ from what
    `office start` saw; those edits do not reach a running run. Runs without a
    recorded baseline compare live resolved values to the pinned ones, so the
    notice there is qualified (a --set at start looks the same)."""
    pinned = run.get("policy") or {}
    root = run.get("repo_root")
    repo = Path(root) if root and Path(root).is_dir() else None
    short = run["id"][:8]
    reserve = (pinned.get("quota") or {}).get("reserve_percent", "-")
    try:
        live, _ = resolve(repo)
        recorded = pinned.get(FILE_BLOCKS_KEY)
        if recorded is not None:
            now = file_blocks(repo)
            differ = [b for b in DRIFT_BLOCKS
                      if any((now.get(t) or {}).get(b) != (recorded.get(t) or {}).get(b) for t in ("user", "repo"))]
            if not differ:
                return None
            detail = (f": quota.reserve_percent pinned {reserve}, live {(live.get('quota') or {}).get('reserve_percent', '-')}"
                      if "quota" in differ else "")
            return (f"config edited since run start; not applied to running run {short} "
                    f"({', '.join(differ)} differ{detail})")
    except (OSError, ValueError, yaml.YAMLError):
        return None
    # Only keys present in the files are compared; a key that exists only in the
    # shipped defaults can change between releases without being an edit.
    now = file_blocks(repo)
    raw = {b: deep_merge(now["user"].get(b) or {}, (now.get("repo") or {}).get(b) or {}, "repo", []) for b in DRIFT_BLOCKS}
    differ = [b for b in DRIFT_BLOCKS if _differs(raw[b], pinned.get(b))]
    if not differ:
        return None
    return (f"config differs from run {short}'s pinned values (edited since start, or --set at start); "
            f"not applied to this run ({', '.join(differ)} differ: quota.reserve_percent pinned {reserve}, "
            f"live {(live.get('quota') or {}).get('reserve_percent', '-')})")


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
