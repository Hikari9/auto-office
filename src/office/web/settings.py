"""Settings view: Machine -> Repository -> Run, with where each value comes from.

Tiers are the shipped default, the machine (user) config file, the repository
config file and the policy a run pinned at `office start`. Machine and
repository values are edited through `office config --user|--repo`; a run's
pinned value is never edited. `apply` says when a change takes effect, from
where the runtime reads the key.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from office import config as cfg
from office import configcmd

TIERS = ("default", "machine", "repository", "run-pinned")
EDITABLE = {"machine": "--user", "repository": "--repo"}
# Prefix -> when a change applies. The longest matching prefix wins.
APPLY = {
    "scheduler": "immediate",           # `office queue` and the web queue loop read it on every pass
    "intake": "immediate",              # read when the queue loop admits an item
    "economics": "immediate",           # read by each `office economics ingest`
    "quota": "before-dispatch",         # re-read and drift-checked at each dispatch (config.config_drift)
    "roles": "before-dispatch",
    "paths": "restart",                 # runs.db and homes are resolved once per process
    "paths.repo": "future-runs",
}
DEFAULT_APPLY = "future-runs"           # pinned into runs.policy_json at office start
MISSING = object()


def apply_of(key: str) -> str:
    best, length = DEFAULT_APPLY, -1
    for prefix, when in APPLY.items():
        if (key == prefix or key.startswith(prefix + ".")) and len(prefix) > length:
            best, length = when, len(prefix)
    return best


def _flat(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in configcmd._leaves(data) if k}


def _read(path: Path | None) -> dict:
    return configcmd._load_quiet(path) if path is not None else {}


def entries(*, repo_root: Path | None = None, pinned: dict | None = None) -> list[dict]:
    """One entry per configurable leaf, resolved down to the narrowest tier given."""
    default = cfg.load_yaml(cfg.default_config_path()) or {}
    default = {k: v for k, v in default.items() if k not in cfg.NON_CONFIGURABLE_KEYS}
    layers = {
        "default": _flat(default),
        "machine": _flat(_read(cfg.config_paths(None)["user"])),
        "repository": _flat(_read(cfg.config_paths(repo_root)["repo"])) if repo_root else {},
        "run-pinned": _flat({k: v for k, v in (pinned or {}).items() if k not in cfg.NON_CONFIGURABLE_KEYS}),
    }
    keys = sorted(set().union(*(layers[t] for t in TIERS)))
    editable = ["machine"] + (["repository"] if repo_root else [])
    out = []
    for key in keys:
        values = {t: layers[t].get(key, MISSING) for t in TIERS}
        source = next((t for t in reversed(TIERS) if values[t] is not MISSING), "default")
        out.append({
            "key": key,
            "value": values[source] if values[source] is not MISSING else None,
            "source": source,
            "values": {t: (None if v is MISSING else v) for t, v in values.items()},
            "set_in": [t for t in TIERS if values[t] is not MISSING],
            "inherited": source == "default",
            "overridden": source != "default" and values["default"] is not MISSING
                          and values[source] != values["default"],
            "editable": editable,
            "apply": apply_of(key),
        })
    return out


def view(*, scope: str, repo_root: Path | None = None, pinned: dict | None = None, **ids) -> dict:
    return {"scope": scope, **ids, "tiers": list(TIERS), "entries": entries(repo_root=repo_root, pinned=pinned)}


def config_args(kind: str, tier: str, key: str, value: Any = None) -> list[str]:
    """`office config` arguments for settings_set / settings_unset."""
    if tier not in EDITABLE:
        raise ValueError(f"tier {tier!r} is not editable")
    if kind == "settings_unset":
        return ["config", EDITABLE[tier], "--unset", "--", key]
    # `--` ends options: a value such as "--repo" or "--force" stays a value.
    return ["config", EDITABLE[tier], "--", key, value if isinstance(value, str) else _yaml_scalar(value)]


def _yaml_scalar(value: Any) -> str:
    import yaml
    return yaml.safe_dump(value, default_flow_style=True).strip().removesuffix("\n...").strip()
