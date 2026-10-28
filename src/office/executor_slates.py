"""Issue #809: version-pinned size floors and advisory ideal executor routes.

This is not a registry. Preferred routes remain subject to catalog, trust,
capability, quota, discovery, and exact invocation qualification gates.
"""
from __future__ import annotations

import math

INDEX = "Artificial Analysis Intelligence Index v4.3.2"
FLOORS = {"XS": 31, "S": 38, "M": 40, "L": 44, "XL": 47}
VERSION = "xs-xl-executor-slates-2026-10-28-861"
# Each tuple: concrete model ID, exact effort, harness (None = any qualified).
IDEAL = {
    "XS": (("claude-haiku-5-5", "medium", "claude"),
           ("gemini-3.8-flash", "low", None),
           ("gpt-6.1-terra", "low", "codex")),
    "S": (("gemini-3.8-flash", "medium", None),
          ("claude-haiku-5-5", "high", "claude"),
          ("gpt-6.1-terra", "medium", "codex")),
    "M": (("claude-haiku-5-5", "high", "claude"),
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

# Planner routes, by RUN gear (#850 §4), used only when the user asks for a standalone planner: the root
# orchestrator plans every run by default. XS and S have no automatic planner and no invented slate.
PLANNER_BY_GEAR = {
    "M": (("claude-opus-5-5", "low", "claude"), ("gpt-6-astra", "low", "codex"), ("gemini-4-argon", "low", None)),
    "L": (("claude-opus-5-5", "medium", "claude"), ("gpt-6-astra", "medium", "codex"),
          ("gemini-4-argon", "medium", None)),
    "XL": (("claude-opus-5-5", "high", "claude"), ("gpt-6-astra", "high", "codex"),
           ("gemini-4-argon", "high", None)),
}


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


# User-approved (#809, 2026-10-27) and deliberately narrow: only this exact model,
# effort, role and subtask size may pass the AA floor while its score is unpublished.
FLOOR_EXCEPTION = {"model_id": "gemini-4-argon", "effort": "medium", "role": "executor", "size": "L"}


def floor_exception(candidate: dict, size: str | None, role: str | None = None) -> dict | None:
    """The audit record when the Argon Medium L exception waives the missing-score floor.

    Never fabricates a score, lowers the L floor for another model, effort or size,
    or waives registration, conformance, trust, quota or risk gates; a published
    score is judged on merit like any other candidate.
    """
    # #861: exact owner-authorized M floor exception; never amend the pinned score.
    model = candidate.get("alias_resolved_to") or candidate.get("model_id")
    if (size == "M" and role in ("executor", "worker") and model == "claude-haiku-5-5"
            and candidate.get("effort") == "high" and candidate.get("harness") == "claude"):
        scores = candidate.get("benchmark_indexes") or {}
        raw = scores.get(INDEX) if isinstance(scores, dict) else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and raw >= FLOORS[size]:
            return None
        return {"exception": "haiku-high-m-owner-authorized-floor", "model_id": model,
                "effort": "high", "harness": "claude", "size": size,
                "index": INDEX, "observed_score": raw,
                "waived": f"{INDEX} >= {FLOORS[size]} (owner authorization #861)",
                "not_waived": ["registration", "conformance", "trust", "quota", "risk"]}
    if size != FLOOR_EXCEPTION["size"] or role not in (FLOOR_EXCEPTION["role"], "worker"):
        return None
    model = candidate.get("alias_resolved_to") or candidate.get("model_id")
    if model != FLOOR_EXCEPTION["model_id"] or candidate.get("effort") != FLOOR_EXCEPTION["effort"]:
        return None
    scores = candidate.get("benchmark_indexes")
    if isinstance(scores, dict) and scores.get(INDEX) is not None:
        return None
    return {"exception": "argon-medium-l-intelligence-floor", "model_id": model,
            "effort": candidate["effort"], "size": size, "index": INDEX,
            "waived": f"{INDEX} >= {FLOORS[size]} (score unpublished; no score fabricated)",
            "not_waived": ["registration", "conformance", "trust", "quota", "risk"]}


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
