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
    return resolve_aliases(list(data.get("models") or []))


_ALIAS_FIELDS = ("benchmark_indexes", "price_fields", "speed_fields", "release_date")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def resolve_aliases(rows: list[dict]) -> list[dict]:
    """A row with `alias_family` (a regex with a `version` group over model_id) takes
    the invocation and scores of the highest-version matching row at the same harness
    and effort, so `opus` follows each new Opus as the catalog adds it. With no
    match, the alias keeps its own static fields. A target without a proven
    invocation_source inherits the alias's."""
    # Non-dispatchable rows count: they are usually just unlistable by their CLI, and the
    # alias's own invocation_source stands behind the full model id as the slug.
    concrete = [r for r in rows if not r.get("alias_family")]
    out = []
    for row in rows:
        pattern = row.get("alias_family")
        if not pattern:
            out.append(row)
            continue
        best = None
        for r in concrete:
            m = re.match(pattern, r.get("model_id") or "")
            if not m or r.get("invocation_harness") != row.get("invocation_harness") or r.get("effort") != row.get("effort"):
                continue
            key = _version_key(m.group("version"))
            if best is None or key > best[0]:
                best = (key, r)
        if best is None:
            out.append(row)
            continue
        target = best[1]
        resolved = dict(row)
        resolved["invocation_model_id"] = target.get("invocation_model_id") or target["model_id"]
        source = str(target.get("invocation_source") or "")
        if source.startswith(("local-evidence:", "documented:")):
            resolved["invocation_source"] = source
        for field in _ALIAS_FIELDS:
            if target.get(field):
                resolved[field] = target[field]
        resolved["alias_resolved_to"] = target["model_id"]
        out.append(resolved)
    return out


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


def vision_note(con: sqlite3.Connection, candidate: dict) -> str:
    """Why a visual route has no `vision` capability yet. A proof binds to the
    exact harness version and adapter, so an upgrade leaves it unproven (#211)."""
    row = con.execute("SELECT harness_version, result, proved_at FROM capability_proofs WHERE capability='vision' "
                      "AND harness=? AND model=? AND effort IS ? ORDER BY proved_at DESC LIMIT 1",
                      (candidate.get("harness"), candidate.get("invocation_model_id"), candidate.get("effort"))).fetchone()
    how = "the first visual gate probes it, or office doctor --probe-vision"
    if row is None:
        return f"vision never probed on this route; {how}"
    return (f"vision unproven for {candidate.get('harness')} {candidate.get('harness_version')} "
            f"(last {row['result']} on {row['harness_version']}, {row['proved_at'][:10]}); {how}")


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


# Aliases that name a family without its vendor prefix.
_FAMILY_ALIASES = {"opus": "claude", "sonnet": "claude", "haiku": "claude", "fable": "claude",
                   "luna": "gpt", "astra": "gpt", "sol": "gpt", "terra": "gpt"}


def model_family(model_id: str | None) -> str | None:
    """The vendor family a model belongs to (claude, gpt, gemini, ...): the
    independence unit for review. `claude-sonnet-5-5` and `sonnet` are both claude."""
    head = (model_id or "").lower().split("-", 1)[0]
    return _FAMILY_ALIASES.get(head, head) or None


def declared_candidate(harness: str, model: str, effort: str | None = None) -> dict:
    """A candidate the user named with --as/--review-as. It bypasses the
    registry, trust, and floors, but still resolves through the catalog when a
    row matches, so `agy/gemini-3.8-flash@medium` invokes the combined slug
    `gemini-3.8-flash-medium` the harness actually accepts."""
    from office.state import Usage
    adapter = adapters.load_all().get(harness)
    if adapter is None:
        raise Usage("unknown-harness", f"--as names harness {harness!r}, which has no adapter",
                    next_step="use one of: " + ", ".join(sorted(adapters.load_all())))
    rows = [r for r in catalog_rows() if r.get("invocation_harness") == harness
            and model in (r.get("model_id"), r.get("invocation_model_id"))]
    if effort:
        rows = [r for r in rows if r.get("effort") == effort] or rows
    # A row named exactly what the user typed beats an alias that invokes it.
    rows.sort(key=lambda r: r.get("model_id") != model)
    row = rows[0] if len(rows) == 1 or (rows and effort) else None
    return {
        "harness": harness,
        "harness_version": adapters.harness_version(adapter) or "unknown",
        "model_id": (row or {}).get("model_id") or model,
        "invocation_model_id": (row or {}).get("invocation_model_id") or model,
        "invocation_source": "user-override",
        "effort": effort or (row or {}).get("effort") or "none",
        "benchmark_indexes": (row or {}).get("benchmark_indexes") or {},
        "capabilities": sorted(set(adapter.get("capabilities") or [])),
        "adapter_id": adapter.get("id"),
        "adapter_hash": adapters.adapter_hash(adapter),
        "cost": _cost(row or {}),
        "quota": {"status": "unknown", "tightest_remaining_percent": None},
        "override": True,
    }


def declared_decision(text: str, *, flag: str = "--as") -> dict:
    """A routing decision for a user-declared `harness/model[@effort]`."""
    from office.state import Usage
    want = parse_route_override(text)
    if not want["harness"] or not want["model_id"]:
        raise Usage("invalid-override", f"{flag} {text!r}: expected <harness>/<model>[@effort]")
    cand = declared_candidate(want["harness"], want["model_id"], want["effort"])
    triple = routing.candidate_id(cand)
    return {"status": "selected", "selected": triple, "candidate": cand, "override": True,
            "selection_disclosure": {"triple": triple, "reason": f"user override ({flag} {text})", "override": True},
            "skipped": []}


def _preferred_seed(policy_cfg: dict, run: dict):
    """roles.<role>.preferred_seed_by_size.<size_class> replaces preferred_seed when the
    run's size class (start --size-class) has an entry there."""
    size = (run.get("risk") or {}).get("size_class")
    by_size = policy_cfg.get("preferred_seed_by_size") or {}
    return by_size.get(size) or policy_cfg.get("preferred_seed")


def protected_quota_remedy(run: dict, role: str, task_id: str | None = None) -> str:
    """What to do when routing `role` stopped on the protected quota reserve.
    Only a role with a user route override gets a command: the executor
    (dispatch --as), task code review (dispatch --review-as), and a task's
    visual review (approve visual). Plan and integration review have none.
    The reserve is pinned at `office start`, so a config edit does not apply."""
    reserve = float(((run.get("policy") or {}).get("quota") or {}).get("reserve_percent", routing.DEFAULT_RESERVE_PERCENT))
    pinned = (f"quota.reserve_percent is pinned at {reserve:g}% for this run (set at office start), "
              "config edits do not apply to it")
    route = "<harness>/<model>[@effort]"
    if role == "executor" and task_id:
        fix = f"office dispatch {task_id} --as {route}"
    elif role == "code_reviewer" and task_id:
        fix = f"office dispatch {task_id} --review-as {route}"
    elif role == "visual_reviewer" and task_id:
        fix = (f"a user may record a visual review they ran: office approve visual {task_id} --by {route} "
               "--report <review file> --quote \"<user's words>\"")
    else:
        return f"wait for quota, choose a cheaper strategy, or obtain explicit user authority; {pinned}"
    return f"fix: {fix}; {pinned}"


def route_role(con: sqlite3.Connection, config: dict, run: dict, role: str, *,
               task_id: str | None = None, override: str | None = None,
               exclude: set[str] | None = None, probe: bool = True, exact: str | None = None) -> dict:
    """Build the request and route. Returns the routing result plus request.
    `exact` keeps only the candidate with that route identity (harness@major/model@effort)."""
    policy_cfg = role_policy(config, role)
    # An explicit --route names its model, so it is not held to the family floor.
    floors = None if override else config.get("model_family_floors")
    candidates, skipped = build_candidates(con, role, probe=probe, family_floors=floors)
    from office import benchmarks
    snapshot = benchmarks.apply(run, candidates)
    if exclude:
        # Entries: an exact triple, "model:<harness>/<model>" (every effort of a
        # model that misbehaved), or "harness:<name>" (a shared quota/auth wall).
        def excluded(c):
            return (routing.candidate_id(c) in exclude or f"harness:{c['harness']}" in exclude
                    or f"model:{c['harness']}/{c['invocation_model_id']}" in exclude)
        candidates = [c for c in candidates if not excluded(c)]
    if exact:
        candidates = [c for c in candidates if routing.candidate_id(c) == exact]
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
        "preferred_seed": None if override else _preferred_seed(policy_cfg, run),
        "cost_policy": cost_policy,
        "allow_advisory_undercut": bool(gear.get("allow_advisory_undercut", True)),
        "runs_db": str(paths.runs_db()),
        "run_id": run.get("id"),
        "family_id": run.get("family_id"),
        "task_id": task_id,
        "benchmark_snapshot": snapshot,
        "candidates": candidates,
    }
    result = routing.route(request)
    result["benchmark_snapshot"] = snapshot
    result["skipped"] = skipped
    result["request"] = request
    return result


def trust_report(con: sqlite3.Connection) -> list[str]:
    """One line per candidate route of each trust-gated role, with its derived trust
    state; non-proven routes carry the exact command a user runs to promote them."""
    from office import routing, scoring
    scoring.ensure_trust_schema(con)
    lines = []
    for role in sorted(routing.MUTABLE_TRUST_ROLES):
        cands, _ = build_candidates(con, role, probe=False)
        if not cands:
            lines.append(f"trust {role}: no candidate routes")
            continue
        for c in cands:
            triple = routing.candidate_id(c)
            _, state = scoring.evaluate_trust_state(con, triple)
            line = f"trust {role} {triple}: {state}"
            if state != "proven":
                line += f" | office approve trust {triple} --quote \"<user's words>\""
            if KIND_FOR_ROLE.get(role) == "vision" and "vision" not in c["capabilities"]:
                line += f" | {vision_note(con, c)}"
            lines.append(line)
    return lines
