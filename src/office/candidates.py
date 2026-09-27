"""Runtime-built route requests.

In v3 the orchestrator assembled route requests by hand. Here the runtime builds
them from the pinned catalog, installed adapters, derived capability proofs and
a live quota probe, then hands them to office.routing.route unchanged.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import yaml

from office import adapters, paths, routing

KIND_FOR_ROLE = {
    "planner": "worker",
    "executor": "worker",
    "worker": "worker",
    "plan_reviewer": "reviewer",
    "code_reviewer": "reviewer",
    "integration_reviewer": "reviewer",
    "closeout_verifier": "reviewer",
    "visual_reviewer": "vision",
}
# Routing policy for a role that has no row of its own in config.
ROLE_POLICY_FALLBACK = {"integration_reviewer": "code_reviewer"}

_QUOTA_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTA_TTL = 60.0


def catalog_rows() -> list[dict]:
    data = yaml.safe_load((paths.resources_root() / "catalog" / "seed.yaml").read_text(encoding="utf-8")) or {}
    return list(data.get("models") or [])


def role_policy(config: dict, role: str) -> dict:
    roles = config.get("roles") or {}
    return roles.get(role) or roles.get(ROLE_POLICY_FALLBACK.get(role, ""), {}) or {}


def probe_quota(adapter: dict) -> dict:
    """Tightest remaining quota percent, or unknown. Never stores account data."""
    harness = adapter.get("id")
    cached = _QUOTA_CACHE.get(harness)
    if cached and time.time() - cached[0] < _QUOTA_TTL:
        return cached[1]
    command = (adapter.get("quota_probe") or {}).get("command")
    result = {"status": "unknown", "tightest_remaining_percent": None}
    shared = _shared_cache_get(harness)
    if shared is not None and not os.environ.get("OFFICE_QUOTA_FIXTURE") and os.environ.get("OFFICE_QUOTA_PROBE") != "off":
        _QUOTA_CACHE[harness] = (time.time(), shared)
        return shared
    fixed = os.environ.get("OFFICE_QUOTA_FIXTURE")
    if fixed:
        # Test/eval fixture: {"codex": 40, "claude": null} — never a live probe.
        value = json.loads(fixed).get(harness)
        result = {"status": "ok", "tightest_remaining_percent": float(value)} if value is not None else result
        _QUOTA_CACHE[harness] = (time.time(), result)
        return result
    if os.environ.get("OFFICE_QUOTA_PROBE") == "off":
        command = None
    if command:
        argv = [str(paths.resources_root() / c) if c.startswith("scripts/") else c for c in command]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                remaining = data.get("tightest_remaining_percent")
                if remaining is not None:
                    result = {"status": "ok", "tightest_remaining_percent": float(remaining)}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        if result["status"] == "ok":
            _shared_cache_put(harness, result)
    _QUOTA_CACHE[harness] = (time.time(), result)
    return result


# Quota moves slowly relative to a dispatch; one probe result serves every
# short-lived office process for a couple of minutes (some probes take ~20s).
_SHARED_TTL = 120.0


def _shared_cache_path() -> Path:
    return paths.state_home() / "quota-cache.json"


def _shared_cache_get(harness: str) -> dict | None:
    try:
        entry = json.loads(_shared_cache_path().read_text()).get(harness)
    except (OSError, ValueError):
        return None
    if entry and time.time() - float(entry.get("at", 0)) < _SHARED_TTL:
        return entry["result"]
    return None


def _shared_cache_put(harness: str, result: dict) -> None:
    path = _shared_cache_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[harness] = {"at": time.time(), "result": result}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


def vision_proven(con: sqlite3.Connection, candidate: dict, adapter: dict) -> bool:
    from office import conformance
    return conformance.proof_status(con, candidate, adapter, "vision") == "pass"


_FAMILY_VERSION = re.compile(r"^([a-z]+)-(\d+(?:\.\d+)*)")


def below_family_floor(model_id: str | None, floors: dict | None) -> str | None:
    """The floor a model_id like gemini-3.6-flash falls under, else None."""
    match = _FAMILY_VERSION.match(model_id or "")
    if not match or not floors or match.group(1) not in floors:
        return None
    floor = str(floors[match.group(1)])
    version = tuple(int(p) for p in match.group(2).split("."))
    if version < tuple(int(p) for p in floor.split(".")):
        return floor
    return None


def build_candidates(con: sqlite3.Connection, role: str, *, probe: bool = True,
                     family_floors: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Return (candidates, skipped). `skipped` explains unavailable harnesses
    and rows under a model_family_floors entry."""
    kind = KIND_FOR_ROLE.get(role, "worker")
    all_adapters = adapters.load_all()
    candidates, skipped = [], []
    for row in catalog_rows():
        harness = row.get("invocation_harness")
        adapter = all_adapters.get(harness)
        label = f"{harness}/{row.get('model_id')}@{row.get('effort')}"
        if row.get("dispatchable") is False:
            continue
        floor = below_family_floor(row.get("model_id"), family_floors)
        if floor:
            skipped.append({"candidate": label, "reason": f"below model family floor {floor}"})
            continue
        if not adapter or not adapters.profile(adapter, kind):
            skipped.append({"candidate": label, "reason": f"no {kind} launch profile"})
            continue
        if not adapters.installed(adapter):
            skipped.append({"candidate": label, "reason": f"{harness} not installed"})
            continue
        version = adapters.harness_version(adapter) or "unknown"
        cand = {
            "harness": harness,
            "harness_version": version,
            "model_id": row.get("model_id"),
            "invocation_model_id": row.get("invocation_model_id") or row.get("model_id"),
            "invocation_source": row.get("invocation_source"),
            "effort": row.get("effort"),
            "benchmark_indexes": row.get("benchmark_indexes") or {},
            "capabilities": sorted(set(adapter.get("capabilities") or [])),
            "adapter_id": adapter.get("id"),
            "adapter_hash": adapters.adapter_hash(adapter),
            "cost": _cost(row),
        }
        if kind == "vision" and vision_proven(con, cand, adapter):
            cand["capabilities"] = sorted(set(cand["capabilities"]) | {"vision"})
        cand["quota"] = {"status": "unknown", "tightest_remaining_percent": None}
        candidates.append(cand)
    if probe and candidates:
        # One probe per harness that has a candidate, all at once.
        from concurrent.futures import ThreadPoolExecutor
        needed = sorted({c["adapter_id"] for c in candidates})
        with ThreadPoolExecutor(max_workers=len(needed)) as pool:
            quotas = dict(zip(needed, pool.map(lambda h: probe_quota(all_adapters[h]), needed)))
        for c in candidates:
            c["quota"] = quotas[c["adapter_id"]]
    return candidates, skipped


def _cost(row: dict) -> dict:
    price = row.get("price_fields") or {}
    out = price.get("output_per_mtok")
    return {"money_estimate": float(out) if isinstance(out, (int, float)) else None}


def parse_route_override(text: str) -> dict:
    """'harness/model@effort' (any part optional except model)."""
    harness = None
    rest = text
    if "/" in text:
        harness, rest = text.split("/", 1)
    model, _, effort = rest.partition("@")
    return {"harness": harness or None, "model_id": model, "effort": effort or None}


def route_role(con: sqlite3.Connection, config: dict, run: dict, role: str, *,
               task_id: str | None = None, override: str | None = None,
               exclude: set[str] | None = None, probe: bool = True) -> dict:
    """Build the request and route. Returns the routing result plus request."""
    policy_cfg = role_policy(config, role)
    # An explicit --route names its model, so it is not held to the family floor.
    floors = None if override else config.get("model_family_floors")
    candidates, skipped = build_candidates(con, role, probe=probe, family_floors=floors)
    if exclude:
        # Entries: an exact triple, "model:<harness>/<model>" (every effort of a
        # model that misbehaved), or "harness:<name>" (a shared quota/auth wall).
        def excluded(c):
            return (routing.candidate_id(c) in exclude or f"harness:{c['harness']}" in exclude
                    or f"model:{c['harness']}/{c['invocation_model_id']}" in exclude)
        candidates = [c for c in candidates if not excluded(c)]
    if override:
        want = parse_route_override(override)
        candidates = [c for c in candidates
                      if (c["model_id"] == want["model_id"] or c["invocation_model_id"] == want["model_id"])
                      and (not want["harness"] or c["harness"] == want["harness"])
                      and (not want["effort"] or c["effort"] == want["effort"])]
    gear = (config.get("gear_presets") or {}).get(run.get("gear") or "", {})
    cost_policy = (config.get("cost_policy") or {}).get("default", "balanced")
    policy = {
        "required_capabilities": list(policy_cfg.get("required_capabilities") or []),
        "floor": policy_cfg.get("floor"),
        "quota_reserve_percent": float((config.get("quota") or {}).get("reserve_percent", routing.DEFAULT_RESERVE_PERCENT)),
        "balanced_money_band_percent": float((config.get("cost_policy") or {}).get(
            "balanced_money_band_percent", routing.DEFAULT_MONEY_BAND_PERCENT)),
        "cost_policy": cost_policy,
    }
    request = {
        "role": role,
        "playbook": run.get("playbook"),
        "policy": policy,
        "preferred_seed": None if override else policy_cfg.get("preferred_seed"),
        "cost_policy": cost_policy,
        "allow_advisory_undercut": bool(gear.get("allow_advisory_undercut", True)),
        "runs_db": str(paths.runs_db()),
        "run_id": run.get("id"),
        "family_id": run.get("family_id"),
        "task_id": task_id,
        "candidates": candidates,
    }
    result = routing.route(request)
    result["skipped"] = skipped
    result["request"] = request
    return result
