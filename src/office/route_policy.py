"""Pure route discovery and user policy contracts (#494).

Invocation conformance is independent of quality evidence and adapter trust.
Callers supply fresh, exact fingerprint probe records; this module never
infers a pass for a sibling route and never grants trust.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

from office.util import dumps, new_run_id, now_iso, sha256_obj

POLICY_VERSION = "route-discovery-1"
PROVENANCE_KEY = "_provenance"
DIGEST_KEY = "_route_policy_digest"
CEILING_KEY = "routing.adaptive.budget_ceiling_usd"
ROUTE_STATUSES = ("available", "discovered-unconfirmed", "probe-pending", "probe-passed",
                  "temporarily-unavailable", "confirmed-unsupported")
DISCOVERY_DEFAULTS = {
    "enabled": False, "roles": ["executor", "worker"],
    "max_probes_per_run": 2, "max_trials_per_run": 1,
    "max_trial_percent_rolling_20": 15,
    "trial_size_classes": ["S", "M"], "trial_blast_radius": ["local", "repo"],
    "require_known_fallback": True, "probe_ttl_days": 7, "probe_timeout_s": 120,
}
_ROUTE = re.compile(r"(?:(?P<harness>[A-Za-z0-9_.-]+)/)?(?P<model>[A-Za-z0-9_.-]+)(?:@(?P<effort>[a-z]+))?\Z")
_HARNESSES = re.compile(r"harness:([A-Za-z0-9_.-]+)\Z")
EVENT_KINDS = ("probe-reserved", "probe-result", "probe-cache-hit", "probe-refused", "probe-expired",
               "probe-abandoned", "dispatch-linked", "trial-reserved", "trial-launched", "trial-launch-failed",
               "trial-fell-back", "trial-submitted", "trial-accepted", "trial-rejected", "trial-abandoned")
EVENT_ORIGINS = ("preflight", "manual", "dispatch", "recovery", "job")
_EVENT_FIELDS = ("run_id", "plan_version", "task_id", "dispatch_id", "role", "fingerprint_json",
                 "candidate_route", "primary_route", "fallback_route", "probe_freshness", "source_attempt_id",
                 "allocation_json", "outcome", "reason_class", "detail")


def row_status(row: dict, probe: dict | None = None) -> dict:
    eligible = (row.get("dispatchable") is False and row.get("discovery") == "eligible"
                and bool(row.get("invocation_model_id")))
    reason = row.get("discovery_reason") or row.get("invocation_source") or "catalog invocation unsupported"
    if row.get("dispatchable") is not False:
        status, reason = "available", "catalog invocation available"
    elif not eligible:
        status = "confirmed-unsupported"
    else:
        status = "discovered-unconfirmed"
        if probe:
            result, category = probe.get("result"), probe.get("reason_class")
            if result == "pending":
                status = "probe-pending"
            elif result == "pass":
                status = "probe-passed"
            elif result == "fail":
                status = ("confirmed-unsupported" if category in
                          ("unsupported-model-effort", "conformance-failed") else "temporarily-unavailable")
            if result in ("pending", "pass", "fail"):
                reason = ": ".join(str(v) for v in (result, category, probe.get("detail")) if v)
    return {"status": status, "reason": reason, "discovery_eligible": eligible}


def alias_status(alias_row: dict, target_row: dict) -> dict:
    """Inherit the exact target's status, including its probe refinement."""
    inherited = row_status(target_row)
    if inherited["discovery_eligible"] and target_row.get("route_status") in ROUTE_STATUSES[1:]:
        inherited["status"] = target_row["route_status"]
        inherited["reason"] = target_row.get("status_reason") or inherited["reason"]
    target = target_row.get("model_id") or target_row.get("invocation_model_id")
    return {**inherited, "reason": f"alias {alias_row.get('model_id')} target {target}: {inherited['reason']}"}


def discovery_settings(config: dict) -> dict:
    settings = deepcopy(DISCOVERY_DEFAULTS)
    settings.update(deepcopy((config.get("routing") or {}).get("discovery") or {}))
    if PROVENANCE_KEY not in config:
        # A pinned pre-#494 policy must never opt in via changed defaults.
        settings["enabled"] = False
    return settings


def validate_discovery(config: dict) -> list[str]:
    raw = (config.get("routing") or {}).get("discovery", {})
    if not isinstance(raw, dict):
        return ["routing.discovery must be a mapping"]
    settings = {**DISCOVERY_DEFAULTS, **raw}
    problems = []
    for key in ("enabled", "require_known_fallback"):
        if type(settings[key]) is not bool:
            problems.append(f"routing.discovery.{key} must be a boolean")
    for key, allowed in (("roles", {"executor", "worker"}), ("trial_size_classes", {"S", "M"}),
                         ("trial_blast_radius", {"local", "repo"})):
        value = settings[key]
        if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
            problems.append(f"routing.discovery.{key} must be a list containing only {', '.join(sorted(allowed))}")
    for key, low, high in (("max_probes_per_run", 0, 20), ("max_trials_per_run", 0, 20),
                           ("probe_ttl_days", 1, 365), ("probe_timeout_s", 1, 900)):
        value = settings[key]
        if type(value) is not int or not low <= value <= high:
            problems.append(f"routing.discovery.{key} must be an integer between {low} and {high}")
    value = settings["max_trial_percent_rolling_20"]
    if type(value) not in (int, float) or not 0 <= value <= 100 or not math.isfinite(value):
        problems.append("routing.discovery.max_trial_percent_rolling_20 must be between 0 and 100")
    return problems


def validate_ceiling(config: dict) -> list[str]:
    value = ((config.get("routing") or {}).get("adaptive") or {}).get("budget_ceiling_usd")
    if value is None:
        return []
    try:
        valid = type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        return [f"{CEILING_KEY} must be a finite positive number or null"]
    return []


def user_policy(config: dict) -> dict:
    raw = (config.get("routing") or {}).get("user_policy") or {}
    provenance = config.get(PROVENANCE_KEY) or {}
    return {"denied": deepcopy(raw.get("denied_models") or []),
            "overkill": deepcopy(raw.get("overkill_rules") or []),
            "sources": {key: provenance.get(f"routing.user_policy.{key}")
                        for key in ("denied_models", "overkill_rules")}}


def _valid_spec(spec) -> bool:
    return isinstance(spec, str) and bool(_ROUTE.fullmatch(spec) or _HARNESSES.fullmatch(spec))


def validate_user_policy(config: dict) -> list[str]:
    raw = (config.get("routing") or {}).get("user_policy", {})
    if not isinstance(raw, dict):
        return ["routing.user_policy must be a mapping"]
    problems = []
    denied = raw.get("denied_models", [])
    if not isinstance(denied, list) or any(not _valid_spec(spec) for spec in denied):
        problems.append("routing.user_policy.denied_models must be a list of model, harness/model[@effort], or harness:<name>")
    overkill = raw.get("overkill_rules", [])
    if not isinstance(overkill, list):
        return problems + ["routing.user_policy.overkill_rules must be a list"]
    for rule in overkill:
        if not isinstance(rule, dict) or not _valid_spec(rule.get("route")):
            problems.append("routing.user_policy.overkill_rules entries need a valid route")
            continue
        for key in ("roles", "size_classes"):
            if key in rule and (not isinstance(rule[key], list) or
                                any(not isinstance(v, str) or not v for v in rule[key])):
                problems.append(f"routing.user_policy.overkill_rules.{key} must be a list of nonempty strings")
    return problems


def _matches(cand: dict, spec: str) -> bool:
    harness = cand.get("harness") or cand.get("invocation_harness")
    if match := _HARNESSES.fullmatch(spec):
        return harness == match[1]
    match = _ROUTE.fullmatch(spec)
    if not match:
        return False
    models = {cand.get(key) for key in ("model_id", "invocation_model_id", "alias_resolved_to")}
    return (match["model"] in models and (match["harness"] is None or match["harness"] == harness)
            and (match["effort"] is None or match["effort"] == cand.get("effort")))


def _policy_reason(kind: str, spec: str, policy: dict, key: str) -> str:
    source = (policy.get("sources") or {}).get(key) or "run"
    return f"{kind} by {source} routing.user_policy.{key}: {spec}"


def is_denied(cand: dict, policy: dict) -> str | None:
    return next((_policy_reason("denied", spec, policy, "denied_models")
                 for spec in policy.get("denied", []) if _matches(cand, spec)), None)


def is_overkill(cand: dict, role: str, size_class: str | None, policy: dict) -> str | None:
    for rule in policy.get("overkill", []):
        if (_matches(cand, rule["route"]) and ("roles" not in rule or role in rule["roles"])
                and ("size_classes" not in rule or size_class in rule["size_classes"])):
            return _policy_reason("overkill", rule["route"], policy, "overkill_rules")
    return None


def budget_ceiling(config: dict) -> dict:
    value = ((config.get("routing") or {}).get("adaptive") or {}).get("budget_ceiling_usd")
    provenance = config.get(PROVENANCE_KEY)
    source = provenance.get(CEILING_KEY) if provenance is not None else ("run" if value is not None else None)
    # Old pinned numbers remain hard; shipped discovery-era scales do not.
    return {"usd": float(value) if value is not None and source in ("run", "repo", "user") else None,
            "source": source}


def policy_digest(config: dict) -> str:
    return sha256_obj({"version": POLICY_VERSION, "discovery": discovery_settings(config),
                       "user_policy": user_policy(config), "budget_ceiling": budget_ceiling(config)})


def new_attempt_id() -> str:
    return new_run_id()


def record_event(con, *, kind: str, attempt_id: str, origin: str, policy_digest: str,
                 probe_key: str, reason: str, **fields) -> str:
    """Append audit evidence inside the caller's state-change transaction.

    JSON fields accept mappings or serialized JSON. This helper never commits,
    replaces a row, updates a cache or deletes prior evidence.
    """
    if not con.in_transaction:
        raise ValueError("record_event requires the caller's db.transaction")
    if kind not in EVENT_KINDS or origin not in EVENT_ORIGINS:
        raise ValueError("unknown discovery event kind or origin")
    for key, value in (("attempt_id", attempt_id), ("policy_digest", policy_digest),
                       ("probe_key", probe_key), ("reason", reason),
                       ("candidate_route", fields.get("candidate_route"))):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"discovery event requires {key}")
    unknown = fields.keys() - set(_EVENT_FIELDS)
    if unknown:
        raise ValueError(f"unknown discovery event fields: {', '.join(sorted(unknown))}")
    fingerprint = fields.get("fingerprint_json")
    if isinstance(fingerprint, str):
        fingerprint = json.loads(fingerprint)
    required = ("harness", "harness_version", "adapter_hash", "profile", "invocation_model_id", "effort")
    if not isinstance(fingerprint, dict) or any(not isinstance(fingerprint.get(k), str) or
                                               not fingerprint[k].strip() for k in required):
        raise ValueError("discovery event requires fingerprint_json with the exact invocation fingerprint")
    if kind == "probe-cache-hit" and not fields.get("source_attempt_id"):
        raise ValueError("probe-cache-hit requires source_attempt_id")
    freshness = fields.get("probe_freshness")
    if freshness is not None and freshness not in ("fresh-run", "cached-fresh", "stale", "none"):
        raise ValueError("unknown probe_freshness")
    for key in ("fingerprint_json", "allocation_json"):
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            fields[key] = dumps(fields[key])
    event_id = new_run_id()
    values = {"id": event_id, "attempt_id": attempt_id, "kind": kind, "origin": origin,
              "policy_digest": policy_digest, "policy_version": POLICY_VERSION, "probe_key": probe_key,
              "reason": reason, "created_at": now_iso(), **fields}
    columns = list(values)
    con.execute(f"INSERT INTO route_discovery_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values[key] for key in columns))
    return event_id
