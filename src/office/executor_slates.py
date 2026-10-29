"""Issue #809: version-pinned size floors and advisory ideal executor routes.

This is not a registry. Preferred routes remain subject to catalog, trust,
capability, quota, discovery, and exact invocation qualification gates.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from string import Formatter

import yaml

from office import paths, shipped_catalog

SIZES = ("XS", "S", "M", "L", "XL")
EFFORTS = frozenset(("none", "low", "medium", "high", "xhigh", "max"))


class ExecutorPolicyError(ValueError):
    """Invalid or unresolved executor policy; routing must fail closed."""


def _catalog_tuple(catalog: dict, model_id: str, effort: str, harness: str | None,
                   source: Path, scope: str) -> tuple[list[str], list[str]]:
    known = [row for row in catalog.get("models", []) if row.get("model_id") == model_id and
             row.get("effort") == effort and
             (harness is None or row.get("invocation_harness") == harness)]
    key = (model_id, effort, harness)
    if not known:
        raise ExecutorPolicyError(f"{source}: {scope} references nonexistent catalog tuple {key}")
    resolved = [shipped_catalog.key(row) for row in known]
    dispatchable = [shipped_catalog.key(row) for row in known if row.get("dispatchable") is not False]
    return resolved, dispatchable


def load_policy(root: Path | None = None) -> dict:
    """Load and resolve policy against shipped catalog facts in the same snapshot."""
    source = (Path(root) if root is not None else paths.resources_root()) / "catalog/executor-policy.yaml"
    try:
        raw = source.read_bytes()
        data = yaml.safe_load(raw)
        catalog = shipped_catalog.load(root)
    except (OSError, yaml.YAMLError, shipped_catalog.CatalogError) as exc:
        raise ExecutorPolicyError(f"{source}: cannot load executor policy/catalog: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ExecutorPolicyError(f"{source}: unsupported executor-policy schema")
    for name in ("policy_version", "benchmark_index"):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise ExecutorPolicyError(f"{source}: {name} must be a nonempty string")
    floors = data.get("floors")
    if not isinstance(floors, dict) or set(floors) != set(SIZES) or any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in floors.values()):
        raise ExecutorPolicyError(f"{source}: floors must specify finite nonnegative values for XS-XL")
    for field, sizes in (("executor_preferred", SIZES), ("planner_preferred", ("M", "L", "XL"))):
        slates = data.get(field)
        if not isinstance(slates, dict) or set(slates) != set(sizes):
            raise ExecutorPolicyError(f"{source}: {field} must provide exactly {', '.join(sizes)}")
        for size, entries in slates.items():
            if not isinstance(entries, list) or not entries:
                raise ExecutorPolicyError(f"{source}: {field}.{size} must be nonempty")
            seen = set()
            for item in entries:
                if (not isinstance(item, dict) or not {"model_id", "effort"} <= set(item) or
                        set(item) - {"model_id", "effort", "harness"} or
                        not isinstance(item.get("model_id"), str) or not item["model_id"].strip() or
                        not isinstance(item.get("effort"), str) or item["effort"] not in EFFORTS or
                        (item.get("harness") is not None and (not isinstance(item["harness"], str) or
                                                              not item["harness"].strip()))):
                    raise ExecutorPolicyError(f"{source}: invalid {field}.{size} route")
                key = (item["model_id"], item["effort"], item.get("harness"))
                if key in seen:
                    raise ExecutorPolicyError(f"{source}: duplicate {field}.{size} route {key}")
                seen.add(key)
                item["_resolved_routes"], item["_dispatchable_routes"] = _catalog_tuple(
                    catalog, item["model_id"], item["effort"], item.get("harness"), source, f"{field}.{size}")
    restrictions = data.get("model_restrictions")
    if not isinstance(restrictions, list):
        raise ExecutorPolicyError(f"{source}: model_restrictions must be a list")
    catalog_models = {row.get("model_id") for row in catalog.get("models", [])}
    for r in restrictions:
        if (not isinstance(r, dict) or not isinstance(r.get("model_id"), str) or
                not isinstance(r.get("reason"), str) or
                (("allowed_sizes" in r) == ("allowed_harnesses" in r))):
            raise ExecutorPolicyError(f"{source}: invalid model restriction")
        if r["model_id"] not in catalog_models:
            raise ExecutorPolicyError(f"{source}: model restriction references unknown catalog model {r['model_id']!r}")
        try:
            fields = {field for _, field, _, _ in Formatter().parse(r["reason"]) if field is not None}
            if not fields <= {"size"}:
                raise ValueError("unsupported format field")
            r["reason"].format(size="XS")
        except (IndexError, KeyError, ValueError) as exc:
            raise ExecutorPolicyError(f"{source}: invalid model restriction reason template") from exc
        if "allowed_sizes" in r and (not isinstance(r["allowed_sizes"], list) or any(x not in SIZES for x in r["allowed_sizes"])):
            raise ExecutorPolicyError(f"{source}: invalid allowed_sizes")
        if "allowed_harnesses" in r and (not isinstance(r["allowed_harnesses"], list) or any(not isinstance(x, str) or not x for x in r["allowed_harnesses"])):
            raise ExecutorPolicyError(f"{source}: invalid allowed_harnesses")
    exceptions = data.get("floor_exceptions")
    if not isinstance(exceptions, list):
        raise ExecutorPolicyError(f"{source}: floor_exceptions must be a list")
    for x in exceptions:
        if (not isinstance(x, dict) or any(not isinstance(x.get(k), str) or not x[k] for k in
                ("name", "model_id", "effort", "size", "role", "score_evidence", "authorization")) or
                x["size"] not in SIZES or x["effort"] not in EFFORTS or x["role"] != "executor" or
                x["score_evidence"] not in ("missing", "numeric-below-floor") or
                not isinstance(x.get("not_waived"), list) or not x["not_waived"] or
                any(not isinstance(item, str) or not item for item in x["not_waived"]) or
                (x.get("harness") is not None and (not isinstance(x["harness"], str) or not x["harness"]))):
            raise ExecutorPolicyError(f"{source}: invalid floor exception")
        if x["score_evidence"] == "numeric-below-floor" and (
                type(x.get("minimum_score")) not in (int, float) or not math.isfinite(x["minimum_score"]) or
                x["minimum_score"] >= floors[x["size"]]):
            raise ExecutorPolicyError(f"{source}: numeric exception must have a finite below-floor minimum")
        x["_resolved_routes"], x["_dispatchable_routes"] = _catalog_tuple(
            catalog, x["model_id"], x["effort"], x.get("harness"), source, f"floor_exceptions.{x['name']}")
    data["_provenance"] = {"source": str(source), "sha256": hashlib.sha256(raw).hexdigest()}
    data["_catalog"] = catalog
    return data


POLICY = load_policy()


def __getattr__(name: str):
    """Legacy read compatibility; these views are derived from the loaded policy, never stored tables."""
    if name == "INDEX":
        return POLICY["benchmark_index"]
    if name == "FLOORS":
        return POLICY["floors"]
    if name == "VERSION":
        return POLICY["policy_version"]
    if name == "IDEAL":
        return {size: tuple((row["model_id"], row["effort"], row.get("harness")) for row in rows)
                for size, rows in POLICY["executor_preferred"].items()}
    if name == "PLANNER_BY_GEAR":
        return {size: tuple((row["model_id"], row["effort"], row.get("harness")) for row in rows)
                for size, rows in POLICY["planner_preferred"].items()}
    if name == "FLOOR_EXCEPTION":
        return next((dict(row) for row in POLICY["floor_exceptions"] if row["score_evidence"] == "missing"), {})
    raise AttributeError(name)


def policy_provenance(policy: dict = POLICY) -> dict:
    return {**policy["_provenance"], "policy_version": policy["policy_version"],
            "benchmark_index": policy["benchmark_index"]}


def policy_for(request: dict) -> dict:
    """Return the one validated snapshot carried through this routing decision."""
    return request.get("_executor_policy") or POLICY


def _seed_entry(row: dict) -> dict:
    return {key: row[key] for key in ("model_id", "effort", "harness") if row.get(key) is not None}


def planner_seed(gear: str | None, policy: dict = POLICY) -> list[dict]:
    return [_seed_entry(row) for row in policy["planner_preferred"].get(gear, ())]


def new_gear_size(request: dict, policy: dict = POLICY) -> str | None:
    """New XS-XL funding is not retroactively imposed on legacy pinned runs."""
    context = request.get("context") or {}
    gear = context.get("gear") or request.get("run_gear")
    # Raw routing callers, replay fixtures, and historical requests without a
    # declared run gear are NOT implicitly upgraded to #809 policy.
    if gear not in policy["floors"]:
        return None
    size = request.get("size_class") or context.get("size_class")
    return size if size in policy["floors"] else None


def ideal_seed(task_size: str | None, policy: dict = POLICY) -> list[dict]:
    return [_seed_entry(row) for row in policy["executor_preferred"].get(task_size, ())]


def model_restriction(candidate: dict, size: str | None, policy: dict = POLICY) -> str | None:
    """Automatic-only model restrictions; explicit user routes have own gates."""
    if size not in policy["floors"]:
        return None
    model = candidate.get("alias_resolved_to") or candidate.get("model_id") or ""
    for restriction in policy["model_restrictions"]:
        if restriction["model_id"] != model:
            continue
        if "allowed_sizes" in restriction and size not in restriction["allowed_sizes"]:
            return restriction["reason"].format(size=size)
        if "allowed_harnesses" in restriction and candidate.get("harness") not in restriction["allowed_harnesses"]:
            return restriction["reason"].format(size=size)
    return None


def benchmark_gate(candidate: dict, size: str | None, policy: dict = POLICY) -> str | None:
    """Fail closed on missing, incompatible, malformed or below-floor scores."""
    if size not in policy["floors"]:
        return None  # historical runs lacking task size retain their old role floor
    scores = candidate.get("benchmark_indexes") or {}
    index, floor = policy["benchmark_index"], policy["floors"][size]
    raw = scores.get(index) if isinstance(scores, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return f"{size} subtask requires numeric {index} >= {floor}; exact-effort benchmark missing"
    if not math.isfinite(raw) or raw < floor:
        return f"{size} subtask requires {index} >= {floor} (got {raw})"
    return None


def floor_exception(candidate: dict, size: str | None, role: str | None = None,
                    policy: dict = POLICY) -> dict | None:
    """Apply only exact floor exceptions resolved from policy data."""
    model = candidate.get("alias_resolved_to") or candidate.get("model_id")
    scores = candidate.get("benchmark_indexes") or {}
    index = policy["benchmark_index"]
    raw = scores.get(index) if isinstance(scores, dict) else None
    for rule in policy["floor_exceptions"]:
        if (size != rule["size"] or role not in (rule["role"], "worker") or model != rule["model_id"] or
                candidate.get("effort") != rule["effort"] or
                ("harness" in rule and candidate.get("harness") != rule["harness"])):
            continue
        if rule["score_evidence"] == "numeric-below-floor":
            if type(raw) not in (int, float) or not math.isfinite(raw) or raw < rule["minimum_score"] or raw >= policy["floors"][size]:
                continue
        elif isinstance(scores, dict) and scores.get(index) is not None:
            continue
        return {"exception": rule["name"], "model_id": model, "effort": rule["effort"], "size": size,
                "index": index, **({"harness": rule["harness"]} if "harness" in rule else {}),
                **({"observed_score": raw} if rule["score_evidence"] == "numeric-below-floor" else {}),
                "waived": f"{index} >= {policy['floors'][size]} ({rule['authorization']})",
                "not_waived": list(rule["not_waived"])}
    return None


def route_summary(preferences: list[dict], candidates: list[dict],
                  rejected: list[dict], slate: list[dict]) -> list[dict]:
    """Audit every nominated ideal without claiming unavailable routes dispatched."""
    from office import routing
    rejected_by_id = {r.get("candidate"): r.get("reason") for r in rejected}
    selected = {entry.get("route"): entry.get("rank") for entry in slate}
    out = []
    for rank, preference in enumerate(preferences):
        matches = [c for c in candidates if routing.preferred_rank(c, [preference]) is not None]
        ref = (preference.get("harness") or "*") + "/" + preference["model_id"] + "@" + preference["effort"]
        failures = [{"route": routing.candidate_id(c), "reason": rejected_by_id[routing.candidate_id(c)]}
                    for c in matches if routing.candidate_id(c) in rejected_by_id]
        qualifying = [c for c in matches if routing.candidate_id(c) not in rejected_by_id
                      and c.get("route_status", "available") == "available"]
        status = ("selected" if any(selected.get(routing.candidate_id(c)) == "PRIMARY" for c in qualifying)
                  else "in-slate" if any(routing.candidate_id(c) in selected for c in qualifying)
                  else "qualified-outscored" if qualifying else "unqualified" if matches else "not-in-catalog")
        out.append({"ideal_rank": rank + 1, "route": ref, "status": status,
                    "qualified_routes": [routing.candidate_id(c) for c in qualifying],
                    "rejections": failures,
                    "reason": ("adaptive fit may change ideal order" if qualifying else
                               "exact route is missing or rejected by catalog, trust, quota, or policy")})
    return out


def skip_lines(ideal: list[dict] | None) -> list[str]:
    """One line per nominated ideal route that did not win, with why: a receipt never implies it ran."""
    out = []
    for entry in ideal or []:
        if entry.get("status") == "selected":
            continue
        reasons = [f"{r['route']}: {r['reason']}" for r in entry.get("rejections") or []]
        why = "; ".join(reasons) if reasons else entry.get("reason") or entry.get("status")
        out.append(f"#{entry['ideal_rank']} {entry['route']} ({entry['status']}): {why}")
    return out
