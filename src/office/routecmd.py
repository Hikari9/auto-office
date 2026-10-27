"""User-facing role policy inspection/edits. No running Office run is required (#837).

Writes the *user* config only; never writes repo policy or mutable trust acts.
A user dispatch override is separately authenticated at dispatch time and is not
created by setting a persistent role rule.
"""
from __future__ import annotations

import json
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


def override(*, route: str | None, role: str | None, run: str | None, reason: str | None, ttl: str | None,
             revoke: str | None = None, list_receipts: bool = False) -> Result:
    """The user's one-off override of a role deny. Only a person at a terminal can issue one."""
    from office import db, route_override
    con = db.connect()
    try:
        if list_receipts:
            rows = route_override.list_receipts(con, run)
            lines = [f"{r['id']} run {r['run_id'][:8]} {r['role']} {r['route']} until {r['expires_at']}"
                     + (" (revoked)" if r["revoked_at"] else "") for r in rows] or ["no override receipts"]
            return Result(lines=lines, data={"receipts": [{k: v for k, v in r.items() if k != "mac"} for r in rows]})
        if revoke:
            ok = route_override.revoke(con, revoke)
            return Result(lines=[f"{revoke}: " + ("revoked" if ok else "not found or already revoked")], data={"revoked": ok})
        if not (route and role and run):
            raise configcmd._usage("override needs <route>, --role and --run")
        receipt = route_override.issue(con, run_id=run, role=role, route=route, reason=reason or "",
                                       ttl_s=route_override.parse_ttl(ttl))
    finally:
        con.close()
    return Result(lines=[f"{receipt['id']}: {receipt['role']} may run {receipt['route']} in run {receipt['run_id'][:8]} "
                         f"until {receipt['expires_at']}. Policy is unchanged; capability, sandbox, independence and "
                         "landing checks still apply."],
                  next=f"the orchestrator re-dispatches with --as/--review-as {receipt['route']}",
                  data={"receipt": {k: v for k, v in receipt.items() if k != "mac"}})


_MIGRATION_BACKUP = ".pre-837-migrate.bak"
_MIGRATION_ADDED = ".837-added.json"


def migrate(*, apply: bool = False, rollback: bool = False) -> Result:
    """Dry-run by default: what #837 changes for existing settings and which routes stop being trust-blocked.

    `--apply` rewrites only the user config: each legacy global `denied_models` entry also becomes an
    explicit all-role deny rule (same meaning, now editable per role). Nothing is removed, so rollback
    is the backup copy. Historical trust acts, runs and pinned policies are never rewritten."""
    import shutil
    path = configcmd._tier_path("user", Path.cwd())
    backup = path.with_name(path.name + _MIGRATION_BACKUP)
    added_file = path.with_name(path.name + _MIGRATION_ADDED)
    if rollback:
        if not added_file.is_file():
            raise configcmd._usage(f"no migration record at {added_file}; the full pre-migration copy is {backup}")
        added = json.loads(added_file.read_text(encoding="utf-8"))
        raw = configcmd._load(path)
        up = raw.setdefault("routing", {}).setdefault("user_policy", {})
        keep = [r for r in up.get("role_rules") or [] if r not in added]
        removed = len(up.get("role_rules") or []) - len(keep)
        up["role_rules"] = keep
        configcmd._validate(raw, "user", Path.cwd())
        configcmd._write(path, raw)
        added_file.unlink()
        return Result(lines=[f"removed {removed} migrated rule(s) from {path}; other settings are untouched"],
                      data={"rolled_back": True, "removed": removed})
    raw = configcmd._load(path)
    user_policy = ((raw.get("routing") or {}).get("user_policy") or {})
    legacy = [d for d in (user_policy.get("denied_models") or []) if isinstance(d, str)]
    existing = {(r.get("role"), r.get("route")) for r in user_policy.get("role_rules") or [] if isinstance(r, dict)}
    pending = [d for d in dict.fromkeys(legacy) if ("*", d) not in existing]  # de-duplicated, order kept
    lines = [f"legacy global denied_models: {len(legacy)} (all roles, still enforced); {len(pending)} not yet explicit role rules"]
    lines += [f"  deny * {d}  (wildcard, every role)" for d in pending]
    data = {"legacy_denied": legacy, "pending": pending, "applied": False}
    trust = _legacy_trust_diff()
    lines += trust["lines"]
    data["trust_diff"] = trust["data"]
    if apply and pending:
        routing = raw.setdefault("routing", {})
        up = routing.setdefault("user_policy", {})
        rules = list(up.get("role_rules") or [])
        new_rules = [{"role": "*", "route": d, "state": "deny", "wildcard": True} for d in pending]
        rules += new_rules
        up["role_rules"] = rules
        configcmd._validate(raw, "user", Path.cwd())
        if path.is_file():
            shutil.copy2(path, backup)
        configcmd._write(path, raw)
        prior = json.loads(added_file.read_text(encoding="utf-8")) if added_file.is_file() else []
        added_file.write_text(json.dumps(prior + new_rules), encoding="utf-8")
        lines.append(f"applied; undo with `office route migrate --rollback` (removes only these rules; full copy {backup})")
        data["applied"] = True
    elif not apply:
        lines.append("dry run: nothing written. `office route migrate --apply` writes the rules above.")
    return Result(lines=lines, next="office route policy --effective --role code_reviewer", data=data)


def _legacy_trust_diff() -> dict:
    """Routes the old trust gate blocked that role policy now admits. Read-only; audit rows untouched."""
    from office import db, routing as routing_mod
    out = {"lines": [], "data": {"newly_eligible": 0, "quarantined_by_user": []}}
    try:
        con = db.connect()
    except Exception:  # no runs.db yet: nothing historical to compare
        return out
    try:
        scoring.ensure_trust_schema(con)
        acts = con.execute("SELECT triple, target_state, actor_id FROM adapter_trust_acts").fetchall()
        out["lines"].append(f"historical trust acts: {len(acts)} (audit only; never converted into a deny)")
        quarantined = sorted({a["triple"] for a in acts if a["target_state"] == "quarantined" and a["actor_id"] == "user"})
        for triple in quarantined:
            out["lines"].append(f"  you once quarantined {triple}: keep it denied with `office route deny <route> --role <role>`")
        out["data"]["quarantined_by_user"] = quarantined
        blocked = 0
        for role in sorted(routing_mod.MUTABLE_TRUST_ROLES):
            cands, _ = candidates.build_candidates(con, role, probe=False, user_policies=[])
            for c in cands:
                if scoring.evaluate_trust_state(con, routing_mod.candidate_id(c))[1] != "proven":
                    blocked += 1
        out["data"]["newly_eligible"] = blocked
        out["lines"].append(f"routes the old trust gate blocked that are now neutral/eligible: {blocked} (role x route pairs)")
    finally:
        con.close()
    return out
