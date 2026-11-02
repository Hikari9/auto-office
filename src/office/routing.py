"""Derived routing: harness@version x model x effort.

Ported from the v3 `scripts/office_routing.py`. Role-scoped allow/neutral/deny
policy is independent of historical trust. Capability, technical safety and
local reward remain separately derived from verified facts. The quota reserve comes from the pinned
policy (5% by default), never a literal.

Executor and worker routes (#300) keep stages 1-6 as qualification, then
`office.adaptive` compares the qualifying set and returns a slate of up to
three. Planner and reviewer routes keep the legacy stages 7-9, including the
balanced money band, until their own routing is redesigned.
"""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from office import adaptive, executor_slates, route_learning, route_policy, scoring
from office.util import sha256_obj

# Roles whose selection confers mutable or gate authority; they need derived trust.
MUTABLE_TRUST_ROLES = {"executor", "code_reviewer", "browser_verifier", "closeout_verifier", "visual_reviewer",
                       "integration_reviewer"}
DEFAULT_RESERVE_PERCENT = 5.0
# Legacy planner/reviewer routing only. Executor and worker routing (#300) has no
# money band; its cost safety valve is routing.adaptive.budget_ceiling_multiple.
DEFAULT_MONEY_BAND_PERCENT = 20.0
ADAPTIVE_ROLES = set(route_learning.ADAPTIVE_ROLES)
_executor_policy_context: ContextVar[dict | None] = ContextVar("executor_policy", default=None)

_REQUIRED_OVERRIDE_FIELDS = (
    "override_id", "run_id", "family_id", "task_id", "role", "candidate_id",
    "bypass_stage", "rationale", "authorized_by", "authorized_at", "expires_at",
)


@contextmanager
def _using_executor_policy(policy: dict):
    token = _executor_policy_context.set(policy)
    try:
        yield
    finally:
        _executor_policy_context.reset(token)


# Rejections that mean a route was avoided for a hard quota or auth failure (#921). Others (trust, capability, concurrency,
# a projected quota reserve, a runtime demotion whose reason is not quota or auth) are not recovered when their route becomes
# available again. A projected quota reserve is never recovery evidence: recovery does not re-run the reserve gate, so a
# probe pass does not show that the reserve admits the route.
QUOTA_AUTH_CATEGORIES = frozenset({"rate-limited"})
QUOTA_AUTH_REASON_CLASSES = frozenset({"auth-quota-blocked"})


def quota_auth_rejection(rejection: dict) -> bool:
    """Whether a rejection avoided its route for quota or auth (#921): a quota or auth cooldown category, or a
    reason class of a failed exact probe. A projected quota reserve is not one."""
    return rejection.get("category") in QUOTA_AUTH_CATEGORIES or rejection.get("reason_class") in QUOTA_AUTH_REASON_CLASSES


def candidate_id(c):
    """Route identity `harness@major/model_id@effort`. Trust keys on the harness major,
    so a point release never resets it; the full `harness_version` stays in disclosure."""
    return f"{c.get('harness')}@{scoring.harness_major(c.get('harness_version'))}/{c.get('model_id')}@{c.get('effort')}"


# Parses a candidate id back into its harness, model id and effort (#921).
CANDIDATE_ID_RE = re.compile(r"(?P<harness>[^@/]+)@[^/]*/(?P<model>[^@]+)@(?P<effort>.+)\Z")


def cooldown_key(harness: str | None, invocation_model_id: str | None) -> str:
    """Stable quota cooldown identity shared by effort variants of one invocation."""
    return f"{harness or ''}/{invocation_model_id or ''}"


def _explicit_allow(rule: dict) -> bool:
    """Whether a user's own role rule allows this route (#845); the shipped seed and neutral default do not count."""
    return rule.get("state") == "allow" and rule.get("source") not in ("shipped", "default")


def demotion_key(harness: str | None, model: str | None, effort: str | None) -> str:
    """The cooldown identity of one route (effort included) demoted after a runtime failure (#845)."""
    return f"{cooldown_key(harness, model)}@{effort}"


def harness_demotion_key(harness: str | None) -> str:
    """The cooldown identity of a whole harness demoted after an auth failure (#845)."""
    return f"{harness or ''}/*"


def shared_quota_pool_key(harness: str | None, model: str | None) -> str | None:
    """Known common provider quota only: OpenCode's *-free models share the free-tier wall.

    Never demote a paid OpenCode route, a different provider, or an unproven
    shared pool based on a single failed model (#860).
    """
    if (harness or "").lower() == "opencode" and (model or "").lower().endswith("-free"):
        return "pool:opencode:free"
    return None


def _demotion_keys(c: dict) -> tuple[str, ...]:
    model = c.get("invocation_model_id") or c.get("model_id")
    keys = (demotion_key(c.get("harness"), model, c.get("effort")),
            harness_demotion_key(c.get("harness")))
    pool = shared_quota_pool_key(c.get("harness"), model)
    return keys + ((pool,) if pool else ())


def preferred_rank(c, preferred_seed):
    """Index of the first roles.<role>.preferred_seed entry c matches, or None.

    An entry matches on model_id (required) plus effort/harness when the entry
    specifies them, so a config seed of {model_id, effort} without harness
    matches that model/effort on any harness.
    """
    for i, p in enumerate(preferred_seed or []):
        if p.get("model_id") != c.get("model_id"):
            continue
        if p.get("effort") and p.get("effort") != c.get("effort"):
            continue
        if p.get("harness") and p.get("harness") != c.get("harness"):
            continue
        return i
    return None


def invocation_provenance(candidate: dict) -> str:
    """Return the machine-readable provenance state for a candidate's invocation slug.

    States:
      - 'proven': slug proven against a local harness command
      - 'documented': slug documented in authoritative sources but unproven against local harness
      - 'none': no harness invocation slug
    """
    invocation = candidate.get("invocation_model_id")
    if not invocation:
        return "none"
    explicit = candidate.get("invocation_provenance")
    if explicit in ("proven", "documented", "none"):
        return explicit
    source = candidate.get("invocation_source")
    if not source:
        return "proven"
    source = str(source).strip()
    if source.startswith("documented") or source == "unproven":
        return "documented"
    if source.startswith("local-evidence") or source.startswith("proven"):
        return "proven"
    if source.startswith("unverified") or source == "none":
        return "none"
    return "proven"


def selection_disclosure(role: str, chosen: dict, preferred_seed, cost_policy: str) -> dict:
    """Build the durable, user-visible explanation for a selected route."""
    rank = preferred_rank(chosen, preferred_seed)
    reasons = ["cleared the applicable role policy, capability, technical and task-shape gates"]
    quota = chosen.get("quota", {})
    if quota.get("status") == "ok" and quota.get("tightest_remaining_percent") is not None:
        reasons.append("fit within the protected quota reserve")
    elif quota.get("status") != "ok" or quota.get("tightest_remaining_percent") is None:
        reasons.append("was selected with quota headroom explicitly unknown")
    if rank is not None:
        reasons.append(f"matched preferred seed #{rank + 1}, which decided the advisory ranking")
    else:
        reasons.append(f"won the {cost_policy} cost and local-evidence comparison")
    invocation = chosen.get("invocation_model_id")
    prov = invocation_provenance(chosen)
    if not invocation or prov == "none":
        # The catalog row carries no harness-specific slug, so the dispatch will
        # be attempted with the canonical model_id. Spec-seed names ("luna") are
        # not harness slugs ("gpt-5.6-luna"), so this fallback is the single
        # largest source of route-time dispatch failures. Say so in the
        # disclosure instead of letting it look like a resolved slug.
        reasons.append("carries no catalog invocation slug, so model_id is being used unverified")
    elif prov == "documented":
        reasons.append("catalog invocation slug is documented but unproven against local harness")
    return {
        "role": role,
        "model_id": chosen.get("model_id"),
        "invocation_model_id": invocation or chosen.get("model_id"),
        "invocation_model_id_source": "catalog" if invocation else "fallback:model_id",
        "invocation_provenance": prov,
        "effort": chosen.get("effort"),
        "harness": chosen.get("harness"),
        "harness_version": chosen.get("harness_version"),
        "triple": candidate_id(chosen),
        "reason": "; ".join(reasons),
    }





def _num(v, default=float("inf")):
    return default if v is None else float(v)


def _default_required_capabilities(role: str) -> list:
    """The caller passes the pinned role policy; absence means no requirement
    was pinned, which the packaged runtime never does (candidates.build_request)."""
    return []


def ensure_override_schema(con: sqlite3.Connection) -> None:
    """Creates `recorded_overrides` if not already present on `con`. No table for this
    is pinned in T0's contract (unlike `outcome_labels`, which has an explicit DDL in
    §7.3.3) and `office_runtime.py`'s `init_db` -- T2-owned -- does not create one, so
    this module owns the table's lifecycle end to end, the same way it owns
    `adapter_trust_acts` via `office_scoring.ensure_trust_schema`."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS recorded_overrides("
        "override_id TEXT PRIMARY KEY, run_id TEXT, family_id TEXT, task_id TEXT, "
        "role TEXT, candidate_id TEXT, bypass_stage INTEGER, rationale TEXT, "
        "authorized_by TEXT, authorized_at TEXT, expires_at TEXT)"
    )
    con.commit()


def record_override(db_path, record: dict) -> dict:
    """Logs a RecordedOverride (§7.5.1) into runs.db. §7.5.2 rule 5 requires the record
    be logged before it can authorize a bypass; `validate_override_record` below checks
    for exactly this row rather than trusting the in-request copy of the record."""
    con = sqlite3.connect(str(db_path))
    try:
        ensure_override_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO recorded_overrides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["override_id"], record.get("run_id"), record.get("family_id"),
                record.get("task_id"), record.get("role"), record.get("candidate_id"),
                record.get("bypass_stage"), record.get("rationale"),
                record.get("authorized_by"), record.get("authorized_at"),
                record.get("expires_at"),
            ),
        )
        con.commit()
    finally:
        con.close()
    return record


def _parse_datetime(value: Any):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_override_record(record: dict | None, request: dict, db_path=None) -> bool:
    """§7.5.2's validation rules, all of which must hold:
    1. authorized_by is strictly "user".
    2. expires_at is strictly in the future.
    3. family_id/task_id/role/candidate_id match the active routing request context.
    4. rationale is a substantive justification (>=10 non-whitespace characters).
    5. the record is logged into runs.db (checked by override_id, not re-trusted from
       the in-request copy).
    Returns False on any defect -- including a malformed record -- never raises; the
    shim's hard stop in `route()` treats "not valid" and "absent" identically.
    """
    if not isinstance(record, dict):
        return False
    if any(record.get(f) in (None, "") for f in _REQUIRED_OVERRIDE_FIELDS):
        return False
    if record["authorized_by"] != "user":
        return False
    if record["bypass_stage"] not in (2, 4, 7):
        return False
    if len(re.sub(r"\s+", "", str(record["rationale"]))) < 10:
        return False

    expires_at = _parse_datetime(record["expires_at"])
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        return False
    if _parse_datetime(record["authorized_at"]) is None:
        return False

    if record.get("role") != request.get("role"):
        return False
    if record.get("family_id") != request.get("family_id"):
        return False
    if record.get("task_id") != request.get("task_id"):
        return False
    if record.get("run_id") != request.get("run_id"):
        return False
    candidate_ids = {candidate_id(c) for c in request.get("candidates", [])}
    if record["candidate_id"] not in candidate_ids:
        return False

    if not db_path:
        return False
    con = sqlite3.connect(str(db_path))
    try:
        ensure_override_schema(con)
        row = con.execute(
            "SELECT 1 FROM recorded_overrides WHERE override_id = ?",
            (record["override_id"],),
        ).fetchone()
    finally:
        con.close()
    return row is not None


def _connect(db_path):
    if not db_path:
        return None
    con = sqlite3.connect(str(db_path))
    scoring.ensure_trust_schema(con)
    ensure_override_schema(con)
    return con


def _concurrency_snapshot(request: dict, con: sqlite3.Connection | None) -> None:
    """Overlay live counts for capped routes once, or honor a recorded snapshot on replay."""
    candidates = request.get("candidates", [])
    snapshot = request.get("concurrency_snapshot")
    if snapshot is None:
        snapshot = {}
        counts: dict[tuple[str, str], int] = {}
        if con is not None:
            for c in candidates:
                if c.get("max_concurrent") is None:
                    continue
                key = (str(c.get("harness") or ""),
                       str(c.get("invocation_model_id") or c.get("model_id") or ""))
                if not all(key) or key in counts:
                    continue
                counts[key] = con.execute(
                    "SELECT COUNT(*) FROM dispatches d JOIN runs r ON r.id=d.run_id "
                    "WHERE d.harness=? AND d.model=? AND d.ended_at IS NULL "
                    "AND d.status IN ('launching','running') "
                    "AND COALESCE(r.phase,'') NOT IN ('closed','abandoned','landed') "
                    "AND COALESCE(r.status,'') NOT IN ('closed','abandoned','landed')", key,
                ).fetchone()[0]
        for c in candidates:
            if c.get("max_concurrent") is not None:
                snapshot[candidate_id(c)] = counts.get(
                    (str(c.get("harness") or ""), str(c.get("invocation_model_id") or c.get("model_id") or "")), 0)
        request["concurrency_snapshot"] = snapshot
    for c in candidates:
        cap = c.get("max_concurrent")
        if cap is not None:
            concurrency = {"count": int(snapshot.get(candidate_id(c), 0)), "cap": int(cap)}
            c["concurrency"] = concurrency
            if concurrency["count"] >= concurrency["cap"]:
                c["quota"] = {
                    **(c.get("quota") or {}),
                    "status": "ok",
                    "tightest_remaining_percent": 0,
                    "cause": "concurrency-cap",
                    "concurrency": concurrency,
                }


def route(request: dict, discovery_input: dict | None = None, *,
          connection: sqlite3.Connection | None = None) -> dict:
    """Route one request. `discovery_input` (#494) is the preflight's recompute handle:
    {"candidate", "probe_key", "reservation_id", "attempt_id"} naming the one discovery
    candidate whose fresh exact probe record the request now carries. The recompute
    re-evaluates every gate against the request's current state, never draws again and
    never swaps in a different untried route. A supplied connection belongs to the
    caller; read its current evidence without committing or closing it."""
    discovery_input = discovery_input or request.get("discovery_input")
    executor_policy = _executor_policy_context.get() or executor_slates.load_policy()
    role = request["role"]
    playbook = request.get("playbook")
    policy = request.get("policy", {})
    if "required_capabilities" in request:
        required = set(request["required_capabilities"])
    elif "required_capabilities" in policy:
        required = set(policy["required_capabilities"])
    else:
        required = set(_default_required_capabilities(role))
    reserve = float(policy.get("quota_reserve_percent", DEFAULT_RESERVE_PERCENT))
    band_percent = float(policy.get("balanced_money_band_percent", DEFAULT_MONEY_BAND_PERCENT))
    cost_policy = request.get("cost_policy", policy.get("cost_policy", "balanced"))
    allow_advisory_undercut = bool(request.get("allow_advisory_undercut", True))
    preferred_seed = request.get("preferred_seed") or policy.get("preferred_seed")
    strict_seed = bool(policy.get("strict_seed"))  # only explicitly user-selected strict mode can restrict neutral
    floor = policy.get("floor")
    rejected = []
    stage = []

    db_path = request.get("runs_db")

    # Hard stop (§7.5.3): an override request without a valid, recorded authorization
    # halts before any candidate is evaluated. Pinned here (not only in the T2 shim)
    # because §7.5.3's title is literally "Hard Stop in route()".
    override_requested = bool(request.get("allow_unverified_override") or request.get("allow_override"))
    override_record = None
    if override_requested:
        candidate_override = request.get("recorded_override")
        if not validate_override_record(candidate_override, request, db_path=db_path):
            return {
                "selected": None,
                "status": "override_not_authorized",
                "reason": (
                    "Execution requested unverified override without a valid recorded "
                    "override authorization."
                ),
                "rejected": [],
            }
        override_record = candidate_override

    def override_applies(cid: str, stage_no: int) -> bool:
        return bool(
            override_record
            and override_record["candidate_id"] == cid
            and override_record["bypass_stage"] == stage_no
        )

    con = connection if connection is not None else _connect(db_path)
    try:
        _concurrency_snapshot(request, con)
        # 1 hard exclusions, then the user's denied and overkill policy (#494). Denial blocks
        # every role and every route, manual or automatic. Overkill skips a route only in
        # automatic selection and only where its role and size scope match.
        policies = request.get("user_policies") or []
        size_class = request.get("size_class") or (request.get("context") or {}).get("size_class")
        new_policy_size = executor_slates.new_gear_size(request, executor_policy)
        pool = []  # discovery candidates: never in the eligible slate, whatever the role
        role_states = {}
        # An explicit caller value wins; otherwise the user's consent decides, so a caller that
        # omits the key cannot silently drop an opted-in user's free routes.
        allow_free = request.get("allow_free_models")
        if allow_free is None:
            from office import onboarding
            allow_free = onboarding.free_models_enabled()
        for c in request.get("candidates", []):
            cid = candidate_id(c)
            role_rule = route_policy.effective_role_state(c, role, policies, new_policy_size)
            if role_rule["state"] == "deny" and (receipt_id := (request.get("policy_overrides") or {}).get(cid)):
                role_rule = {"state": "allow", "source": f"user override receipt {receipt_id}", "overrides": role_rule}
            role_states[cid] = role_rule
            if (scope := c.get("user_allow_sizes")) is not None and new_policy_size not in scope:
                # A size-scoped allow lifted the catalog disable for these sizes only (#850).
                rejected.append({"candidate": cid, "stage": 1, "reason": f"not dispatchable: the user's allow covers "
                                 f"task sizes {', '.join(scope)}, not {new_policy_size or 'an unknown size'}"})
                continue
            denied = role_rule.get("reason") or f"{role} policy deny by {role_rule['source']}: {role_rule.get('rule', cid)}" if role_rule["state"] == "deny" else None
            overkill = None if request.get("manual_route") else next(
                (why for pol in policies if (why := route_policy.is_overkill(c, role, size_class, pol))), None)
            if c.get("hard_excluded") or c.get("local_hard_excluded"):
                rejected.append({"candidate": cid, "stage": 1, "reason": "hard exclusion"})
            elif route_policy.is_free_model(c) and not allow_free:
                rejected.append({"candidate": cid, "stage": 1, "reason": "free model opt-in is disabled",
                                 "category": "free-model-opt-in"})
            elif denied:
                rejected.append({"candidate": cid, "stage": 1, "reason": denied,
                                 "category": "denied" if role_rule["source"] == "legacy-user-deny" else "role-deny",
                                 "policy": role_rule})
            elif overkill:
                rejected.append({"candidate": cid, "stage": 1, "reason": overkill, "category": "overkill"})
            elif (role in ADAPTIVE_ROLES and
                  (restriction := executor_slates.model_restriction(c, new_policy_size, executor_policy)) and
                  not (request.get("manual_route") and cid in
                       (request.get("declared_route"), request.get("exact_route")))):
                rejected.append({"candidate": cid, "stage": 1, "reason": restriction,
                                 "category": "size-model-restriction"})
            elif c.get("route_status", "available") != "available" and not (
                    c.get("route_status") == "probe-passed" and cid == request.get("declared_route")):
                # A route the user declared (--as, amend route) whose exact probe passed is a pinned route,
                # not a discovery candidate: stage 2 applies its trust rules and later stages still apply.
                pool.append(c)
            else:
                stage.append(c)

        # 2 role-policy admission (#837). Only a DENY above excludes automatically;
        # missing historical trust never vetoes neutral routes. Provenance and prior
        # failures remain available as evidence, not a shadow permission tier.
        adaptive_role = role in ADAPTIVE_ROLES and request.get("adaptive", True) is not False
        discovering = bool(adaptive_role and pool and (request.get("discovery") or {}).get("settings", {}).get("enabled"))
        if pool and not discovering:
            _reject_pool(pool, rejected, "discovery is not enabled for this role or run")
        learned = request.get("learned_eligibility") or {}
        eligibility: dict[str, dict] = {}
        for c in stage:
            cid = candidate_id(c)
            rule = role_states[cid]
            event = learned.get(route_learning.candidate_key(c)) or {}
            # Mature, replay-validated evidence influences selection preference;
            # it is not a second permission tier or an automatic policy deny.
            learned_allow = rule["state"] == "neutral" and event.get("state") == "learned-eligible"
            source = "learned" if learned_allow else ("declared" if cid == request.get("declared_route") else rule["source"])
            eligibility[cid] = {"policy": rule["state"], "source": source}
            if learned_allow:
                eligibility[cid]["event_id"] = event.get("event_id")

        # 3 required capabilities
        nxt = []
        for c in stage:
            cid = candidate_id(c)
            caps = set(c.get("capabilities", []))
            if _explicit_allow(role_states[cid]) and "vision" in required:
                caps.add("vision")  # the user's allow stands in for the vision proof (#845)
            if not required.issubset(caps):
                reason = f"missing capabilities {sorted(required - caps)}"
                if "vision" in required - caps:
                    reason += " (vision is proven per exact harness version and adapter: office doctor --probe-vision)"
                rejected.append({"candidate": cid, "stage": 3, "reason": reason})
                continue
            nxt.append(c)
        stage = nxt

        # 4 absolute role floor -- derived from `roles.<role>.floor` config against the
        # candidate's pinned catalog row (§7.2). A caller-supplied `absolute_floor_pass`
        # is read nowhere below.
        nxt = []
        for c in stage:
            cid = candidate_id(c)
            waiver = (executor_slates.floor_exception(c, new_policy_size, role, executor_policy)
                      if role in ADAPTIVE_ROLES else None)
            passed, reason = scoring.evaluate_capability_floor(c, floor, unbenchmarked_ok=bool(waiver))
            if _explicit_allow(role_states[cid]):
                nxt.append(c)  # a user allow is not held to a benchmark floor (#845)
                continue
            if not passed and not override_applies(cid, 4):
                rejected.append({"candidate": cid, "stage": 4, "reason": reason})
                continue
            size_reason = (executor_slates.benchmark_gate(c, new_policy_size, executor_policy)
                           if role in ADAPTIVE_ROLES and not waiver else None)
            if size_reason and not override_applies(cid, 4):
                rejected.append({"candidate": cid, "stage": 4, "reason": size_reason,
                                 "category": "subtask-intelligence-floor"})
                continue
            nxt.append(c)
        stage = nxt

        # 5 task shape
        nxt = []
        for c in stage:
            cid = candidate_id(c)
            supported = c.get("supported_playbooks")
            if supported and playbook and playbook not in supported:
                rejected.append({"candidate": cid, "stage": 5, "reason": "task shape unsupported"})
                continue
            nxt.append(c)
        stage = nxt

        # A dispatch-reported quota wall blocks the same harness invocation across
        # effort variants. Cooldowns live in shared runs.db across runs/processes.
        now = datetime.now(timezone.utc).isoformat()
        routes = [(cooldown_key(c.get("harness"), c.get("invocation_model_id") or c.get("model_id")), c)
                  for c in stage]
        demoted_keys = {id(c): _demotion_keys(c) for _, c in routes}
        rate_limited = {}
        if con and routes:
            has_cooldowns = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_cooldowns'"
            ).fetchone()
            if has_cooldowns:
                ids = list(dict.fromkeys([key for key, _ in routes] + [k for keys in demoted_keys.values() for k in keys]))
                placeholders = ",".join("?" for _ in ids)
                rate_limited = {row[0]: row[1] for row in con.execute(
                    f"SELECT route, expires_at FROM route_cooldowns WHERE expires_at>? AND route IN ({placeholders})",
                    (now, *ids))}
        for key, c in routes:
            if demoted := next((k for k in demoted_keys[id(c)] if k in rate_limited), None):
                c["quota"] = {**(c.get("quota") or {}), "cause": "route-demoted", "expires_at": rate_limited[demoted]}
            elif key in rate_limited:
                c["quota"] = {
                    **(c.get("quota") or {}),
                    "status": "ok",
                    "tightest_remaining_percent": 0,
                    "cause": "rate-limited",
                    "expires_at": rate_limited[key],
                }

        # 6 quota safety: unknown quota is scored conservatively downstream, not
        # excluded when a known-safe route is also available.
        from office import candidates as candidate_builder
        safe = []; unknown = []; unsafe = []
        for c in stage:
            gate = _quota_gate(c, reserve)
            if gate and gate[2] in {"concurrency-cap", "rate-limited", "route-demoted"}:
                category = "concurrency" if gate[2] == "concurrency-cap" else gate[2]
                rejected.append({"candidate": candidate_id(c), "stage": gate[0], "category": category,
                                 "reason": gate[1]})
                continue
            q = candidate_builder.quota_for_candidate(c); status = q.get("status", "unknown")
            if status != "ok" or q.get("tightest_remaining_percent") is None:
                unknown.append(c); continue
            remaining = float(q["tightest_remaining_percent"]); burn = float(q.get("projected_burn_percent") or 0)
            (safe if remaining - burn >= reserve else unsafe).append(c)
        if not (safe or unknown or unsafe):
            _reject_pool(pool, rejected, "no route survived the earlier gates, so none is left to select (a discovery trial would have no fallback)")
            return {"selected": None, "status": "no_qualifying_candidate", "rejected": rejected}
        if safe:
            for c in unsafe:
                rejected.append({"candidate": candidate_id(c), "stage": 6, "reason_class": "quota-reserve",
                                 "reason": "projected quota crosses reserve while safe alternative exists"})
            stage = safe + unknown
        elif unknown:
            for c in unsafe:
                rejected.append({"candidate": candidate_id(c), "stage": 6, "reason_class": "quota-reserve",
                                 "reason": "known quota crosses reserve; only unknown candidates remain"})
            stage = unknown
        else:
            _reject_pool(pool, rejected, "protected quota blocks every qualifying route")
            return {
                "selected": None, "status": "protected_quota_would_be_consumed", "rejected": rejected,
                "action": "choose a smaller/cheaper valid strategy, propose another route, or obtain explicit user authority",
            }

        if adaptive_role:
            return _adaptive(request, role, playbook, policy, stage, rejected, eligibility, learned,
                             preferred_seed, allow_advisory_undercut, override_applies, override_record,
                             pool=pool if discovering else [], discovery_input=discovery_input,
                             required=required, floor=floor, reserve=reserve,
                             executor_policy=executor_policy)

        # 7 advisory quality anchor. A caller-supplied per-candidate `advisory_pass` is
        # read nowhere below: retention is derived purely from `preferred_seed` match,
        # which is itself config/request data, never a candidate's self-assertion.
        if preferred_seed:
            matched = [c for c in stage if preferred_rank(c, preferred_seed) is not None]
            advisory = matched if matched else stage
            if stage and not matched and strict_seed and request.get("strict_seed_user_authorized") is True:
                # A strict seed is the whole review order: when every seeded route is excluded, tried or
                # unavailable, no unseeded route is substituted silently (bcf80833: a free flash route).
                for c in stage:
                    rejected.append({"candidate": candidate_id(c), "stage": 7,
                                     "reason": "outside the strict preferred_seed order for this role"})
                return {"selected": None, "status": "no_qualifying_candidate", "rejected": rejected,
                        "action": "every seeded route is unavailable; re-pin one: office review ... --as <route>"}
        else:
            advisory = stage
        if advisory and not allow_advisory_undercut:
            excluded = [c for c in stage if c not in advisory]
            kept = []
            for c in excluded:
                cid = candidate_id(c)
                if override_applies(cid, 7):
                    kept.append(c)
                else:
                    rejected.append({"candidate": cid, "stage": 7, "reason": "advisory anchor retained by gear/policy"})
            stage = advisory + kept

        # 8 cost, 9 local tie-break
        def quota_burn(c): return _num(c.get("cost", {}).get("quota_burn"))
        def money(c): return _num(c.get("cost", {}).get("money_estimate"))
        def wall(c): return _num(c.get("cost", {}).get("wall_clock_seconds"))

        reward_cache: dict[str, float | None] = {}

        def reward_key(c):
            cid = candidate_id(c)
            if cid not in reward_cache:
                reward_cache[cid] = scoring.compute_local_reward(con, cid) if con is not None else None
            return scoring.reward_sort_key(reward_cache[cid])

        if preferred_seed:
            default_rank = len(preferred_seed)
            stage.sort(key=lambda c: (
                preferred_rank(c, preferred_seed) if preferred_rank(c, preferred_seed) is not None else default_rank,
                quota_burn(c), money(c), wall(c), reward_key(c), candidate_id(c),
            ))
        elif cost_policy == "quota_saver":
            stage.sort(key=lambda c: (quota_burn(c), money(c), wall(c), reward_key(c), candidate_id(c)))
        elif cost_policy == "money_saver":
            stage.sort(key=lambda c: (money(c), quota_burn(c), wall(c), reward_key(c), candidate_id(c)))
        else:
            known_money = [money(c) for c in stage if math.isfinite(money(c))]
            if known_money:
                cheapest = min(known_money); band = cheapest * (1.0 + band_percent / 100.0)
                in_band = [c for c in stage if money(c) <= band]
                if in_band:
                    out_band = [c for c in stage if c not in in_band]
                    for c in out_band:
                        rejected.append({"candidate": candidate_id(c), "stage": 8, "reason": f"outside balanced {band_percent:g}% cheapest-money band"})
                    stage = in_band
                    stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), money(c), candidate_id(c)))
                else:
                    stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), money(c), candidate_id(c)))
            else:
                stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), candidate_id(c)))

        chosen = stage[0]
        cid = candidate_id(chosen)
        disclosure = selection_disclosure(role, chosen, preferred_seed, cost_policy)
        for stage_no in (2, 4, 7):
            if override_applies(cid, stage_no):
                disclosure["override"] = {
                    "override_id": override_record["override_id"],
                    "bypass_stage": stage_no,
                    "authorized_by": override_record["authorized_by"],
                    "rationale": override_record["rationale"],
                }
                break
        return {
            "selected": cid, "status": "selected", "candidate": chosen, "rejected": rejected,
            "selection_disclosure": disclosure,
            "decision_hash": sha256_obj({"role": role, "playbook": playbook, "selected": cid, "policy": policy, "candidate": chosen}),
        }
    finally:
        if connection is None and con is not None:
            con.close()


def _pool_category(c: dict) -> tuple[str, str]:
    """(category, reason) for a discovery candidate that is not being tried now."""
    probe = c.get("probe") or {}
    if probe.get("result") == "fail":
        return "probe-failed", f"exact probe failed: {probe.get('reason_class')}: {probe.get('detail') or ''}".rstrip(": ")
    if probe.get("result") == "pending":
        return "untried", "an exact probe is in flight"
    return "untried", c.get("status_reason") or "no fresh exact probe pass yet"


def _probe_fields(c: dict) -> dict:
    """The reason class of a failed exact probe, for the rejection of its route (#921)."""
    probe = c.get("probe") or {}
    return {"reason_class": probe.get("reason_class")} if probe.get("result") == "fail" else {}


def _reject_pool(pool: list[dict], rejected: list, why: str | None = None) -> None:
    """Every discovery candidate leaves the eligible slate with a category; none is dropped silently."""
    for c in pool:
        category, reason = _pool_category(c)
        rejected.append({"candidate": candidate_id(c), "stage": 2, "category": category,
                         "reason": f"{reason}; {why}" if why else reason, **_probe_fields(c)})


def _pool_gate(c: dict, required: set, floor, playbook, quarantined: set,
               *, policy_size: str | None = None, role: str | None = None,
               executor_policy: dict | None = None) -> tuple[int, str, str] | None:
    """The non-probe gates for a discovery candidate: (stage, reason, blocked token) or None.
    An unknown benchmark score is not a failure here (the scorer prices that uncertainty);
    a known low score, low effort, disallowed source, quarantine, missing capability and a
    quota that would cross the protected reserve all still reject."""
    cid = candidate_id(c)
    if cid in quarantined:
        return 2, "adapter trust is quarantined; a quarantined route never takes a trial", "quarantine"
    caps = set(c.get("capabilities", []))
    if not required.issubset(caps):
        return 3, f"missing capabilities {sorted(required - caps)}", "permission"
    passed, reason = scoring.evaluate_capability_floor(c, floor, unbenchmarked_ok=True)
    if not passed:
        return 4, reason, "floor"
    # New-gear discovery may trial an unknown (not normally dispatchable)
    # benchmark, but it must never trial a candidate with a *known* score below
    # the independently sized subtask floor, nor violate XL-only Opus.
    if policy_size:
        executor_policy = executor_policy or executor_slates.POLICY
        model_reason = executor_slates.model_restriction(c, policy_size, executor_policy)
        if model_reason:
            return 1, model_reason, "floor"
        known = (c.get("benchmark_indexes") or {}).get(executor_policy["benchmark_index"])
        if known is not None:
            # #861: a discovery probe may use the same exact M owner waiver,
            # but still cannot be dispatched without verified conformance.
            waiver = executor_slates.floor_exception(c, policy_size, role, executor_policy)
            task_reason = executor_slates.benchmark_gate(c, policy_size, executor_policy) if not waiver else None
            if task_reason:
                return 4, task_reason, "floor"
    supported = c.get("supported_playbooks")
    if supported and playbook and playbook not in supported:
        return 5, "task shape unsupported", "task-shape"
    return None


def _quota_gate(c: dict, reserve: float) -> tuple[int, str, str] | None:
    q = c.get("quota", {})
    if q.get("cause") == "rate-limited":
        return 6, "rate-limited", "rate-limited"
    if q.get("cause") == "route-demoted":
        return 6, f"demoted after a runtime failure until {q.get('expires_at')}", "route-demoted"
    if q.get("cause") == "concurrency-cap":
        concurrency = q.get("concurrency") or {}
        return (6, f"concurrency cap reached ({concurrency.get('count')}/{concurrency.get('cap')})",
                "concurrency-cap")
    if q.get("status", "unknown") == "ok" and q.get("tightest_remaining_percent") is not None:
        remaining, burn = float(q["tightest_remaining_percent"]), float(q.get("projected_burn_percent") or 0)
        if remaining - burn < reserve:
            return 6, "projected quota crosses the protected reserve", "quota"
    return None


def _discovery(request: dict, role: str, playbook, rec: dict, pool: list[dict], required: set, floor,
               reserve: float, discovery_input: dict | None, rejected: list,
               executor_policy: dict) -> tuple[dict, dict | None]:
    """Discovery allocation for one executor/worker decision (#494). Separate from exploration.

    Returns (block, trial). First call: at most one safe candidate wins a seeded draw and
    becomes `intent: "probe"`; the primary stays the known-working route. With
    `discovery_input` (the preflight recompute): the named candidate becomes
    `intent: "trial"` only if a fresh exact probe pass is on record and every gate still
    holds; otherwise it is dropped with the reason. A trial is never drawn twice and
    never swapped for another untried route.
    """
    d = request["discovery"]
    s, alloc = d["settings"], d["allocation"]
    known, quarantined = set(d.get("known_working") or []), set(d.get("quarantined") or [])
    risk = d.get("risk") or {}
    primary = rec["slate"][0]["route"]
    block = {"active": True, "intent": "none", "candidate": None, "probe_key": None, "probe": None,
             "caps": {k: dict(alloc[k]) for k in ("probes", "trials", "rolling")}, "fallback": None}
    if discovery_input:
        block["reservation_id"] = discovery_input.get("reservation_id")
        block["attempt_id"] = discovery_input.get("attempt_id")
    scoped = [c for c in pool if not discovery_input or candidate_id(c) == discovery_input.get("candidate")]

    def drop(c, blocked, *, category="untried", stage=2, reason=None):
        block["blocked"] = blocked
        rejected.append({"candidate": candidate_id(c), "stage": stage, "category": category,
                         "reason": reason or f"discovery: {blocked}", **_probe_fields(c)})

    def other(c):  # a candidate this decision is not trying
        category, reason = _pool_category(c)
        rejected.append({"candidate": candidate_id(c), "stage": 2, "category": category, "reason": reason,
                         **_probe_fields(c)})

    for c in pool:
        if c not in scoped:
            other(c)
    if discovery_input and not scoped:
        block["blocked"] = "candidate-gone"
        return block, None
    # Global gates: the same for every candidate.
    global_block = None
    if route_policy.trial_risk_blocked(s, risk, risk.get("task")):
        global_block = "risk"
    elif alloc["trials"]["used"] >= alloc["trials"]["max"]:
        global_block = "trial-cap"
    elif alloc["rolling"]["used"] >= alloc["rolling"]["max"]:
        global_block = "rolling-cap"
    elif s.get("require_known_fallback", True) and primary not in known:
        global_block = "no-fallback"
    elif rec["audit"].get("exploration", {}).get("active"):
        global_block = "exploration-active"
    if discovery_input and scoped:
        block.update(candidate=candidate_id(scoped[0]), probe_key=scoped[0].get("probe_key"),
                     probe=_probe_view(scoped[0].get("probe")))
    if global_block:
        block["blocked"] = global_block
        for c in scoped:
            category, reason = _pool_category(c)
            rejected.append({"candidate": candidate_id(c), "stage": 2, "category": category,
                             "reason": f"{reason}; discovery blocked: {global_block}"})
        return block, None
    survivors = []
    for c in scoped:
        probe = c.get("probe")
        gate = (_pool_gate(c, required, floor, playbook, quarantined,
                           policy_size=executor_slates.new_gear_size(request, executor_policy), role=role,
                           executor_policy=executor_policy)
                or _quota_gate(c, reserve))
        if gate:
            stage, why, token = gate
            drop(c, token, category="floor" if token == "floor" else "untried", stage=stage, reason=why)
            continue
        if discovery_input:
            if c.get("probe_key") != discovery_input.get("probe_key"):
                drop(c, "fingerprint-changed")
                continue
            if not probe:
                drop(c, "probe-missing")
                continue
            if probe.get("result") != "pass":
                token = "probe-pending" if probe.get("result") == "pending" else f"probe-failed:{probe.get('reason_class')}"
                drop(c, token, category="probe-failed" if probe.get("result") == "fail" else "untried")
                continue
        else:
            if probe and probe.get("result") in ("fail", "pending"):
                other(c)
                continue
            if not (probe and probe.get("result") == "pass") and alloc["probes"]["used"] >= alloc["probes"]["max"]:
                drop(c, "probe-cap")
                continue
        survivors.append(c)
    if not survivors:
        return block, None
    bounds = adaptive.trial_rows(survivors, rec, request)
    kept = []
    for c in survivors:
        b = bounds.get(candidate_id(c))
        if b is None:  # an alias row folded into the concrete row of the same invocation
            other(c)
            continue
        if b["reason"]:
            drop(c, "margin" if not b["within_margin"] else "cost" if not b["within_cost"] else "ceiling",
                 reason=f"discovery: {b['reason']}")
            continue
        kept.append((c, b))
    if not kept:
        return block, None
    if discovery_input:
        c, b = kept[0]
        probe = c["probe"]
        block.update(intent="trial", candidate=candidate_id(c), probe_key=c.get("probe_key"), probe=_probe_view(probe),
                     fallback=primary)
        return block, {"candidate": c, "bounds": b}
    seed = request.get("routing_seed") or ""
    rate = float(s["max_trial_percent_rolling_20"]) / 100.0
    draw = adaptive._draw(seed, "discovery")
    block["draw"] = round(draw, 6)
    block["rate"] = rate
    if draw >= rate:
        for c, _ in kept:
            other(c)
        return block, None
    kept.sort(key=lambda cb: candidate_id(cb[0]))
    c, b = kept[int(adaptive._draw(seed, "discovery-pick") * len(kept)) % len(kept)]
    for other_c, _ in kept:
        if other_c is not c:
            other(other_c)
    probe = c.get("probe")
    block.update(intent="probe", candidate=candidate_id(c), probe_key=c.get("probe_key"), probe=_probe_view(probe),
                 fallback=primary)
    rejected.append({"candidate": candidate_id(c), "stage": 2, "category": "probe-candidate",
                     "reason": "chosen for an exact conformance probe; not in the eligible slate until a fresh "
                               "exact probe passes"})
    return block, None


def _probe_view(probe: dict | None) -> dict | None:
    if not probe:
        return None
    return {"result": probe.get("result"), "reason_class": probe.get("reason_class"),
            "probed_at": probe.get("probed_at"), "fresh": bool(probe.get("fresh", True))}


def _adaptive(request, role, playbook, policy, stage, rejected, eligibility, learned, preferred_seed,
              allow_advisory_undercut, override_applies, override_record, *, pool=None, discovery_input=None,
              required=None, floor=None, reserve=5.0, executor_policy: dict) -> dict:
    """Stages 7-8 for executor/worker routes: learned eligibility, then the
    adaptive comparison (`office.adaptive`). Preference is weighted evidence; a
    gear that forbids advisory undercut still holds routes to its seed chain."""
    pool = pool or []
    if preferred_seed and not allow_advisory_undercut:
        matched = [c for c in stage if preferred_rank(c, preferred_seed) is not None]
        if matched:
            for c in stage:
                if c not in matched and not override_applies(candidate_id(c), 7):
                    rejected.append({"candidate": candidate_id(c), "stage": 7,
                                     "reason": "advisory anchor retained by gear/policy"})
            stage = [c for c in stage if c in matched or override_applies(candidate_id(c), 7)]
    nxt = []
    for c in stage:
        cid = candidate_id(c)
        event = learned.get(route_learning.candidate_key(c)) or {}
        if event.get("state") == "learned-ineligible" and not override_applies(cid, 7):
            rejected.append({"candidate": cid, "stage": 7,
                             "reason": f"learned ineligible from mature local evidence ({event.get('event_id')})"})
            continue
        nxt.append(c)
    if not nxt:
        _reject_pool(pool, rejected, "no qualifying known-working route, so a discovery trial would have no fallback")
        return {"selected": None, "status": "no_qualifying_candidate", "rejected": rejected, "slate": []}
    rec = adaptive.recommend(nxt, request, config={"routing": {"adaptive": request.get("adaptive_config") or {}}})
    rejected.extend(rec["rejected"])
    if not rec["slate"]:
        _reject_pool(pool, rejected, "no qualifying known-working route, so a discovery trial would have no fallback")
        return {"selected": None, "status": "no_qualifying_candidate", "rejected": rejected, "slate": []}
    audit = rec["audit"]
    by_id = dict(rec["by_id"])
    slate = rec["slate"]
    disc, trial = None, None
    active = bool((request.get("discovery") or {}).get("settings", {}).get("enabled"))
    if active and (pool or discovery_input):
        disc, trial = _discovery(request, role, playbook, rec, pool, set(required or ()), floor,
                                 float(reserve), discovery_input, rejected, executor_policy)
    elif active:
        disc = {"active": True, "intent": "none", "candidate": None, "probe_key": None, "probe": None,
                "caps": {k: dict(request["discovery"]["allocation"][k]) for k in ("probes", "trials", "rolling")},
                "fallback": None}
    audit["rejected"] = rejected
    size = executor_slates.new_gear_size(request, executor_policy)
    floor_exceptions = [{"candidate": candidate_id(c), **waiver} for c in stage
                        if role in ADAPTIVE_ROLES and (waiver := executor_slates.floor_exception(
                            c, size, role, executor_policy))]
    if floor_exceptions:
        audit["floor_exceptions"] = floor_exceptions
    if trial:
        c, b = trial["candidate"], trial["bounds"]
        tid = candidate_id(c)
        by_id[tid] = c
        fallback = next(e for e in slate if e["route"] == disc["fallback"])
        entry = {"rank": adaptive.RANK_LABELS[0], "route": tid, "label": adaptive.label(c), "utility": b["utility"],
                 "reason": f"discovery trial: a fresh exact conformance probe passed; falls back to {fallback['label']}",
                 "strength": "fresh exact probe pass (invocation conformance only)",
                 "weakness": "no local quality evidence yet"}
        rest = [e for e in slate if e is not fallback]
        slate = [entry, fallback, *rest][:max(adaptive.SLATE_SIZE, len(slate))]
        for i, e in enumerate(slate):
            slate[i] = {**e, "rank": adaptive.RANK_LABELS[i]}
        audit["candidates"] = [*audit["candidates"], {**b["row"], "rank": 0}]
        audit["slate"] = slate
        eligibility[tid] = {"policy": "neutral", "source": "trial", "probe_key": disc["probe_key"],
                            "attempt_id": disc.get("attempt_id")}
        # A discovery trial was not in the ordinary eligible stage. Persist its
        # exact benchmark waiver too; otherwise a selected trial would lack the
        # owner-authorized floor exception in its durable routing receipt.
        if waiver := executor_slates.floor_exception(c, size, role, executor_policy):
            audit.setdefault("floor_exceptions", []).append({"candidate": tid, **waiver})
    if disc:
        audit["discovery"] = disc
    for row in audit["candidates"]:
        row["eligibility"] = eligibility.get(row["route"]) or {"source": "factual gates"}
    chosen = by_id[slate[0]["route"]]
    cid = candidate_id(chosen)
    disclosure = selection_disclosure(role, chosen, preferred_seed, policy.get("cost_policy", "balanced"))
    if (executor_slates.new_gear_size(request, executor_policy) == "M" and not request.get("manual_route")
            and (chosen.get("alias_resolved_to") or chosen.get("model_id")) == "claude-sonnet-5-5"
            and chosen.get("effort") == "medium"):
        disclosure["operational_fallback"] = (
            "Temporary Sonnet Medium M fallback (#861); Haiku High is preferred only "
            "after exact harness/model/effort conformance and availability qualify")
        # Persist the explanation in route_audit as well as the dispatch payload.
        audit["operational_fallback"] = disclosure["operational_fallback"]
    head = slate[0]
    disclosure["reason"] = "; ".join(
        [disclosure["reason"].split("; ")[0], head["reason"], f"+ {head['strength']}", f"- {head['weakness']}"]
        + [p for p in disclosure["reason"].split("; ")[1:] if "slug" in p])
    disclosure["adaptive"] = True
    disclosure["slate"] = [{k: e[k] for k in ("rank", "route", "label", "reason", "strength", "weakness")}
                           for e in slate]
    if trial:
        disclosure["trial"] = {"fallback": disc["fallback"], "probe_key": disc["probe_key"]}
    for stage_no in (2, 4, 7):
        if override_applies(cid, stage_no):
            disclosure["override"] = {
                "override_id": override_record["override_id"], "bypass_stage": stage_no,
                "authorized_by": override_record["authorized_by"], "rationale": override_record["rationale"],
            }
            break
    hashed = {"role": role, "playbook": playbook, "policy": policy, "selected": cid,
              **({"task_descriptor": (request.get("context") or {}).get("task_descriptor")}
                 if (request.get("context") or {}).get("task_descriptor") else {}),
              "slate": [e["route"] for e in slate],
              "inputs": [{**{k: r[k] for k in ("route", "p_success", "cost_to_success",
                                                 "time_to_success_seconds", "quota", "preference", "utility")},
                          **({"concurrency": r["concurrency"]} if r.get("concurrency") else {})}
                         for r in audit["candidates"]],
              "clincher": audit["clincher"], "exploration": audit["exploration"],
              "policy_version": audit["policy_version"], "evidence_digest": audit["evidence_digest"]}
    if disc:
        # In the hash only when discovery is active, so discovery-off decisions hash exactly as before.
        hashed["discovery"] = disc
    if audit.get("lineage_supersession"):
        # Likewise only when a predecessor was superseded, so other decisions hash as before.
        hashed["lineage_supersession"] = audit["lineage_supersession"]
    decision_hash = sha256_obj(hashed)
    audit["decision_hash"] = decision_hash
    qualifying = [r["route"] for r in audit["candidates"]]
    return {
        "selected": cid, "status": "selected", "candidate": chosen, "rejected": rejected,
        "selection_disclosure": disclosure, "decision_hash": decision_hash,
        "slate": slate, "qualifying": qualifying,
        "qualifying_candidates": by_id,
        "routing": audit,
        **({"discovery": disc} if disc else {}),
    }
