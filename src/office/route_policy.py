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
from office import model_lineages

POLICY_VERSION = "route-discovery-3"
# A route that fails at runtime is demoted for this long, never permanently (#845).
ROUTE_FAILURE_TTL_MINUTES = 30
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
    # Off here so a run pinned before this key existed never gains it; the shipped config turns it on for new runs.
    "trial_bounded_production_tasks": False,
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
    reason = row.get("discovery_reason") or row.get("invocation_source") or "catalog invocation unconfirmed"
    if row.get("dispatchable") is not False:
        status, reason = "available", "catalog invocation available"
    elif not eligible:
        status = "discovered-unconfirmed"
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


def is_free_model(row: dict) -> bool:
    """Whether the catalog explicitly marks this route as a free model."""
    return row.get("free_model") is True


def is_explicit_free_slug(harness: str | None, *slugs: str | None) -> bool:
    """Whether an opencode slug names itself free (the `-free` suffix convention)."""
    return harness == "opencode" and any(isinstance(s, str) and s.strip().lower().endswith("-free") for s in slugs)


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


def trial_risk_blocked(settings: dict, risk: dict, task: dict | None = None) -> bool:
    """Whether the run/task risk bars a discovery trial. One definition for routing and the dispatch recheck.

    The run-level gate is unchanged: an S/M run with a local/repo blast radius, nothing irreversible. A
    `production` run (never `production-data`, never irreversible) may still trial a builder on one task
    that is itself declared small and bounded: `task_size` S/M, `bounds: bounded`, and not infrastructure.
    Executor tasks run in an isolated worktree and never deploy or write production, review and landing
    floors are untouched, and the known-working fallback stays required (`no-fallback`)."""
    if risk.get("irreversible"):
        return True
    if risk.get("size_class") in settings["trial_size_classes"] and risk.get("blast_radius") in settings["trial_blast_radius"]:
        return False
    if not settings.get("trial_bounded_production_tasks") or risk.get("blast_radius") != "production":
        return True
    task = task or {}
    return not (task.get("task_size") in settings["trial_size_classes"] and task.get("bounds") == "bounded"
                and task.get("domain") != "infrastructure")


def validate_discovery(config: dict) -> list[str]:
    raw = (config.get("routing") or {}).get("discovery", {})
    if not isinstance(raw, dict):
        return ["routing.discovery must be a mapping"]
    settings = {**DISCOVERY_DEFAULTS, **raw}
    problems = []
    for key in ("enabled", "require_known_fallback", "trial_bounded_production_tasks"):
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
            "role_rules": deepcopy(raw.get("role_rules") or []),
            "strict_roles": list(raw.get("strict_allow_roles") or []),
            "ceilings": deepcopy(raw.get("version_ceilings") or {}),
            "ceiling_sources": {lineage: provenance.get(f"routing.user_policy.version_ceilings.{lineage}")
                                for lineage in (raw.get("version_ceilings") or {})},
            "overkill": deepcopy(raw.get("overkill_rules") or []),
            "sources": {key: provenance.get(f"routing.user_policy.{key}")
                        for key in ("denied_models", "version_ceilings", "overkill_rules", "role_rules",
                                    "strict_allow_roles")}}


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
    ceilings = raw.get("version_ceilings", {})
    if not isinstance(ceilings, dict):
        problems.append("routing.user_policy.version_ceilings must be a mapping of provider/lineage to numeric version")
    else:
        for lineage, ceiling in ceilings.items():
            if not isinstance(lineage, str) or not model_lineages.LINEAGE_KEY_RE.fullmatch(lineage):
                problems.append(f"routing.user_policy.version_ceilings: invalid lineage {lineage!r}")
                continue
            try:
                model_lineages.version_parts(ceiling)
            except model_lineages.LineageError:
                problems.append(f"routing.user_policy.version_ceilings.{lineage} must be a quoted numeric generation")
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
    strict = raw.get("strict_allow_roles", [])
    if not isinstance(strict, list) or any(r not in POLICY_ROLES for r in strict):
        problems.append("routing.user_policy.strict_allow_roles must be a list of known roles")
    rules = raw.get("role_rules", [])
    if not isinstance(rules, list):
        return problems + ["routing.user_policy.role_rules must be a list"]
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"role", "route", "state", "wildcard"}:
            problems.append("routing.user_policy.role_rules entries must contain only role, route, state, wildcard")
            continue
        role, spec, state = rule.get("role"), rule.get("route"), rule.get("state")
        if role not in POLICY_ROLES and not (role == "*" and rule.get("wildcard") is True):
            problems.append(f"routing.user_policy.role_rules invalid role: {role!r}")
        if state not in ROLE_STATES:
            problems.append(f"routing.user_policy.role_rules invalid state: {state!r}")
        if not _valid_spec(spec) and not _canonical_spec(spec):
            problems.append(f"routing.user_policy.role_rules invalid route: {spec!r}")
        elif not _is_exact_spec(spec) and rule.get("wildcard") is not True:
            problems.append(f"routing.user_policy.role_rules broad route {spec!r} requires wildcard: true")
        if "wildcard" in rule and type(rule["wildcard"]) is not bool:
            problems.append("routing.user_policy.role_rules wildcard must be a boolean")
        key = (role, spec)
        if key in seen:
            problems.append(f"routing.user_policy.role_rules duplicate rule: {role}/{spec}")
        seen.add(key)
    return problems


ROLE_STATES = ("allow", "neutral", "deny")
POLICY_ROLES = frozenset(("executor", "worker", "planner", "plan_reviewer", "code_reviewer",
                          "browser_verifier", "visual_reviewer", "closeout_verifier", "integration_reviewer"))
_CANONICAL = re.compile(r"(?P<harness>[A-Za-z0-9_.-]+)@(?P<major>[0-9]+)/(?P<model>[A-Za-z0-9_.-]+)@(?P<effort>[a-z]+)\Z")
# Only these reviewed default *preferences* ship. Legacy trust history does not grant admission.
SEED_ROLE_RULES = (
    {"role": "executor", "route": "claude@2/claude-sonnet-5-5@high", "state": "allow"},
    {"role": "code_reviewer", "route": "claude@2/claude-sonnet-5-5@high", "state": "allow"},
    {"role": "plan_reviewer", "route": "claude@2/claude-sonnet-5-5@high", "state": "allow"},
    {"role": "code_reviewer", "route": "claude@2/claude-opus-5-5@low", "state": "allow"},
    {"role": "code_reviewer", "route": "codex@0/gpt-6-luna@high", "state": "allow"},
    {"role": "plan_reviewer", "route": "codex@0/gpt-6-luna@high", "state": "allow"},
    {"role": "visual_reviewer", "route": "agy@1/gemini-3.8-flash@low", "state": "allow"},
)


def _canonical_spec(spec) -> bool:
    return isinstance(spec, str) and bool(_CANONICAL.fullmatch(spec))


def _is_exact_spec(spec) -> bool:
    # Intentionally require both harness and effort for an unambiguous non-wildcard rule.
    if _canonical_spec(spec):
        return True
    match = _ROUTE.fullmatch(spec) if isinstance(spec, str) else None
    return bool(match and match["harness"] and match["effort"])


def _rule_matches(cand: dict, spec: str, *, state: str | None = None) -> bool:
    if not isinstance(spec, str):
        return False
    if match := _CANONICAL.fullmatch(spec):
        from office import scoring
        major = scoring.harness_major(cand.get("harness_version"))
        # A deny must not fail open because the installed major cannot be read.
        major_ok = major == match["major"] or (state == "deny" and not major.isdigit())
        return (cand.get("harness") == match["harness"] and major_ok and
                match["model"] in {cand.get("model_id"), cand.get("alias_resolved_to")} and
                cand.get("effort") == match["effort"])
    return _matches(cand, spec)


def effective_role_state(cand: dict, role: str, policies: list[dict]) -> dict:
    """Effective role policy; newest user rules > legacy denies > shipped allow > neutral.

    Policy evaluation is independent of technical availability, quotas and review safety.
    A list entry is exact unless its author deliberately set `wildcard: true`.
    """
    for policy in reversed(policies):
        matching = [rule for rule in policy.get("role_rules", ())
                    if rule.get("role") in (role, "*") and _rule_matches(cand, rule["route"], state=rule.get("state"))]
        if matching:
            # Specific role always beats a deliberate all-role wildcard.
            matching.sort(key=lambda r: (r["role"] != role, not _is_exact_spec(r["route"])))
            best = [r for r in matching if r["role"] == matching[0]["role"]
                    and _is_exact_spec(r["route"]) == _is_exact_spec(matching[0]["route"])]
            # Equivalent spellings of one route can disagree; fail closed, deterministically.
            rule = next((r for r in best if r["state"] == "deny"), None) or sorted(best, key=lambda r: r["state"])[0]
            source = (policy.get("sources") or {}).get("role_rules") or "user"
            return {"state": rule["state"], "source": source, "rule": rule["route"]}
    for policy in reversed(policies):
        if reason := is_denied(cand, policy):
            return {"state": "deny", "source": "legacy-user-deny", "reason": reason}
    for rule in SEED_ROLE_RULES:
        if rule["role"] == role and _rule_matches(cand, rule["route"]):
            return {"state": rule["state"], "source": "shipped", "rule": rule["route"]}
    return {"state": "neutral", "source": "default", "reason": "no role-specific deny"}


def user_allows(cand: dict, role: str, policies: list[dict]) -> dict | None:
    """The effective rule when role policy allows this route (#845), else None.

    An allow is the whole admission test: it makes a route dispatchable without any proof, qualification or
    trust gate, and it outranks a catalog or overlay `dispatchable: false`. A runtime failure falls back."""
    rule = effective_role_state(cand, role, policies) if policies else None
    return rule if rule and rule["state"] == "allow" else None


def preferred_seed_policy(role: str, entries) -> dict:
    """A policy holding one allow per exact entry (harness, model_id and effort) of a role's preferred list.

    A preferred list is a user choice of routes, so it makes them dispatchable (#845). It is placed
    before real policies so any explicit user rule outranks it, and it is never a deny or a floor bypass."""
    rules = [{"role": role, "route": f"{e['harness']}/{e['model_id']}@{e['effort']}", "state": "allow"}
             for e in entries or [] if isinstance(e, dict) and e.get("harness") and e.get("model_id") and e.get("effort")]
    return {"denied": [], "role_rules": rules, "strict_roles": [], "ceilings": {}, "ceiling_sources": {},
            "overkill": [], "sources": {"role_rules": "preferred_seed"}}


def allow_specs(role: str, policies: list[dict]) -> list[str]:
    """Every route a policy allows for this role by name (harness/model@effort), for catalog cross-checks (#845)."""
    out = []
    for policy in policies:
        for rule in policy.get("role_rules", ()):
            if rule.get("state") == "allow" and rule.get("role") in (role, "*") and rule["route"] not in out:
                out.append(rule["route"])
    return out


def spec_parts(spec: str) -> tuple[str | None, str | None, str | None]:
    """(harness, model, effort) of a route spec in either spelling; model is None for a `harness:<name>` spec."""
    if match := _CANONICAL.fullmatch(spec):
        return match["harness"], match["model"], match["effort"]
    if match := _ROUTE.fullmatch(spec):
        return match["harness"], match["model"], match["effort"]
    return None, None, None


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
    explicit = next((_policy_reason("denied", spec, policy, "denied_models")
                     for spec in policy.get("denied", []) if _matches(cand, spec)), None)
    if explicit:
        return explicit
    ceilings = policy.get("ceilings") or {}
    if not ceilings:
        return None
    # Prefer an audited, concrete route target over an alias's UI name.
    identity = model_lineages.identify(cand.get("model_id"), aliases=cand)
    # Denial uses authoritative catalog identity, never caller-provided claims.
    # User-created/unclassified routes cannot spoof their version to escape a
    # ceiling; they remain subject to the separate unverified/trust gates.
    lineage = (identity or {}).get("lineage_key")
    version = (identity or {}).get("generation_version")
    if lineage not in ceilings or not version:
        return None  # unknown identity is not a fabricated user denial
    if model_lineages.version_parts(version) <= model_lineages.version_parts(ceilings[lineage]):
        source = ((policy.get("ceiling_sources") or {}).get(lineage)
                  or (policy.get("sources") or {}).get("version_ceilings") or "run")
        return (f"denied by {source} routing.user_policy.version_ceilings: "
                f"{lineage} <= {ceilings[lineage]} (concrete generation {version})")
    return None


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
                       "user_policy": user_policy(config), "seeded_role_rules": SEED_ROLE_RULES, "budget_ceiling": budget_ceiling(config)})


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
    if freshness not in ("fresh-run", "cached-fresh", "stale", "none"):
        raise ValueError("discovery event requires valid probe_freshness")
    allocation = fields.get("allocation_json")
    if isinstance(allocation, str):
        allocation = json.loads(allocation)
    if not isinstance(allocation, dict) or any(not isinstance(allocation.get(k), dict) or
                                              not {"used", "max"} <= allocation[k].keys()
                                              for k in ("probes", "trials", "rolling")):
        raise ValueError("discovery event requires allocation_json with probes, trials and rolling used/max")
    fields["fingerprint_json"] = dumps(fingerprint)
    fields["allocation_json"] = dumps(allocation)
    event_id = new_run_id()
    values = {"id": event_id, "attempt_id": attempt_id, "kind": kind, "origin": origin,
              "policy_digest": policy_digest, "policy_version": POLICY_VERSION, "probe_key": probe_key,
              "reason": reason, "created_at": now_iso(), **fields}
    columns = list(values)
    con.execute(f"INSERT INTO route_discovery_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values[key] for key in columns))
    return event_id
