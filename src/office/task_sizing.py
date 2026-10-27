"""#809: independent, evidence-backed implementation size and dispatch profile.

This module classifies work, not risk. The run's pinned review/authorization
contract is never an input to task size, and no model is hard-wired to a size.
Versioned records are deterministic so plan revisions and dispatches can diff.
"""
from __future__ import annotations

import re

SIZES = ("XS", "S", "M", "L", "XL")
VERSION = "subtask-sizing-1"
PROFILE_VERSION = "task-dispatch-profile-1"
PROFILES = ("lean", "balanced", "deep", "discovery")


def classify(task: dict, *, run_gear: str | None = None) -> dict:
    """Derive one independent task size, rationale, and versioned route profile.

    Existing descriptor values remain authoritative recommendations, but they
    cannot change or weaken the run's pinned guarantees. `run_gear` is accepted
    so callers can pass it, but it never influences sizing.
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
            "task_size_source": "planner" if explicit in SIZES else "inferred",
            "profile": profile, "profile_version": PROFILE_VERSION,
            "task_size_version": VERSION,
            "decomposition_recommended": size == "XL" and (len(scopes) > 2 or len(deps) > 1 or
                                                         len(interfaces) > 2)}



def planner_declared(descriptor: dict | None) -> dict:
    """The descriptor with an Office-inferred size removed: a gate that needs a size the planner declared
    (a production-run discovery trial) never reads the conservative M that sizing fills in."""
    d = dict(descriptor or {})
    # `profile_version` is written only by classify, so a sized descriptor with no source is Office's, not the planner's.
    if d.get("task_size_source") == "inferred" or (not d.get("task_size_source") and d.get("profile_version")):
        d.pop("task_size", None)
    return d


# ------------------------------------------------------------------ execution mode (#809 R11)

EXECUTION_VERSION = "execution-mode-1"
INLINE_ROUTE = "orchestrator-inline (no model launched)"
INLINE_SIZES = ("XS", "S", "M")
_ESTIMATE = re.compile(r"inline=(\d+(?:\.\d+)?)\s+dedicated=(\d+(?:\.\d+)?)\s+([A-Za-z][\w./-]*):\s*(\S.*)", re.S)


def inline_estimate(text: str | None) -> dict | None:
    """The planner's measured total cost-to-success estimate, `inline=<n> dedicated=<n> <unit>: <evidence>`,
    or None when absent or malformed. The cost is the whole path to an accepted result (executor time,
    retries and review rounds), so a cheap first run that needs more repair does not beat dedicated dispatch."""
    m = _ESTIMATE.fullmatch((text or "").strip())
    if not m:
        return None
    inline, dedicated = float(m[1]), float(m[2])
    return {"inline": inline, "dedicated": dedicated, "unit": m[3], "evidence": m[4].strip(),
            "savings": round(dedicated - inline, 4)}


def execution_mode(gear: str | None, descriptor: dict | None, risk: dict | None, *, task_count: int | None = None) -> dict:
    """Who executes a task: the orchestrator inline, or a dedicated executor (the default).

    XS runs go inline by default; an S run only on a measured estimate that inline beats dedicated dispatch;
    an M run only when it is a single task; L and XL always delegate; elevated risk always delegates. The
    record carries the owner, the reason and the estimated savings. An inline task still gets the worktree,
    the fenced lease, scope, self-review subagents, convergence review and landing of a delegated one.
    """
    d, risk = descriptor or {}, risk or {}
    size = d.get("task_size")
    estimate = inline_estimate(d.get("inline_advantage"))

    def record(mode: str, why: str, savings: dict | None = None) -> dict:
        return {"mode": mode, "owner": "orchestrator" if mode == "inline" else "executor", "why": why,
                "estimated_savings": savings, "decision_version": EXECUTION_VERSION}

    if gear not in ("XS", "S", "M"):
        return record("delegated", f"dedicated executor: a {gear or 'legacy'} run never executes inline")
    if risk.get("classification") != "low":
        return record("delegated", "dedicated executor: inline needs low risk")
    if size not in INLINE_SIZES or (gear == "XS" and size == "M"):
        return record("delegated", f"dedicated executor: a {size or 'unsized'} task is too large for inline in a {gear} run")
    if gear == "XS":
        return record("inline", "XS run, low risk, bounded task: inline by default to keep orchestrator context")
    if gear == "M":
        if task_count == 1:
            return record("inline", "M run with a single task: nothing to parallelize, inline keeps context")
        return record("delegated", "dedicated executor: an M run goes inline only for a single-task run")
    if estimate is None:
        return record("delegated", "dedicated executor: an S run goes inline only on a measured "
                                   "inline=<n> dedicated=<n> <unit> cost-to-success estimate")
    if estimate["savings"] <= 0:
        return record("delegated", f"dedicated executor: estimated inline cost {estimate['inline']:g} does not beat "
                                   f"dedicated {estimate['dedicated']:g} {estimate['unit']}")
    return record("inline", f"measured cost-to-success: inline {estimate['inline']:g} vs dedicated "
                            f"{estimate['dedicated']:g} {estimate['unit']} ({estimate['evidence']})", estimate)
