"""Issue #809: version-pinned size floors and advisory ideal executor routes.

This is not a registry. Preferred routes remain subject to catalog, trust,
capability, quota, discovery, and exact invocation qualification gates.
"""
from __future__ import annotations

import math

INDEX = "Artificial Analysis Intelligence Index v4.3.2"
FLOORS = {"XS": 31, "S": 38, "M": 40, "L": 44, "XL": 47}
VERSION = "xs-xl-executor-slates-2026-10-27"
# Each tuple: concrete model ID, exact effort, harness (None = any qualified).
IDEAL = {
    "XS": (("claude-haiku-5-5", "medium", "claude"),
           ("gemini-3.8-flash", "low", None),
           ("gpt-6.1-terra", "low", "codex")),
    "S": (("gemini-3.8-flash", "medium", None),
          ("claude-haiku-5-5", "high", "claude"),
          ("gpt-6.1-terra", "medium", "codex")),
    "M": (("claude-sonnet-5-5", "medium", "claude"),
          ("claude-sonnet-5-5", "medium", "agy"),
          ("gpt-6.1-terra", "medium", "codex"),
          ("glm-5.3-flash", "max", "opencode")),
    "L": (("claude-sonnet-5-5", "high", "claude"),
          ("claude-sonnet-5-5", "high", "agy"),
          ("gpt-6.1-terra", "high", "codex"),
          ("gemini-4-argon", "medium", None)),
    "XL": (("claude-opus-5-5", "medium", "claude"),
           ("gpt-6.1-sol", "medium", "codex"),
           ("gemini-4-argon", "high", None)),
}


def new_gear_size(request: dict) -> str | None:
    """New XS-XL funding is not retroactively imposed on legacy pinned runs."""
    context = request.get("context") or {}
    gear = context.get("gear") or request.get("run_gear")
    if gear is not None and gear not in FLOORS:
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
    if model == "claude-opus-5-5" and size != "XL":
        return f"Opus 5.5 is reserved for XL subtasks (got {size})"
    if model == "claude-sonnet-5-5" and candidate.get("harness") not in ("claude", "agy"):
        return "Sonnet 5.5 harness must be Claude Code or agy"
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
