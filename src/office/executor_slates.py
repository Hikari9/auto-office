"""Issue #809: version-pinned size floors and advisory ideal executor routes.

This is not a registry. Preferred routes remain subject to catalog, trust,
capability, quota, discovery, and exact invocation qualification gates.
"""
from __future__ import annotations

import math

from pathlib import Path
import hashlib
import yaml

from office import paths

SIZES = ("XS", "S", "M", "L", "XL")
EFFORTS = frozenset(("none", "low", "medium", "high", "xhigh", "max"))


class ExecutorPolicyError(ValueError):
    """Invalid shipped size/slate policy: fail closed instead of dispatching."""


def load_policy(root: Path | None = None) -> dict:
    """Load exactly the deployed policy, or an isolated root for policy-contract tests.

    Never use the test suite as a fallback routing-policy registry.
    """
    source = (Path(root) if root is not None else paths.resources_root()) / "catalog/executor-policy.yaml"
    try:
        raw = source.read_bytes()
        policy = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise ExecutorPolicyError(f"{source}: cannot load executor policy: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ExecutorPolicyError(f"{source}: unsupported executor-policy schema")
    for field in ("policy_version", "benchmark_index"):
        if not isinstance(policy.get(field), str) or not policy[field].strip():
            raise ExecutorPolicyError(f"{source}: {field} must be a nonempty string")
    floors = policy.get("floors")
    if not isinstance(floors, dict) or set(floors) != set(SIZES) or any(
        type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in floors.values()
    ):
        raise ExecutorPolicyError(f"{source}: floors must specify finite nonnegative values for XS-XL")
    for field, sizes in (("executor_preferred", SIZES), ("planner_preferred", ("M", "L", "XL"))):
        slates = policy.get(field)
        if not isinstance(slates, dict) or set(slates) != set(sizes):
            raise ExecutorPolicyError(f"{source}: {field} must provide exactly {', '.join(sizes)}")
        for size, entries in slates.items():
            if not isinstance(entries, list) or not entries:
                raise ExecutorPolicyError(f"{source}: {field}.{size} must be nonempty")
            observed = set()
            for item in entries:
                if not isinstance(item, dict) or not {"model_id", "effort"} <= set(item) or (
                    set(item) - {"model_id", "effort", "harness"}
                ) or not isinstance(item["model_id"], str) or not item["model_id"].strip() or (
                    item["effort"] not in EFFORTS
                ) or (item.get("harness") is not None and (
                    not isinstance(item["harness"], str) or not item["harness"].strip()
                )):
                    raise ExecutorPolicyError(f"{source}: invalid {field}.{size} route")
                key = (item["model_id"], item["effort"], item.get("harness"))
                if key in observed:
                    raise ExecutorPolicyError(f"{source}: duplicate {field}.{size} route {key}")
                observed.add(key)
    restrictions = policy.get("model_restrictions")
    if not isinstance(restrictions, list):
        raise ExecutorPolicyError(f"{source}: model_restrictions must be a list")
    for r in restrictions:
        if not isinstance(r, dict) or not isinstance(r.get("model_id"), str) or not isinstance(
            r.get("reason"), str
        ) or not (("allowed_sizes" in r) != ("allowed_harnesses" in r)):
            raise ExecutorPolicyError(f"{source}: invalid model restriction")
        if "allowed_sizes" in r and (not isinstance(r["allowed_sizes"], list) or any(
            size not in SIZES for size in r["allowed_sizes"]
        )):
            raise ExecutorPolicyError(f"{source}: invalid allowed_sizes")
        if "allowed_harnesses" in r and (not isinstance(r["allowed_harnesses"], list) or any(
            not isinstance(h, str) or not h for h in r["allowed_harnesses"]
        )):
            raise ExecutorPolicyError(f"{source}: invalid allowed_harnesses")
    exceptions = policy.get("floor_exceptions")
    if not isinstance(exceptions, list):
        raise ExecutorPolicyError(f"{source}: floor_exceptions must be a list")
    seen = set()
    for x in exceptions:
        if not isinstance(x, dict) or not all(isinstance(x.get(k), str) and x[k] for k in (
            "name", "model_id", "effort", "size", "role", "score_evidence", "authorization"
        )) or x["size"] not in SIZES or x["effort"] not in EFFORTS or x["role"] != "executor" or (
            x["score_evidence"] not in ("missing", "numeric-below-floor")
        ) or not isinstance(x.get("not_waived"), list) or not x["not_waived"]:
            raise ExecutorPolicyError(f"{source}: invalid floor exception")
        if x.get("harness") is not None and not isinstance(x["harness"], str):
            raise ExecutorPolicyError(f"{source}: invalid exception harness")
        if x["score_evidence"] == "numeric-below-floor" and (
            type(x.get("minimum_score")) not in (int, float) or
            not math.isfinite(x["minimum_score"]) or x["minimum_score"] >= floors[x["size"]]
        ):
            raise ExecutorPolicyError(f"{source}: numeric exception must have a finite below-floor minimum")
        key = (x["model_id"], x["effort"], x["size"], x["role"], x.get("harness"))
        if key in seen:
            raise ExecutorPolicyError(f"{source}: duplicate exception {key}")
        seen.add(key)
    # This digest identifies the deployed policy bytes; it is not a dispatch receipt.
    policy["_provenance"] = {"source": str(source), "sha256": hashlib.sha256(raw).hexdigest(),
                              "precedence": "shipped default; authenticated user policy is resolved later"}
    return policy


POLICY = load_policy()
INDEX = POLICY["benchmark_index"]
FLOORS = POLICY["floors"]
VERSION = POLICY["policy_version"]
IDEAL = {size: tuple((r["model_id"], r["effort"], r.get("harness")) for r in entries)
         for size, entries in POLICY["executor_preferred"].items()}
PLANNER_BY_GEAR = {size: tuple((r["model_id"], r["effort"], r.get("harness")) for r in entries)
                   for size, entries in POLICY["planner_preferred"].items()}
FLOOR_EXCEPTION = next((dict(x) for x in POLICY["floor_exceptions"]
                        if x["score_evidence"] == "missing"), {})


def policy_provenance() -> dict:
    """Expose the canonical shipped policy fingerprint for audit and future snapshots."""
    return {**POLICY["_provenance"], "policy_version": VERSION, "benchmark_index": INDEX}



def planner_seed(gear: str | None) -> list[dict]:
    return [{"model_id": model, "effort": effort, **({"harness": harness} if harness else {})}
            for model, effort, harness in PLANNER_BY_GEAR.get(gear, ())]


def new_gear_size(request: dict) -> str | None:
    """New XS-XL funding is not retroactively imposed on legacy pinned runs."""
    context = request.get("context") or {}
    gear = context.get("gear") or request.get("run_gear")
    # Raw routing callers, replay fixtures, and historical requests without a
    # declared run gear are NOT implicitly upgraded to #809 policy.
    if gear not in FLOORS:
        return None
    size = request.get("size_class") or context.get("size_class")
    return size if size in FLOORS else None


def ideal_seed(task_size: str | None) -> list[dict]:
    return [{"model_id": model, "effort": effort,
             **({"harness": harness} if harness else {})}
            for model, effort, harness in IDEAL.get(task_size, ())]


def model_restriction(candidate: dict, size: str | None) -> str | None:
    """Automatic-only model restrictions; explicit user routes have own gates."""
    if size not in FLOORS:
        return None
    model = candidate.get("alias_resolved_to") or candidate.get("model_id") or ""
    for restriction in POLICY["model_restrictions"]:
        if restriction["model_id"] != model:
            continue
        if "allowed_sizes" in restriction and size not in restriction["allowed_sizes"]:
            return restriction["reason"].format(size=size)
        if "allowed_harnesses" in restriction and candidate.get("harness") not in restriction["allowed_harnesses"]:
            return restriction["reason"].format(size=size)

    return None


def benchmark_gate(candidate: dict, size: str | None) -> str | None:
    """Fail closed on missing, incompatible, malformed or below-floor scores."""
    if size not in FLOORS:
        return None  # historical runs lacking task size retain their old role floor
    scores = candidate.get("benchmark_indexes") or {}
    raw = scores.get(INDEX) if isinstance(scores, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return f"{size} subtask requires numeric {INDEX} >= {FLOORS[size]}; exact-effort benchmark missing"
    if not math.isfinite(raw) or raw < FLOORS[size]:
        return f"{size} subtask requires {INDEX} >= {FLOORS[size]} (got {raw})"
    return None


def floor_exception(candidate: dict, size: str | None, role: str | None = None) -> dict | None:
    """Exact audited exceptions are data-driven; no unknown score or scope is invented."""
    model = candidate.get("alias_resolved_to") or candidate.get("model_id")
    scores = candidate.get("benchmark_indexes") or {}
    raw = scores.get(INDEX) if isinstance(scores, dict) else None
    for exception in POLICY["floor_exceptions"]:
        if (size != exception["size"] or role not in (exception["role"], "worker") or
                model != exception["model_id"] or candidate.get("effort") != exception["effort"] or
                ("harness" in exception and candidate.get("harness") != exception["harness"])):
            continue
        evidence = exception["score_evidence"]
        if evidence == "numeric-below-floor":
            if type(raw) not in (int, float) or not math.isfinite(raw) or (
                    raw < exception["minimum_score"] or raw >= FLOORS[size]):
                continue
        elif isinstance(scores, dict) and scores.get(INDEX) is not None:
            continue
        return {"exception": exception["name"], "model_id": model,
                "effort": exception["effort"], "size": size, "index": INDEX,
                **({"harness": exception["harness"]} if "harness" in exception else {}),
                **({"observed_score": raw} if evidence == "numeric-below-floor" else {}),
                "waived": f"{INDEX} >= {FLOORS[size]} ({exception['authorization']})",
                "not_waived": list(exception["not_waived"])}
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
