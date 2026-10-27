"""#809: independent, evidence-backed implementation size and dispatch profile.

This module classifies work, not risk. The run's pinned review/authorization
contract is never an input to task size, and no model is hard-wired to a size.
Versioned records are deterministic so plan revisions and dispatches can diff.
"""
from __future__ import annotations

SIZES = ("XS", "S", "M", "L", "XL")
VERSION = "subtask-sizing-1"
PROFILE_VERSION = "task-dispatch-profile-1"
PROFILES = ("lean", "balanced", "deep", "discovery")


def classify(task: dict, *, run_gear: str | None = None) -> dict:
    """Derive one independent task size, rationale, and versioned route profile.

    Existing descriptor values remain authoritative recommendations, but they
    cannot change or weaken the run's pinned guarantees. The run gear is used
    only by the execution-mode decision, never for sizing.
    """
    d = task.get("descriptor") or {}
    scopes = task.get("scope") or []
    deps = task.get("depends") or []
    interfaces = task.get("interfaces") or []
    difficulty = d.get("difficulty_estimate")
    bounds = d.get("bounds")
    work = d.get("work")
    parts = []
    if scopes:
        parts.append(f"{len(scopes)} ownership scope(s)")
    if deps:
        parts.append(f"{len(deps)} dependency edge(s)")
    if interfaces:
        parts.append(f"{len(interfaces)} interface(s)")
    if work:
        parts.append(f"{work} work")
    if bounds:
        parts.append(f"{bounds} bounds")
    if difficulty:
        parts.append(f"{difficulty} difficulty")
    explicit = d.get("task_size")
    if explicit in SIZES:
        size = explicit
        rationale = d.get("task_size_rationale") or (
            "planner sizing evidence: " + ", ".join(parts) if parts else
            "planner-declared size; insufficient independent evidence (review at plan approval)")
    else:
        score = min(3, len(scopes) // 2) + min(3, len(deps)) + min(3, len(interfaces))
        score += 2 if work == "architecture" else 0
        score += 2 if bounds == "open" else 0
        score += {"high": 2, "very-high": 4}.get(difficulty, 0)
        if difficulty == "very-high" and score >= 10:
            size = "XL"
        elif score >= 7:
            size = "L"
        elif score >= 3:
            size = "M"
        elif (work == "mechanical" and len(scopes) <= 1 and not deps and not interfaces
              and bounds != "open" and difficulty not in ("high", "very-high")):
            size = "XS"
        elif parts:
            size = "S"
        else:
            size = "M"  # uncertainty must not masquerade as bounded quick work
        rationale = ("inferred from " + ", ".join(parts) if parts else
                     "insufficient scope, dependency and uncertainty evidence: conservative M")
    if bounds == "open" or (work == "architecture" and difficulty in ("unknown", "very-high")):
        profile = "discovery"
    elif difficulty in ("high", "very-high") or size in ("L", "XL"):
        profile = "deep"
    elif size == "XS" and bounds != "open":
        profile = "lean"
    else:
        profile = "balanced"
    # Record decomposition advice without silently changing size, acceptance,
    # ownership or scope. The orchestrator owns splitting substantial XL tasks.
    return {"task_size": size, "task_size_rationale": rationale,
            "profile": profile, "profile_version": PROFILE_VERSION,
            "task_size_version": VERSION,
            "decomposition_recommended": size == "XL" and (len(scopes) > 2 or len(deps) > 1 or
                                                         len(interfaces) > 2)}

