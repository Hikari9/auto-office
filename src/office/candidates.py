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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from office import adapters, executor_slates, model_lineages, paths, route_policy, routing, state, task_sizing

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
    from office import user_catalog
    return model_lineages.annotate(resolve_aliases(user_catalog.apply(list(data.get("models") or []))))


_ALIAS_FIELDS = ("benchmark_indexes", "task_benchmarks", "price_fields", "speed_fields", "release_date",
                 "free_model", "max_concurrent")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def _legacy_custom_alias(row: dict, own: dict, concrete: list[dict]) -> list[dict]:
    """Compatibility for explicit user/test regex aliases absent from shipped lineage map.

    This does not classify or retire *canonical* named model generations. A later
    catalog migration can require their identity/provenance before publication.
    """
    pattern = row.get("alias_family")
    best = None
    for route in concrete:
        match = re.match(pattern, route.get("model_id") or "")
        if not match or route.get("invocation_harness") != row.get("invocation_harness"):
            continue
        key = _version_key(match.group("version"))
        if best is None or key > best[0]:
            best = (key, route["model_id"])
    by_effort = {r.get("effort"): r for r in concrete
                 if best and r.get("model_id") == best[1] and
                    r.get("invocation_harness") == row.get("invocation_harness")}
    out = []
    for effort in dict.fromkeys([*own, *by_effort]):
        base = own.get(effort) or {**row, "effort": effort, "effort_confidence": "mapped"}
        target = by_effort.get(effort)
        if target is None:
            out.append(base)
            continue
        resolved = dict(base)
        if target.get("dispatchable") is False or base.get("dispatchable") is False:
            resolved["dispatchable"] = False
            if base.get("dispatchable") is not False:
                for field in ("discovery", "discovery_reason"):
                    if target.get(field):
                        resolved[field] = target[field]
            else:
                resolved.pop("discovery", None)
            resolved["status_reason"] = route_policy.alias_status(resolved, target)["reason"]
        resolved["invocation_model_id"] = target.get("invocation_model_id") or target["model_id"]
        source = str(target.get("invocation_source") or "")
        if source.startswith(("local-evidence:", "documented:")):
            resolved["invocation_source"] = source
        for field in _ALIAS_FIELDS:
            if target.get(field):
                resolved[field] = target[field]
        resolved["source_effort"] = target.get("source_effort") or resolved.get("source_effort")
        resolved["alias_resolved_to"] = target["model_id"]
        out.append(resolved)
    return out


def resolve_aliases(rows: list[dict], *, manifest: dict[str, dict] | None = None,
                    aliases: dict[str, str] | None = None) -> list[dict]:
    """Bind known aliases to concrete *explicitly released* lineage generations.

    Existing alias regex is retained as seed metadata, not as authority to infer
    an unverified vendor release. Aliases never resurrect an archived third
    generation or leap to an ambiguous preview. Per-effort technical gates and
    invocation provenance are copied from the exact concrete route.
    """
    manifest = model_lineages.load() if manifest is None else manifest
    aliases = model_lineages.alias_lineages() if aliases is None else aliases
    concrete = [r for r in rows if not r.get("alias_family")]
    out = []
    expanded: set[tuple] = set()
    # Keep exactly the newest two released identities per named lineage.
    window = model_lineages.partition(concrete, manifest=manifest)["active"]
    for row in rows:
        if not row.get("alias_family"):
            out.append(row)
            continue
        lineage = aliases.get(row.get("model_id"))
        group = (row.get("model_id"), row.get("invocation_harness"), row.get("alias_family"))
        if group in expanded:
            continue
        expanded.add(group)
        own = {r.get("effort"): r for r in rows
               if (r.get("model_id"), r.get("invocation_harness"), r.get("alias_family")) == group}
        if not lineage:
            out.extend(_legacy_custom_alias(row, own, concrete))
            continue
        eligible = [r for r in window if r.get("lineage_key") == lineage and
                    r.get("invocation_harness") == row.get("invocation_harness")]
        if not eligible:
            out.extend(own.values())
            continue
        latest = max(model_lineages.version_parts(r["generation_version"]) for r in eligible)
        current = {r.get("effort"): r for r in eligible
                   if model_lineages.version_parts(r["generation_version"]) == latest}
        for effort in dict.fromkeys([*own, *current]):
            original = own.get(effort) or row
            target = current.get(effort)
            if target is None:
                # Preserve a previously declared effort as-is; never fabricate
                # a newer model's missing effort or bypass its conformance gate.
                if effort in own:
                    out.append(original)
                continue
            resolved = dict(original)
            for key in _ALIAS_FIELDS + ("invocation_model_id", "invocation_source", "source_identifier",
                                         "dispatchable", "discovery", "discovery_reason", "status_reason"):
                if key in target:
                    resolved[key] = target[key]
                else:
                    resolved.pop(key, None)
            # A user-disabled alias or a technically gated target must remain
            # gated. The alias cannot launder either denial into availability.
            if original.get("dispatchable") is False:
                resolved["dispatchable"] = False
                resolved.pop("discovery", None)
                resolved.pop("discovery_reason", None)
            elif target.get("dispatchable") is False:
                resolved["dispatchable"] = False
            if target.get("dispatchable") is False:
                resolved["status_reason"] = route_policy.alias_status(resolved, target)["reason"]
            source = str(target.get("invocation_source") or "")
            if not source.startswith(("local-evidence:", "documented:")):
                resolved["invocation_source"] = original.get("invocation_source")
            resolved["source_effort"] = target.get("source_effort") or resolved.get("source_effort")
            resolved["effort"] = effort
            resolved["alias_resolved_to"] = target["model_id"]
            out.append(resolved)
    return out


_POLICY_CACHE: dict = {}


_REPO_ROOTS: dict[str, Path] = {}


def _repo_root_here() -> Path | None:
    """The repository holding the cwd, remembered per directory so routing does not fork git each call."""
    try:
        here = os.getcwd()
    except OSError:
        return None
    if here not in _REPO_ROOTS:
        ident = paths.repo_identity(here)
        if ident is None:
            return None  # not remembered: a repository may be created here later
        _REPO_ROOTS[here] = ident[0]
    return _REPO_ROOTS[here]


def live_user_policies(repo_root=None) -> list[dict]:
    """The user's denied/overkill policy as the user and repo config files say now.

    A denial applies the moment the user writes it, even to a run pinned earlier, so
    it can never be bypassed by a stale pin. `repo_root` defaults to the cwd's
    repository. Returns [] when nothing is set (the shipped default). Raises
    OSError, ValueError or yaml.YAMLError when the config cannot be read, so a
    caller that must fail closed (the probe) can tell that from "no denials"."""
    from office import config as cfg
    repo_root = repo_root if repo_root is not None else _repo_root_here()
    # Read here, not through `cfg.read_files`: this feeds only the denial lookup and must not
    # add a second read to the snapshot `office start` pins and baselines its drift check on.
    files = {tier: (path.read_text(encoding="utf-8") if path.is_file() else None)
             for tier, path in cfg.config_paths(repo_root).items()}
    cache_key = (str(repo_root), tuple(sorted(files.items(), key=lambda kv: kv[0])))
    policy = _POLICY_CACHE.get(cache_key)
    if policy is None:  # held in a local: concurrent probes share this cache and may clear it
        effective, warnings = cfg.resolve(repo_root, files=files)
        ignored = [w for w in warnings if w["reason"] == "type-mismatch-ignored"
                   and (w["key"] in ("routing", "routing.user_policy") or w["key"].startswith("routing.user_policy."))]
        if ignored:  # a malformed routing value is dropped by the merge: it may have been a denial
            raise ValueError(f"{ignored[0]['tier']} config {ignored[0]['key']} has the wrong type "
                             f"(expected {ignored[0]['expected']}, got {ignored[0]['got']})")
        policy = route_policy.user_policy(effective)
        _POLICY_CACHE.clear()
        _POLICY_CACHE[cache_key] = policy
    return [policy] if policy["denied"] or policy["overkill"] or policy["ceilings"] or policy["role_rules"] or policy["strict_roles"] else []


def required_user_policies(repo_root=None) -> list[dict]:
    """`live_user_policies`, refusing when the config cannot be read: a route the
    unreadable policy might deny is never offered, declared or discovered."""
    try:
        return live_user_policies(repo_root)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise state.Refused("policy-unreadable", f"the user or repo routing policy cannot be read ({exc})",
                            next_step="fix the config (office config --user / --repo) and retry") from exc


def current_user_policies(repo_root=None) -> list[dict]:
    """`live_user_policies`, or [] when the config cannot be read: an unreadable
    config already stops `office` elsewhere."""
    try:
        return live_user_policies(repo_root)
    except (OSError, ValueError, yaml.YAMLError):
        return []


def role_policy(config: dict, role: str) -> dict:
    roles = config.get("roles") or {}
    return roles.get(role) or roles.get(ROLE_POLICY_FALLBACK.get(role, ""), {}) or {}


def probe_quota(adapter: dict) -> dict:
    """Tightest remaining quota percent, or unknown/unavailable with a safe cause.

    A successful result is cached per harness (`quota_cache_ttl`); a failed one is
    cached only briefly and stays unknown/unavailable, never ok (#450)."""
    harness = adapter.get("id")
    fixed = os.environ.get("OFFICE_QUOTA_FIXTURE")
    disabled = os.environ.get("OFFICE_QUOTA_PROBE") == "off"
    shared = _shared_cache_get(harness) if not fixed and not disabled else None
    if shared is not None:
        _QUOTA_CACHE[harness] = (time.time(), shared)
        return shared
    cached = _QUOTA_CACHE.get(harness)
    if not fixed and not disabled and cached and time.time() - cached[0] < _QUOTA_TTL:
        return cached[1]

    def unknown(cause: str) -> dict:
        return {"status": "unknown", "tightest_remaining_percent": None, "cause": cause}

    command = (adapter.get("quota_probe") or {}).get("command")
    result = unknown("no command configured")
    if fixed:
        # Test/eval fixture: {"codex": 40, "claude": null} — never a live probe.
        value = json.loads(fixed).get(harness)
        if isinstance(value, dict):
            result = _normalize_probe_result(value, unknown)
        else:
            result = ({"status": "ok", "tightest_remaining_percent": float(value)} if value is not None
                      else unknown("fixture returned null"))
        _QUOTA_CACHE[harness] = (time.time(), result)
        return result
    if disabled:
        result = unknown("probe disabled")
        _QUOTA_CACHE[harness] = (time.time(), result)
        return result
    if command:
        argv = [str(paths.resources_root() / c) if c.startswith("scripts/") else c for c in command]
        result = _run_probe(argv, unknown)
        _shared_cache_put(harness, result)
    _QUOTA_CACHE[harness] = (time.time(), result)
    return result


PROBE_TIMEOUT_S = 20
PROBE_RETRY_BACKOFF_S = 2.0


def _run_probe(argv: list[str], unknown) -> dict:
    """Run one probe command. A timeout while the host is overloaded is the
    environment, not the harness (the rule checks use, gates.host_overloaded):
    it is retried once after a backoff, and if it times out again the result is
    `unavailable` with the load as its cause. A timeout on a quiet host stays a
    plain unknown with no retry, since a retry would only cost another 20s."""
    from office import gates
    for attempt in (1, 2):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            loaded = gates.host_overloaded()
            if not loaded:
                return unknown(f"command timed out after {PROBE_TIMEOUT_S} seconds")
            if attempt == 1:
                time.sleep(PROBE_RETRY_BACKOFF_S)
                continue
            return {"status": "unavailable", "tightest_remaining_percent": None,
                    "cause": f"command timed out twice after {PROBE_TIMEOUT_S} seconds; {loaded}"}
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            return unknown("command could not be run or parsed")
        if proc.returncode != 0:
            return unknown(f"command exited with code {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return unknown("command returned invalid JSON")
        return _normalize_probe_result(data, unknown)
    return unknown("command could not be run or parsed")  # pragma: no cover


def _normalize_probe_result(data: dict, unknown) -> dict:
    if not isinstance(data, dict) or "tightest_remaining_percent" not in data:
        return unknown("command response missing tightest_remaining_percent")
    if data["tightest_remaining_percent"] is None:
        result = unknown(str(data.get("cause") or "command returned null tightest_remaining_percent"))
        if isinstance(data.get("families"), dict):  # a known family can still gate its own models
            result["families"] = {str(f): _normalize_family(str(f), raw) for f, raw in data["families"].items()}
        return result
    try:
        result = {"status": "ok", "tightest_remaining_percent": float(data["tightest_remaining_percent"])}
    except (TypeError, ValueError):
        return unknown("command could not be run or parsed")

    if isinstance(data.get("families"), dict):
        result["families"] = {str(f): _normalize_family(str(f), raw) for f, raw in data["families"].items()}
    return result


def _normalize_family(family: str, raw) -> dict:
    """One probe family result; anything but a readable ok percent is unknown."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        remaining = float(raw["tightest_remaining_percent"]) if raw.get("status") == "ok" else None
    except (KeyError, TypeError, ValueError):
        remaining = None
    if remaining is None:
        return {"status": "unknown", "tightest_remaining_percent": None,
                "reset_time": raw.get("reset_time"), "cause": f"{family} family quota not reported"}
    return {"status": "ok", "tightest_remaining_percent": remaining, "reset_time": raw.get("reset_time")}


# Quota moves slowly relative to a dispatch; one successful probe result serves every
# short-lived office process for a few minutes (some probes take ~20s). OFFICE_QUOTA_CACHE_TTL
# (seconds) overrides it. A failed probe is remembered only for a short while, so a harness
# whose probe always times out does not cost every launch 20s; it is never reported as ok.
_SHARED_TTL = 300.0
_FAILED_TTL = 60.0


def _shared_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("OFFICE_QUOTA_CACHE_TTL", _SHARED_TTL)))
    except ValueError:
        return _SHARED_TTL


def _shared_cache_path() -> Path:
    return paths.state_home() / "quota-cache.json"


def _shared_cache_get(harness: str) -> dict | None:
    try:
        entry = json.loads(_shared_cache_path().read_text()).get(harness)
    except (OSError, ValueError, AttributeError):
        return None
    if not entry or not isinstance(entry.get("result"), dict):
        return None
    ok = entry["result"].get("status") == "ok"
    ttl = _shared_ttl() if ok else min(_FAILED_TTL, _shared_ttl())
    if time.time() - float(entry.get("at", 0)) < ttl:
        return entry["result"]
    return None


def _shared_cache_put(harness: str, result: dict) -> None:
    """Read-modify-write under an flock so concurrent office processes do not
    drop each other's harness entries; the file itself is replaced atomically."""
    import fcntl
    path = _shared_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[harness] = {"at": time.time(), "result": result}
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
    except OSError:
        pass


_NOT_PROBED = (None, "no command configured", "probe disabled")


def quota_unknown_record(candidate: dict | None) -> dict | None:
    """Why a chosen route's quota was not read, or None when it was. Kept on the
    dispatch's route record and shown in `office status` (#450)."""
    quota = (candidate or {}).get("quota") or {}
    if quota.get("status") == "ok" and quota.get("tightest_remaining_percent") is not None:
        return None
    if quota.get("cause") in _NOT_PROBED:
        return None  # no probe ran: nothing failed, so nothing to flag on every live task
    return {"harness": (candidate or {}).get("adapter_id") or (candidate or {}).get("harness"),
            "status": quota.get("status") or "unknown", "cause": quota.get("cause") or "quota not probed"}


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


def _probe_view(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {"result": rec.get("result"), "reason_class": rec.get("reason_class"), "detail": rec.get("detail"),
            "probed_at": rec.get("probed_at"), "fresh": True, "attempt_id": rec.get("attempt_id")}


def build_candidates(con: sqlite3.Connection, role: str, *, probe: bool = True,
                     family_floors: dict | None = None,
                     quota_snapshot: dict[str, dict] | None = None,
                     discovery: dict | None = None,
                     user_policies: list[dict] | None = None,
                     allow_policies: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (candidates, skipped). Every active catalog row lands in exactly one of
    them: `skipped` names each row that is not a candidate, with a `category`
    (denied, not-eligible, untried, floor, not-installed, no-profile) and a reason.

    `discovery` is the run's pinned `routing.discovery` settings when discovery
    applies to this call (#494). Rows that are not dispatchable but are explicitly
    discovery-eligible then become candidates carrying `route_status` and, when
    `con` has one, the fresh exact probe record. They are never available routes;
    routing keeps them out of the eligible slate. `user_policies` (default: the
    user's current policy) removes denied routes; routing passes [] and rejects
    denied routes itself, with the policy source, at stage 1.

    `allow_policies` (default: `user_policies`) are the policies whose role allow makes a route
    dispatchable (#845): an allowed route is a candidate even when its catalog row, a user catalog
    overlay or the family floor says otherwise, and it needs no probe. Routing still applies deny,
    capability, quota and independence rules to it.
    """
    from office import onboarding, route_probe
    kind = KIND_FOR_ROLE.get(role, "worker")
    sources = adapters.load_sources()
    all_adapters = {aid: entry[0] for aid, entry in sources.items()}
    policies = current_user_policies() if user_policies is None else user_policies
    allow_pols = policies if allow_policies is None else allow_policies
    candidates, skipped, not_routable = [], [], []
    free_models_allowed = onboarding.free_models_enabled()
    for row in catalog_rows():
        harness = row.get("invocation_harness")
        adapter = all_adapters.get(harness)
        label = f"{harness}/{row.get('model_id')}@{row.get('effort')}"
        state = route_policy.row_status(row)
        is_free_model = route_policy.is_free_model(row)
        if is_free_model and not free_models_allowed:
            skipped.append({"candidate": label, "category": "free-model-opt-in",
                            "reason": "free model excluded because user opt-in is disabled"})
            continue
        if row.get("status_reason"):
            state = {**state, "reason": row["status_reason"]}
        floor = below_family_floor(row.get("model_id"), family_floors)
        allowed = None
        if (row.get("dispatchable") is False or floor) and allow_pols and adapter and adapters.installed(adapter):
            allowed = route_policy.user_allows(
                {"harness": harness, "harness_version": adapters.route_version(harness, adapter, sources),
                 "model_id": row.get("model_id"), "invocation_model_id": row.get("invocation_model_id") or row.get("model_id"),
                 "alias_resolved_to": row.get("alias_resolved_to"), "effort": row.get("effort")}, role, allow_pols)
        if allowed and row.get("dispatchable") is False:
            # The user's allow outranks the catalog or an overlay: no proof, qualification or trust gate (#845).
            why = "a user catalog overlay disabled it" if row.get("user_disabled") else (
                row.get("invocation_source") or "the catalog row is not dispatchable")
            state = {"status": "available", "discovery_eligible": False,
                     "reason": f"dispatchable by role {role} allow ({allowed['source']}: {allowed.get('rule')}); {why}"}
        elif row.get("dispatchable") is False and not (discovery and state["discovery_eligible"]):
            if state["discovery_eligible"]:
                not_routable.append({"candidate": label, "category": "untried",
                                     "reason": "discovery-eligible but discovery is off for this role or run: "
                                               + state["reason"]})
            else:
                not_routable.append({"candidate": label, "category": "not-eligible",
                                     "reason": f"not a candidate: {state['reason']}"})
            continue
        if floor and not allowed:
            skipped.append({"candidate": label, "category": "floor", "reason": f"below model family floor {floor}"})
            continue
        if not adapter or not adapters.profile(adapter, kind):
            skipped.append({"candidate": label, "category": "no-profile", "reason": f"no {kind} launch profile"})
            continue
        if not adapters.installed(adapter):
            skipped.append({"candidate": label, "category": "not-installed", "reason": f"{harness} not installed"})
            continue
        version = adapters.route_version(harness, adapter, sources)
        cand = {
            "harness": harness,
            "harness_version": version,
            "model_id": row.get("model_id"),
            "lineage_key": row.get("lineage_key"),
            "generation_version": row.get("generation_version"),
            "invocation_model_id": row.get("invocation_model_id") or row.get("model_id"),
            "invocation_source": row.get("invocation_source"),
            "effort": row.get("effort"),
            "benchmark_indexes": row.get("benchmark_indexes") or {},
            "task_benchmarks": row.get("task_benchmarks") or [],
            "capabilities": sorted(set(adapter.get("capabilities") or [])),
            "adapter_id": adapter.get("id"),
            "adapter_hash": adapters.adapter_hash(adapter),
            "cost": _cost(row),
            "price_fields": row.get("price_fields") or {},
            "speed_fields": row.get("speed_fields") or {},
            "route_status": state["status"], "status_reason": state["reason"],
            "free_model": is_free_model,
            "max_concurrent": _max_concurrent(row, adapter, is_free_model),
            "discovery": state["discovery_eligible"], "probe_key": None,
        }
        if allowed:
            cand["user_allowed"] = True
        if row.get("alias_resolved_to"):
            cand["alias_resolved_to"] = row["alias_resolved_to"]
        # Role-aware (#837): a role allow outranks a legacy global deny, a role deny stays role-scoped.
        role_rule = route_policy.effective_role_state(cand, role, policies) if policies else None
        if role_rule and role_rule["state"] == "deny":
            skipped.append({"candidate": label, "category": "denied",
                            "reason": role_rule.get("reason") or f"{role} is denied by {role_rule['source']}: {role_rule.get('rule')}"})
            continue
        if state["discovery_eligible"]:
            cand["probe_key"] = route_probe.key(cand, adapter)
            fresh = route_probe.status(con, cand, adapter=adapter, ttl_days=discovery["probe_ttl_days"],
                                       timeout_s=discovery["probe_timeout_s"]) if con is not None else None
            cand["probe"] = _probe_view(fresh)
            refined = route_policy.row_status(row, fresh)
            cand["route_status"], cand["status_reason"] = refined["status"], refined["reason"]
        if kind == "vision" and vision_proven(con, cand, adapter):
            cand["capabilities"] = sorted(set(cand["capabilities"]) | {"vision"})
        cand["quota"] = {"status": "unknown", "tightest_remaining_percent": None}
        candidates.append(cand)
    skipped.extend(not_routable)
    if quota_snapshot is not None:
        for c in candidates:
            c["quota"] = quota_for_candidate(c, quota_snapshot.get(c["adapter_id"], c["quota"]))
    elif probe and candidates:
        # One probe per harness that has a candidate, all at once.
        needed = sorted({c["adapter_id"] for c in candidates})
        with ThreadPoolExecutor(max_workers=len(needed)) as pool:
            quotas = dict(zip(needed, pool.map(lambda h: probe_quota(all_adapters[h]), needed)))
        for c in candidates:
            c["quota"] = quota_for_candidate(c, quotas[c["adapter_id"]])
    return candidates, skipped


def probe_quota_snapshot(con: sqlite3.Connection, roles: list[str], *, family_floors: dict | None = None) -> dict[str, dict]:
    """Probe each adapter represented in the plan's routing roles once."""
    needed = set()
    for role in roles:
        built, _ = build_candidates(con, role, probe=False, family_floors=family_floors)
        needed.update(c["adapter_id"] for c in built)
    all_adapters = adapters.load_all()
    needed = sorted(needed)
    if not needed:
        return {}
    with ThreadPoolExecutor(max_workers=len(needed)) as pool:
        return dict(zip(needed, pool.map(lambda h: probe_quota(all_adapters[h]), needed)))


def _cost(row: dict) -> dict:
    price = row.get("price_fields") or {}
    out = price.get("output_per_mtok")
    return {"money_estimate": float(out) if isinstance(out, (int, float)) else None}


def _max_concurrent(row: dict, adapter: dict, is_free_model: bool) -> int | None:
    """Resolve a catalog row cap before the adapter default for free models."""
    cap = row.get("max_concurrent")
    return cap if cap is not None else (adapter.get("free_model_max_concurrent") if is_free_model else None)


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


# agy's "Claude and GPT models" group shares one allowance.
_AGY_QUOTA_FAMILY = {"gpt": "claude"}


def quota_for_candidate(candidate: dict, quota: dict | None = None) -> dict:
    """Return an Agy candidate's model-family quota, or the harness-wide result.

    Probes without a ``families`` mapping keep the pre-family contract and apply
    to every candidate on the harness.
    """
    result = quota if quota is not None else candidate.get("quota") or {}
    if candidate.get("adapter_id") != "agy" and candidate.get("harness") != "agy":
        return result
    families = result.get("families") if isinstance(result, dict) else None
    family = model_family(candidate.get("model_id"))
    family = _AGY_QUOTA_FAMILY.get(family, family)
    family_result = families.get(family) if isinstance(families, dict) else None
    return family_result if isinstance(family_result, dict) else result


def declared_candidate(harness: str, model: str, effort: str | None = None, *, repo_root=None, role: str = "executor",
                       run_id: str | None = None, scope: str | None = None, claim: bool = False) -> dict:
    """A candidate the user named with --as/--review-as. It bypasses the
    registry, trust, and floors, but still resolves through the catalog when a
    row matches, so `agy/gemini-3.8-flash@medium` invokes the combined slug
    `gemini-3.8-flash-medium` the harness actually accepts."""
    from office import onboarding
    from office.state import Refused, Usage
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
    # Only a route the catalog does not know at all can be judged by its slug; any catalog
    # match, even an ambiguous one, keeps the catalog's answer.
    named_free = not rows and route_policy.is_explicit_free_slug(harness, model)
    # An ambiguous match (several rows, no effort) is free if any matching row is, so an
    # omitted effort cannot step around the opt-in.
    free_model = (route_policy.is_free_model(row) if row is not None
                  else any(route_policy.is_free_model(r) for r in rows)) or named_free
    if free_model and not onboarding.free_models_enabled():
        why = "names itself free" if named_free else "is marked free"
        raise Refused("free-model-opt-in", f"{harness}/{model} {why} and requires explicit opt-in",
                      next_step="office onboard --enable-free-models")
    # A user-denied route is not selectable by any manual route either (--as, --review-as,
    # amend route). Overkill is automatic-only and never blocks a declared route (#494).
    named = {"harness": harness, "model_id": (row or {}).get("model_id") or model,
             "invocation_model_id": (row or {}).get("invocation_model_id") or model,
             "alias_resolved_to": (row or {}).get("alias_resolved_to"),
             "effort": effort or (row or {}).get("effort") or "none",
             "lineage_key": (row or {}).get("lineage_key"),
             "generation_version": (row or {}).get("generation_version")}
    active_policy = route_policy.effective_role_state(named, role, required_user_policies(repo_root))
    policy_override = None
    if active_policy["state"] == "deny":
        from office import route_override
        receipt = route_override.find_valid(run_id, role, named)
        context = f"{role} {harness}/{named['model_id']}@{named['effort']}"
        # Validation only checks the receipt is free for this scope; a launch (claim=True) takes it atomically.
        if receipt and (route_override.record_use(receipt, context, scope=scope) if claim
                        else route_override.available_for(receipt, scope)):
            # A genuine, signed user receipt overrides policy only; the capability, sandbox and
            # independence checks the caller applies next are untouched.
            policy_override = {"receipt": receipt["id"], "overrides": active_policy}
        else:
            denied = active_policy.get("reason") or f"{role} is denied by {active_policy['source']}: {active_policy.get('rule')}"
            way_back = ("it stays denied until you change the policy: office config routing.user_policy.denied_models "
                        f"--user (or --repo) to remove {harness}/{model}"
                        if active_policy["source"] == "legacy-user-deny" else
                        f"it stays denied until you change the policy: office route neutral|allow {harness}/{model}@{named['effort']} --role {role}")
            raise state.Refused("route-denied", denied, next_step=(
                f"{way_back}. A quote or orchestrator pin cannot override a deny; the user can issue a one-off override "
                f"in their own terminal: office route override {harness}/{model}@{named['effort']} --role {role} "
                "--run <run> --reason \"<why>\""))
    return {
        "harness": harness,
        "harness_version": adapters.route_version(harness, adapter),
        "model_id": (row or {}).get("model_id") or model,
        "invocation_model_id": (row or {}).get("invocation_model_id") or model,
        "invocation_source": "user-override",
        "effort": effort or (row or {}).get("effort") or "none",
        "benchmark_indexes": (row or {}).get("benchmark_indexes") or {},
        "task_benchmarks": (row or {}).get("task_benchmarks") or [],
        "capabilities": sorted(set(adapter.get("capabilities") or [])),
        "adapter_id": adapter.get("id"),
        "adapter_hash": adapters.adapter_hash(adapter),
        "cost": _cost(row or {}),
        "price_fields": (row or {}).get("price_fields") or {},
        "speed_fields": (row or {}).get("speed_fields") or {},
        "free_model": free_model,
        "quota": {"status": "unknown", "tightest_remaining_percent": None},
        "override": True,
        **({"policy_override": policy_override} if policy_override else {}),
    }


def declared_decision(text: str, *, flag: str = "--as", role: str | None = None,
                      config: dict | None = None, repo_root=None, run_id: str | None = None,
                      scope: str | None = None, claim: bool = False) -> dict:
    """A routing decision for a user-declared `harness/model[@effort]`.

    A user override bypasses the registry, trust and floors, but not what the
    harness can do: with `role`, the declared route must satisfy that role's
    `required_capabilities` (the same gate routed candidates clear at selection),
    so a builder-only harness is never dispatched as a planner or reviewer."""
    from office.state import Refused, Usage
    want = parse_route_override(text)
    if not want["harness"] or not want["model_id"]:
        raise Usage("invalid-override", f"{flag} {text!r}: expected <harness>/<model>[@effort]")
    cand = declared_candidate(want["harness"], want["model_id"], want["effort"], repo_root=repo_root, role=role or "executor",
                          run_id=run_id, scope=scope, claim=claim)
    if role:
        required = set(role_policy(config or {}, role).get("required_capabilities") or [])
        missing = sorted(required - set(cand.get("capabilities") or []))
        if missing:
            raise Refused(
                "capability-missing",
                f"{flag} {text!r}: {want['harness']} declares capabilities {cand.get('capabilities') or []}, "
                f"which lacks {missing} that role {role} requires",
                scope=role,
                next_step=f"use a route whose harness declares {', '.join(missing)} for {role}",
            )
    triple = routing.candidate_id(cand)
    return {"status": "selected", "selected": triple, "candidate": cand, "override": True,
            "selection_disclosure": {"triple": triple, "reason": f"user override ({flag} {text})", "override": True},
            "skipped": []}


def _preferred_seed(policy_cfg: dict, run: dict, task_size: str | None = None,
                    role: str = "executor"):
    """A pinned explicit seed wins; otherwise nominate #809 ideal size routes.
    Qualification is independently enforced downstream at actual dispatch."""
    size = task_size or (run.get("risk") or {}).get("size_class")
    by_size = policy_cfg.get("preferred_seed_by_size") or {}
    if size in by_size:
        return by_size[size]
    if policy_cfg.get("preferred_seed"):
        return policy_cfg["preferred_seed"]
    if (role in routing.ADAPTIVE_ROLES and run.get("gear") in executor_slates.FLOORS
            and size in executor_slates.FLOORS):
        return executor_slates.ideal_seed(size)
    return None


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
        fix = f'office dispatch {task_id} --as {route} --quote "<user\'s words>"'
    elif role == "code_reviewer" and task_id:
        fix = f"office dispatch {task_id} --review-as {route}"
    elif role == "visual_reviewer" and task_id:
        fix = (f"a user may record a visual review they ran: office approve visual {task_id} --by {route} "
               "--report <review file> --quote \"<user's words>\"")
    else:
        return f"wait for quota, choose a cheaper strategy, or obtain explicit user authority; {pinned}"
    return f"fix: {fix}; {pinned}"


def _effective_task_descriptor(run: dict, task: dict | None) -> dict:
    """Route on a task's effective size, even for a partially migrated descriptor.

    Historical pinned-run descriptors remain untouched. Classification is
    side-effect-free for planning previews, reroutes and retries.
    """
    if not task:
        return {}
    d = task.get("descriptor") or {}
    if ((run.get("gates") or {}).get("gear_model_version") == "run-gear-1"
            and (not d.get("task_size") or not d.get("profile_version"))):
        return {**d, **task_sizing.classify({**task, "descriptor": d}, run_gear=run.get("gear"))}
    return d


def seeded_fallback_blockers(decision: dict) -> str:
    """Legacy compatibility hook: trust-only fallback blocks do not exist after #837.

    All current failures appear in the ordinary per-route `rejected` reasons.
    Never suggest a second trust approval ceremony.
    """
    return ""


def adaptive_inputs(con: sqlite3.Connection, config: dict, run: dict, role: str, candidates: list[dict], *,
                    task_id: str | None, dispatch_kind: str,
                    wave_load: dict | None = None, as_of: str | None = None, pending_explorations: int = 0) -> dict:
    """Learner evidence and reproducibility inputs for an executor/worker request (#300).

    Everything the adaptive scorer reads is computed here, so `routing.route`
    stays offline and a recorded request replays to the same decision."""
    from datetime import datetime, timezone
    from office import route_learning
    from office.util import sha256_obj
    s = config.get("routing") or {}
    adaptive_cfg = s.get("adaptive") or {}
    ceiling = None
    if route_policy.PROVENANCE_KEY in config:
        # A #494-era run: only a user-set ceiling is hard, and its source is disclosed. A run
        # pinned before provenance existed keeps its own ceiling (or the old default) untouched.
        ceiling = route_policy.budget_ceiling(config)
        adaptive_cfg = {**adaptive_cfg, "budget_ceiling_usd": ceiling["usd"]}
    task = state.get_task(con, run["id"], task_id) if task_id and run.get("id") else None
    desc = _effective_task_descriptor(run, task)
    task_size = desc.get("task_size") or (run.get("risk") or {}).get("size_class")
    context = {"role": role, "playbook": run.get("playbook"), "size_class": task_size,
               "gear": run.get("gear"), "dispatch_kind": dispatch_kind,
               "task_size_rationale": desc.get("task_size_rationale"),
               "dispatch_profile": desc.get("profile"),
               "dispatch_profile_version": desc.get("profile_version")}
    if desc:
        context["task_descriptor"] = desc
    as_of = as_of or datetime.now(timezone.utc).isoformat()
    route_learning.ensure_schema(con)
    # Executor and worker dispatches are the same kind of work; they pool.
    outcomes = route_learning.derive_outcomes(con)
    evidence = route_learning.evidence_for(outcomes, candidates, context, as_of=as_of,
                                           config=adaptive_cfg.get("learning"))
    explored = con.execute("SELECT COUNT(*) FROM route_audit WHERE run_id=? AND role=? AND explored=1",
                           (run.get("id"), role)).fetchone()[0]
    return {
        **({"budget_ceiling": ceiling} if ceiling else {}),
        "context": context, "evidence": evidence, "adaptive_config": adaptive_cfg,
        "learned_eligibility": route_learning.current_eligibility(con, role),
        "exploration_history": route_learning.recent_exploration(con, role),
        "run_explorations": explored + pending_explorations, "wave_load": dict(wave_load or {}),
        # No plan_version: an amendment must not redraw the close call of a task it does not touch (#816).
        "routing_seed": sha256_obj({"run": run.get("id"), "task": task_id, "role": role, "kind": dispatch_kind}),
    }


def discovery_inputs(con: sqlite3.Connection, run: dict, role: str, candidates: list[dict], settings: dict,
                     task: dict | None = None) -> dict:
    """The recorded, replayable inputs the discovery allocation reads (#494): caps by
    read-only count, task risk, and which routes are known-working (proven or
    learned-eligible) or quarantined. `routing.route` stays offline."""
    from office import route_learning, route_probe, scoring
    scoring.ensure_trust_schema(con)
    learned = route_learning.current_eligibility(con, role)
    known, quarantined = [], []
    for c in candidates:
        cid = routing.candidate_id(c)
        # Known working is a factual available invocation, not an old trust grant.
        # Confirmed-unsupported and fresh failed probes stay outside this list.
        if c.get("route_status", "available") == "available":
            known.append(cid)
    risk = run.get("risk") or {}
    return {"settings": settings, "allocation": route_probe.allocation(con, run.get("id"), settings),
            "known_working": sorted(known), "quarantined": sorted(quarantined),
            "risk": {"size_class": risk.get("size_class"), "blast_radius": risk.get("blast_radius"),
                     "irreversible": bool(risk.get("irreversible")),
                     "task": {k: task_sizing.planner_declared((task or {}).get("descriptor")).get(k)
                              for k in ("task_size", "bounds", "domain")}}}


def route_role(con: sqlite3.Connection, config: dict, run: dict, role: str, *,
               task_id: str | None = None, override: str | None = None,
               exclude: set[str] | None = None, probe: bool = True, exact: str | None = None,
               dispatch_kind: str = "fresh", wave_load: dict | None = None, pending_explorations: int = 0,
               quota_snapshot: dict[str, dict] | None = None,
               quota_event_seen: set[str] | None = None, discovery_input: dict | None = None,
               manual: bool = False, declared: str | None = None, observe: bool = False) -> dict:
    """Build the request and route. Returns the routing result plus request.
    `declared` is a user-declared route identity that passes unverified adapter trust.
    `discovery_input` is the dispatch preflight's recompute handle (#494); a manual
    `override` or a non-builder role never discovers. `manual` marks a recorded
    declared route being re-qualified: like `override` it is exempt from overkill
    rules, but it keeps the ordinary candidate set, floors and discovery inputs.
    `exact` keeps only the candidate with that route identity (harness@major/model@effort).
    Executor and worker requests carry the adaptive inputs (#300); `wave_load`
    counts routes already planned for other tasks of the same wave. `observe`
    reads the supplied evidence connection without emitting quota notices."""
    from office import onboarding

    live = required_user_policies(run.get("repo_root"))
    policy_cfg = role_policy(config, role)
    # An explicit --route names its model, so it is not held to the family floor.
    floors = None if override else config.get("model_family_floors")
    settings = route_policy.discovery_settings(config)
    discovering = bool(settings["enabled"] and role in settings["roles"] and role in routing.ADAPTIVE_ROLES
                       and not override)
    task_for_route = state.get_task(con, run["id"], task_id) if task_id and run.get("id") else None
    descriptor_for_route = _effective_task_descriptor(run, task_for_route)
    sized_work = role in routing.ADAPTIVE_ROLES
    effective_task_size = (descriptor_for_route.get("task_size") if sized_work else None) or \
        (run.get("risk") or {}).get("size_class")
    seed = None if override else _preferred_seed(policy_cfg, run, effective_task_size, role=role)
    # A role allow, or an exact entry of the role's preferred list, makes a route dispatchable (#845).
    allow_policies = [route_policy.preferred_seed_policy(role, policy_cfg.get("preferred_seed") if seed else None),
                      route_policy.user_policy(config), *live]
    candidates, skipped = build_candidates(con, role, probe=probe, family_floors=floors,
                                           quota_snapshot=quota_snapshot,
                                           discovery=settings if discovering else None, user_policies=[],
                                           allow_policies=allow_policies)
    for entry in seed or []:
        if not any(routing.preferred_rank(c, [entry]) is not None for c in candidates):
            wanted = f"{entry.get('harness') + '/' if entry.get('harness') else ''}{entry.get('model_id')}" \
                     f"{'@' + entry['effort'] if entry.get('effort') else ''}"
            skipped.append({"candidate": wanted, "reason": "preferred_seed entry matches no candidate"})
    if probe and not observe and os.environ.get("OFFICE_QUOTA_PROBE") != "off" and run.get("id"):
        # One notice per call, naming every harness whose quota could not be read. A probe the
        # operator switched off is a choice, not a failure, and says nothing.
        seen = quota_event_seen if quota_event_seen is not None else set()
        unknown: dict[str, str] = {}
        for c in candidates:
            quota = c.get("quota") or {}
            harness = c.get("adapter_id")
            if quota.get("status") in ("unknown", "unavailable") and quota.get("cause") and harness not in seen:
                unknown.setdefault(harness, quota["cause"])
        if unknown:
            seen.update(unknown)
            payload = ({"harness": next(iter(unknown)), "cause": next(iter(unknown.values()))} if len(unknown) == 1
                       else {"harnesses": unknown})
            state.emit(con, run, "quota-probe-unknown",
                       "Quota probe unknown for " + "; ".join(f"{h}: {why}" for h, why in unknown.items()),
                       task_id=task_id, payload=payload)
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
        "strict_seed": bool(policy_cfg.get("strict_seed")),
    }
    request = {
        "role": role,
        "playbook": run.get("playbook"),
        "policy": policy,
        "preferred_seed": seed,
        "cost_policy": cost_policy,
        "allow_free_models": onboarding.free_models_enabled(),
        "allow_advisory_undercut": bool(gear.get("allow_advisory_undercut", True)),
        "runs_db": str(paths.runs_db()),
        "run_id": run.get("id"),
        "family_id": run.get("family_id"),
        "task_id": task_id,
        "benchmark_snapshot": snapshot,
        "candidates": candidates,
    }
    if declared:
        request["declared_route"] = declared
    if exact:
        request["exact_route"] = exact
    if role in routing.ADAPTIVE_ROLES:
        request.update(adaptive_inputs(con, config, run, role, candidates, task_id=task_id,
                                       dispatch_kind=dispatch_kind,
                                       wave_load=wave_load, pending_explorations=pending_explorations))
    if discovering:
        request["discovery"] = discovery_inputs(
            con, run, role, candidates, settings,
            state.get_task(con, run["id"], task_id) if task_id and run.get("id") else None)
        if discovery_input:
            request["discovery_input"] = discovery_input
    policies = [pol for pol in (route_policy.user_policy(config), *live)
                if pol["denied"] or pol["overkill"] or pol["ceilings"] or pol["role_rules"] or pol["strict_roles"]]
    if policies:
        request["user_policies"] = policies
        request["size_class"] = effective_task_size
        if (exact or override) and run.get("id"):
            from office import route_override
            granted = {}
            for c in candidates:
                if receipt := route_override.find_valid(run["id"], role, c, con=con):
                    # A planning/inspection call only checks the receipt is free for this task; a real
                    # routing call claims it atomically so a second task cannot reuse it.
                    free = (route_override.available_for(receipt, task_id, con=con) if observe else
                            route_override.record_use(receipt, f"{role} {routing.candidate_id(c)}", scope=task_id, con=con))
                    if free:
                        granted[routing.candidate_id(c)] = receipt["id"]
            if granted:
                request["policy_overrides"] = granted
        if any(role in pol["strict_roles"] for pol in policies):
            # Explicit user opt-in (#837): the seeded order is the whole order for this role.
            request["policy"]["strict_seed"] = True
            request["strict_seed_user_authorized"] = True
        if override or manual:
            request["manual_route"] = True
    result = routing.route(request, connection=con) if observe or con.in_transaction else routing.route(request)
    if role in routing.ADAPTIVE_ROLES and effective_task_size in executor_slates.FLOORS and seed:
        ideal = executor_slates.route_summary(seed, candidates, result.get("rejected") or [],
                                              result.get("slate") or [])
        result["ideal_route_order"] = ideal
        if isinstance(result.get("selection_disclosure"), dict):
            result["selection_disclosure"]["ideal_route_order"] = ideal
        if isinstance(result.get("audit"), dict):
            result["audit"]["ideal_route_order"] = ideal
    result["benchmark_snapshot"] = snapshot
    result["skipped"] = skipped
    result["request"] = request
    return result


def learner_priors(con: sqlite3.Connection, config: dict) -> dict[str, dict[str, dict]]:
    """role -> evidence key -> {"prior_p"}: each installed route's benchmark prior,
    the base the learner's eligibility posterior starts from."""
    from office import adaptive, route_learning
    s = adaptive.settings(config)
    out = {}
    for role in route_learning.ADAPTIVE_ROLES:
        cands, _ = build_candidates(con, role, probe=False)
        out[role] = {route_learning.candidate_key(c): {"prior_p": adaptive.benchmark_prior(c, s)[0]} for c in cands}
    return out


def trust_report(con: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Historic derived trust evidence, kept for audits only after #837.

    These observations never admit or exclude a route; `office route policy`
    is the authoritative user policy inspection command.
    """
    from office import routing, scoring
    scoring.ensure_trust_schema(con)
    summary, detail = [], []
    for role in sorted(routing.MUTABLE_TRUST_ROLES):
        cands, _ = build_candidates(con, role, probe=False)
        if not cands:
            summary.append(f"trust {role}: no candidate routes")
            continue
        counts: dict[str, int] = {}
        for c in cands:
            triple = routing.candidate_id(c)
            _, state = scoring.evaluate_trust_state(con, triple)
            counts[state] = counts.get(state, 0) + 1
            line = f"legacy trust evidence (audit only) {role} {triple}: {state}"
            if KIND_FOR_ROLE.get(role) == "vision" and "vision" not in c["capabilities"]:
                line += f" | {vision_note(con, c)}"
            detail.append(line)
        tally = ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))
        summary.append(f"legacy trust audit {role}: {len(cands)} routes ({tally}) | current policy: office route policy --effective --role {role}")
    return summary, detail


def canonical_trust_routes() -> list[str]:
    """All canonical route triples `harness@major/model_id@effort` across active catalog rows."""
    from office import scoring
    sources = adapters.load_sources()
    all_adapters = {aid: entry[0] for aid, entry in sources.items()}
    triples: set[str] = set()
    for row in catalog_rows():
        harness = row.get("invocation_harness")
        adapter = all_adapters.get(harness)
        if not adapter:
            continue
        version = adapters.route_version(harness, adapter, sources)
        cand = {
            "harness": harness,
            "harness_version": version,
            "model_id": row.get("model_id"),
            "effort": row.get("effort"),
        }
        triples.add(routing.candidate_id(cand))
    return sorted(triples)


def resolve_trust_targets(spec: str, con: sqlite3.Connection | None = None,
                          run: dict | None = None) -> tuple[list[str], list[str], str]:
    """Resolve a user-provided route `harness/model@effort` (alias or concrete, with or without
    @version) through the catalog expansion into canonical routing triples and any remaining gate notices.
    """
    import difflib
    from office import scoring
    clean_spec = (spec or "").strip()
    m = re.match(r"^(?P<harness>[^@/]+)(?:@(?P<version>[^/]*))?/(?P<model>[^@/]+)@(?P<effort>[^@/]+)$", clean_spec)
    canonical = canonical_trust_routes()
    if not m:
        nearest = difflib.get_close_matches(clean_spec, canonical, n=3, cutoff=0.3)
        msg = (f"unknown route {clean_spec!r}; nearest canonical routes: {', '.join(nearest)}"
               if nearest else f"unknown route {clean_spec!r}; see available routes with office inspect trust")
        raise state.Refused("unknown-route", msg, next_step="office inspect trust")

    harness = m.group("harness")
    spec_version = m.group("version")
    model = m.group("model")
    effort = m.group("effort")

    sources = adapters.load_sources()
    all_adapters = {aid: entry[0] for aid, entry in sources.items()}
    adapter = all_adapters.get(harness)
    if not adapter:
        nearest = difflib.get_close_matches(clean_spec, canonical, n=3, cutoff=0.3)
        msg = (f"unknown route {clean_spec!r}; nearest canonical routes: {', '.join(nearest)}"
               if nearest else f"unknown route {clean_spec!r}; see available routes with office inspect trust")
        raise state.Refused("unknown-route", msg, next_step="office inspect trust")

    harness_ver = adapters.route_version(harness, adapter, sources)
    installed_major = scoring.harness_major(harness_ver)
    if spec_version is not None and scoring.harness_major(spec_version) != installed_major:
        nearest = difflib.get_close_matches(clean_spec, canonical, n=3, cutoff=0.3)
        msg = (f"unknown route {clean_spec!r}; nearest canonical routes: {', '.join(nearest)}"
               if nearest else f"unknown route {clean_spec!r}; see available routes with office inspect trust")
        raise state.Refused("unknown-route", msg, next_step="office inspect trust")

    rows = catalog_rows()
    matching = [
        r for r in rows
        if r.get("invocation_harness") == harness
        and r.get("effort") == effort
        and (r.get("model_id") == model or r.get("invocation_model_id") == model)
    ]
    matching.sort(key=lambda r: r.get("model_id") != model)
    if not matching:
        nearest = difflib.get_close_matches(clean_spec, canonical, n=3, cutoff=0.3)
        msg = (f"unknown route {clean_spec!r}; nearest canonical routes: {', '.join(nearest)}"
               if nearest else f"unknown route {clean_spec!r}; see available routes with office inspect trust")
        raise state.Refused("unknown-route", msg, next_step="office inspect trust")

    primary_row = matching[0]
    triples: list[str] = []

    if primary_row.get("alias_resolved_to"):
        alias_cand = {
            "harness": harness,
            "harness_version": harness_ver,
            "model_id": primary_row["model_id"],
            "effort": effort,
        }
        alias_triple = routing.candidate_id(alias_cand)
        triples.append(alias_triple)
        target_model = primary_row["alias_resolved_to"]
        concrete_rows = [
            r for r in rows
            if r.get("invocation_harness") == harness
            and r.get("effort") == effort
            and r.get("model_id") == target_model
            and not r.get("alias_family")
        ]
        if concrete_rows:
            conc_cand = {
                "harness": harness,
                "harness_version": harness_ver,
                "model_id": target_model,
                "effort": effort,
            }
            conc_triple = routing.candidate_id(conc_cand)
            if conc_triple not in triples:
                triples.append(conc_triple)
    else:
        conc_cand = {
            "harness": harness,
            "harness_version": harness_ver,
            "model_id": primary_row["model_id"],
            "effort": effort,
        }
        triples.append(routing.candidate_id(conc_cand))

    # Evaluate remaining gates on the route
    gate_lines: list[str] = []
    remedy: str | None = None

    if primary_row.get("dispatchable") is False:
        discovery_eligible = bool(primary_row.get("discovery") == "eligible" and primary_row.get("invocation_model_id"))
        if not discovery_eligible:
            gate_lines.append("remaining gate: not dispatchable (remedy: route is non-dispatchable in catalog; select an available route)")
            remedy = remedy or "route is not dispatchable; select an available route"
        else:
            # Check discovery configuration
            repo = paths.repo_identity()
            repo_root = Path(run["repo_root"]) if (run and run.get("repo_root")) else (repo[0] if repo else None)
            from office.config import resolve
            config_error = None
            live_cfg = None
            try:
                live_cfg, _ = resolve(repo_root)
            except Exception as exc:
                config_error = str(exc)

            if config_error:
                gate_lines.append(f"remaining gate: config unreadable (remedy: repair the unreadable config: {config_error})")
                remedy = remedy or f"repair the unreadable config: {config_error}"
            else:
                is_existing_run = bool(run and (run.get("id") or run.get("policy")))
                if is_existing_run:
                    pinned_policy = run.get("policy")
                    if pinned_policy is None and con is not None and run.get("id"):
                        try:
                            loaded = state.get_run(con, run["id"])
                            if loaded:
                                pinned_policy = loaded.get("policy")
                        except Exception:
                            pass
                    run_disc = route_policy.discovery_settings(pinned_policy or {})
                    if not run_disc.get("enabled"):
                        gate_lines.append("remaining gate: discovery off or blocked (remedy: enable discovery at the applicable config tier and then authorize office config --run <id> --apply-routing --quote \"<words>\" for this run)")
                        remedy = remedy or "enable discovery at the applicable config tier and then authorize office config --run <id> --apply-routing --quote \"<words>\" for this run"
                else:
                    disc_settings = route_policy.discovery_settings(live_cfg or {})
                    if not disc_settings.get("enabled"):
                        gate_lines.append("remaining gate: discovery off or blocked (remedy: office config routing.discovery.enabled true)")
                        remedy = remedy or "office config routing.discovery.enabled true"

            # Check exact conformance probe status
            disc_settings = route_policy.discovery_settings(
                (run.get("policy") if (run and run.get("policy")) else live_cfg) or {}
            )
            probe_passed = False
            if con is not None:
                try:
                    from office import route_probe
                    probe_cand = {
                        "harness": harness,
                        "harness_version": harness_ver,
                        "model_id": primary_row.get("model_id"),
                        "invocation_model_id": primary_row.get("invocation_model_id") or primary_row.get("model_id"),
                        "effort": effort,
                        "adapter_id": adapter.get("id"),
                    }
                    status = route_probe.status(con, probe_cand, adapter=adapter,
                                                ttl_days=disc_settings.get("probe_ttl_days", 7),
                                                timeout_s=disc_settings.get("probe_timeout_s", 120))
                    if status and status.get("result") == "pass":
                        probe_passed = True
                except Exception:
                    probe_passed = False
            if not probe_passed:
                probe_spec = f"{harness}/{primary_row.get('model_id')}@{effort}"
                gate_lines.append(f"remaining gate: conformance probe required (remedy: office doctor --probe-route {probe_spec})")
                remedy = remedy or f"office doctor --probe-route {probe_spec}"

    next_action = remedy if gate_lines else "retry the dispatch that needed it"
    return triples, gate_lines, next_action
