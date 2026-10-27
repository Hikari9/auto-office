"""User-facing role policy inspection/edits. No running Office run is required (#837).

Writes the *user* config only; never writes repo policy or mutable trust acts.
A user dispatch override is separately authenticated at dispatch time and is not
created by setting a persistent role rule.
"""
from __future__ import annotations

from pathlib import Path

from office import adapters, candidates, config as cfg, configcmd, route_policy, scoring
from office.result import Result


def _require_route(spec: str | None, *, wildcard: bool) -> None:
    if not isinstance(spec, str) or not spec.strip():
        raise configcmd._usage("route is required: harness/model@effort or harness@major/model@effort")
    if not (route_policy._valid_spec(spec) or route_policy._canonical_spec(spec)):
        raise configcmd._usage(f"invalid route: {spec!r}")
    if not route_policy._is_exact_spec(spec) and not wildcard:
        raise configcmd._usage("broad route requires explicit --wildcard")


def _canonicalize(spec: str) -> str:
    """Pin known short aliases to one concrete harness-major/model/effort identity.

    Unknown exact invocations are kept as written so a user can provision policy
    before a new route enters the catalog; no guessed provider/lineage is used.
    """
    if route_policy._canonical_spec(spec) or not route_policy._is_exact_spec(spec):
        return spec
    harness, name_effort = spec.split("/", 1)
    model, effort = name_effort.split("@", 1)
    sources = adapters.load_sources()
    if harness not in sources:
        return spec
    matches = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == harness
               and r.get("effort") == effort
               and model in (r.get("model_id"), r.get("invocation_model_id"))]
    identities = {r.get("alias_resolved_to") or r.get("model_id") for r in matches}
    if len(identities) != 1:
        return spec  # ambiguous or future route: never guess which model the user intended
    version = adapters.route_version(harness, sources[harness][0], sources)
    major = scoring.harness_major(version)
    target = next(iter(identities))
    # The sandbox or a first install may not have a harness executable. Never
    # fabricate its version or write an invalid `@unknown` canonical identity.
    return f"{harness}@{major}/{target}@{effort}" if major.isdigit() else f"{harness}/{target}@{effort}"


def policy(*, role: str | None = None) -> Result:
    if role and role not in route_policy.POLICY_ROLES:
        raise configcmd._usage(f"unknown routing role {role!r}")
    effective, _ = cfg.resolve(configcmd._repo_root(Path.cwd()))
    p = route_policy.user_policy(effective)
    sources = adapters.load_sources()
    roles = (role,) if role else tuple(sorted(route_policy.POLICY_ROLES))
    output = []
    lines = ["effective role routing policy (#837); availability and technical qualification are separate:"]
    unknown_versions = set()
    for r in roles:
        count = 0
        for row in candidates.catalog_rows():
            harness = row.get("invocation_harness")
            if harness not in sources or not row.get("model_id") or not row.get("effort"):
                continue
            adapter = sources[harness][0]
            version = adapters.route_version(harness, adapter, sources)
            if not scoring.harness_major(version).isdigit():
                unknown_versions.add(harness)
            c = {"harness": harness, "harness_version": version,
                 "model_id": row["model_id"], "invocation_model_id": row.get("invocation_model_id"),
                 "alias_resolved_to": row.get("alias_resolved_to"), "effort": row["effort"]}
            state = route_policy.effective_role_state(c, r, [p])
            result = {"role": r, "route": f"{harness}/{row['model_id']}@{row['effort']}",
                      "state": state["state"], "source": state["source"], "technical_dispatchable": row.get("dispatchable") is not False}
            output.append(result)
            if role is not None:
                lines.append(f"  {result['route']}: {result['state']} ({result['source']})"
                             + (" [technically not dispatchable]" if not result["technical_dispatchable"] else ""))
            count += 1
        if role is None:
            lines.append(f"  {r}: {count} catalog rows (use --role to inspect effective states)")
    if unknown_versions:
        lines.append("warning: harness major unknown for " + ", ".join(sorted(unknown_versions))
                     + "; exact shipped major-scoped allowances cannot be resolved until installed/probed")
    lines.append("legacy global denied_models and version ceilings remain enforced unless explicitly superseded by a user role allow")
    return Result(lines=lines, data={"policy": p, "effective": output})


def change(action: str, *, role: str | None, route: str | None, wildcard: bool = False) -> Result:
    """Transactional-in-effect YAML edit: validate all layers BEFORE atomic file write."""
    if role not in route_policy.POLICY_ROLES and not (role == "*" and wildcard):
        raise configcmd._usage(f"a specific --role is required (known: {', '.join(sorted(route_policy.POLICY_ROLES))})")
    _require_route(route, wildcard=wildcard)
    original_spec = route
    route = _canonicalize(route)
    if action not in (*route_policy.ROLE_STATES, "clear"):
        raise configcmd._usage(f"invalid action {action!r}")
    path = configcmd._tier_path("user", Path.cwd())
    raw = configcmd._load(path)
    routing = raw.setdefault("routing", {})
    if not isinstance(routing, dict):
        raise configcmd._usage("user routing config must be a mapping")
    user_policy = routing.setdefault("user_policy", {})
    if not isinstance(user_policy, dict):
        raise configcmd._usage("user routing policy must be a mapping")
    old = user_policy.get("role_rules", [])
    if not isinstance(old, list):
        raise configcmd._usage("existing user role_rules must be a list; refusing to overwrite")
    new = [r for r in old if not (isinstance(r, dict) and r.get("role") == role and r.get("route") in (route, original_spec))]
    if action != "clear":
        new.append({"role": role, "route": route, "state": action, **({"wildcard": True} if wildcard else {})})
    if new == old:
        return Result(lines=[f"{role}/{route}: no policy change"], data={"changed": False})
    user_policy["role_rules"] = new
    configcmd._validate(raw, "user", Path.cwd())
    backup = configcmd._write(path, raw)
    lines = [f"{role}/{route}: {'cleared user rule' if action == 'clear' else action} (user config)"]
    if backup:
        lines.append(f"commented source backed up at {backup}")
    return Result(lines=lines, next="office route policy --effective --role " + (role if role != "*" else "code_reviewer"),
                  data={"changed": True, "role": role, "route": route, "action": action, "path": str(path)})
