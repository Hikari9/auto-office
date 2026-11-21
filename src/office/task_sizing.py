"""#809: independent, evidence-backed implementation size and dispatch profile.

This module classifies work, not risk. The run's pinned review/authorization
contract is never an input to task size, and no model is hard-wired to a size.
Versioned records are deterministic so plan revisions and dispatches can diff.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal

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
    if run_gear == "greenfield":
        # One cohesive build must qualify for the strong (XL) executor slate;
        # a short file list or a deceptively easy first phase must not downsize it.
        # This is a routing floor, not an elevated safety-risk classification.
        return {"task_size": "XL", "task_size_rationale": "greenfield: one executor builds all 2-3 phases as a single cohesive XL task",
                "task_size_source": "greenfield-gear", "profile": "deep", "profile_version": PROFILE_VERSION,
                "task_size_version": VERSION, "decomposition_recommended": False}
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
    or None when absent, malformed, not strictly positive or backed by no real evidence (under 12 characters or
    3 words). The cost is the whole path to an accepted result
    (executor time, retries and review rounds), so a cheap first run that needs more repair does not beat
    dedicated dispatch, and a zero cost is a missing measurement, not a free path."""
    m = _ESTIMATE.fullmatch((text or "").strip())
    if not m:
        return None
    inline, dedicated = float(m[1]), float(m[2])
    if not (math.isfinite(inline) and math.isfinite(dedicated)) or inline <= 0 or dedicated <= 0:
        return None
    # The saving is taken from the decimal text, so it neither gains float noise (22 - 14.1) nor rounds a real
    # advantage to zero.
    # Ordering is decided on the decimals: two distinct texts can collapse to one float. The original texts are
    # kept for the receipt.
    evidence = m[4].strip()
    if len(evidence) < 12 or len(evidence.split()) < 3:  # a placeholder is not a measurement (as task_size_rationale)
        return None
    saving = Decimal(m[2]) - Decimal(m[1])
    return {"inline": inline, "dedicated": dedicated, "inline_text": m[1], "dedicated_text": m[2],
            "beneficial": saving > 0, "unit": m[3], "evidence": evidence, "savings": float(saving)}


def number(x: float) -> str:
    """A cost as the shortest text that round-trips (`22`, `14.1`, `1.0000000000001`), never a rounded one."""
    text = repr(float(x))
    return text[:-2] if text.endswith(".0") else text


def execution_gear(run: dict) -> str | None:
    """The gear execution_mode may act on: only a run pinned to the XS-XL gear model. A run pinned before it
    (a legacy name, or a gear with no `gear_model_version`) keeps its original semantics and never goes inline."""
    gates = run.get("gates") or {}
    return run.get("gear") if gates.get("gear_model_version") == "run-gear-1" else None


def execution_mode(gear: str | None, descriptor: dict | None, risk: dict | None, *, task_count: int | None = None,
                   fastlane: dict | None = None, parallel: bool = False) -> dict:
    """Who executes a task: the orchestrator inline, or a dedicated executor (the default).

    XS runs go inline by default; an S run only on a measured estimate that inline beats dedicated dispatch;
    an M run only when a single task has measured cost advantage; L and XL always delegate; elevated risk always delegates. The
    record carries the owner, the reason and the estimated savings. An inline task still gets the worktree,
    the fenced lease, scope, self-review subagents, convergence review and landing of a delegated one.
    """
    d, risk = descriptor or {}, risk or {}
    size = d.get("task_size")
    estimate = inline_estimate(d.get("inline_advantage"))

    def record(mode: str, why: str, savings: dict | None = None) -> dict:
        return {"mode": mode, "owner": "orchestrator" if mode == "inline" else "executor", "why": why,
                "estimated_savings": savings, "decision_version": EXECUTION_VERSION}

    if fastlane is not None:  # #1297: a run that pinned a fastlane policy at start
        return _fastlane_mode(gear, d, risk, size, estimate, fastlane, parallel)
    if gear not in ("XS", "S", "M"):
        return record("delegated", f"dedicated executor: a {gear or 'legacy'} run never executes inline")
    if risk.get("classification") != "low":
        return record("delegated", "dedicated executor: inline needs low risk")
    if size not in INLINE_SIZES or (gear == "XS" and size == "M"):
        return record("delegated", f"dedicated executor: a {size or 'unsized'} task is too large for inline in a {gear} run")
    if gear == "XS":
        return record("inline", "XS run, low risk, bounded task: inline by default to keep orchestrator context")
    if gear == "M" and task_count != 1:
        return record("delegated", "dedicated executor: an M run goes inline only for a single-task run")
    if estimate is None:
        return record("delegated", f"dedicated executor: an {gear} run goes inline only on a measured "
                                   "inline=<n> dedicated=<n> <unit> cost-to-success estimate")
    if not estimate["beneficial"]:  # the exact decimal costs decide
        return record("delegated", f"dedicated executor: estimated inline cost {estimate['inline_text']} does not beat "
                                   f"dedicated {estimate['dedicated_text']} {estimate['unit']}")
    return record("inline", f"measured cost-to-success ({gear} run): inline {estimate['inline_text']} vs dedicated "
                            f"{estimate['dedicated_text']} {estimate['unit']} ({estimate['evidence']})", estimate)


FASTLANE_EXECUTION_VERSION = "execution-mode-2"
_LED = ("orchestrator-led with its own strategy (inline or its own subagents, no Office executor routing); "
        "independent review still required")


def evidence_text(text: str | None) -> str | None:
    """A declared statement with real content (at least 12 characters and 3 words), or None."""
    value = (text or "").strip()
    return value if len(value) >= 12 and len(value.split()) >= 3 else None


def orchestrator_floor(text: str | None, size: str | None) -> str | None:
    """Why an inline_quality statement fails the executor floor for `size`, or None when it passes.

    The statement starts with the orchestrator's own route, `<harness>/<model>@<effort>: <why>`. Office looks the
    route up in the catalog and applies the same size floor an executor of that size must meet (#809)."""
    from office import candidates, executor_slates
    route, sep, _ = (text or "").partition(":")
    route = route.strip()
    if not sep or "/" not in route or "@" not in route or " " in route:
        return "inline_quality must start with the orchestrator's route, <harness>/<model>@<effort>: <why>"
    parsed = candidates.parse_route_override(route)
    if not (parsed["harness"] and parsed["model_id"] and parsed["effort"]):
        return "inline_quality must start with the orchestrator's route, <harness>/<model>@<effort>: <why>"
    rows = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == parsed["harness"]
            and parsed["model_id"] in (r.get("model_id"), r.get("invocation_model_id")) and r.get("effort") == parsed["effort"]]
    if not rows:
        return f"the orchestrator route {route} is not in the catalog, so its {size} floor cannot be checked"
    failed = [executor_slates.benchmark_gate(r, size) for r in rows]
    return None if any(f is None for f in failed) else f"the orchestrator route {route}: {failed[0]}"


def _fastlane_mode(gear, d: dict, risk: dict, size, estimate, fastlane: dict, parallel: bool) -> dict:
    """#1297 execution mode. `inline` keeps its recorded name (owner orchestrator) and now means orchestrator-led:
    the orchestrator may implement inline or use its own subagents. Overflow is checked first and latches
    delegation for every later decision; then the invoker's flag wins; otherwise Office applies fastlane only when
    it is sure (low risk, and the XS default or all of quality, time and cost)."""

    def record(mode: str, why: str, savings: dict | None = None, checks: dict | None = None) -> dict:
        out = {"mode": mode, "owner": "orchestrator" if mode == "inline" else "executor", "why": why,
               "estimated_savings": savings, "decision_version": FASTLANE_EXECUTION_VERSION,
               "fastlane": fastlane.get("policy")}
        if checks is not None:
            out["fastlane_checks"] = checks
        return out

    policy = fastlane.get("policy")
    if gear == "greenfield":
        gear = "XL"  # #1300 G3: the one cohesive greenfield build is an XL task (its route and overflow target)
    if gear not in SIZES:
        return record("delegated", f"dedicated executor: a {gear or 'legacy'} run has no fastlane")
    if fastlane.get("overflow"):
        return record("delegated", "dedicated executor: fastlane overflowed, so the remaining work goes to Office executors")
    if policy == "off":
        return record("delegated", "dedicated executor: the invoker turned fastlane off (--no-fastlane)")
    if policy == "on":
        return record("inline", f"fastlane forced by the invoker's --fastlane flag: {_LED}")
    cls = risk.get("classification")
    if cls != "low":
        return record("delegated", f"dedicated executor: not sure, fastlane is only suggested on low risk (risk is {cls or 'unknown'})")
    if gear == "XS" and size in ("XS", "S"):
        return record("inline", f"fastlane: XS default for a bounded low-risk task, {_LED}")
    if size not in SIZES or str(d.get("task_size_rationale") or "").startswith("insufficient"):
        return record("delegated", "dedicated executor: not sure, the task size is unknown")
    quality = evidence_text(d.get("inline_quality"))
    below = orchestrator_floor(quality, size) if quality else None
    checks = {"quality": bool(quality) and below is None, "time": not parallel,
              "cost": bool(estimate and estimate["beneficial"])}
    failed = []
    if not quality:
        failed.append(f"quality (no inline_quality: <harness>/<model>@<effort>: <why> naming the orchestrator's route "
                      f"for the {size} executor floor)")
    elif below:
        failed.append(f"quality ({below})")
    if parallel:
        failed.append("time (the plan has tasks Office executors could run in parallel)")
    if estimate is None:
        failed.append("cost (no measured inline=<n> dedicated=<n> <unit>: <evidence> estimate)")
    elif not estimate["beneficial"]:
        failed.append(f"cost (orchestrator-led {estimate['inline_text']} does not beat executors "
                      f"{estimate['dedicated_text']} {estimate['unit']})")
    if failed:
        return record("delegated", "dedicated executor: not sure, fastlane needs quality, time and cost all in favour; "
                                   "failed " + "; ".join(failed), checks=checks)
    return record("inline", f"fastlane ({gear} run, {size} task): quality ({quality}), time (no parallel tasks) and cost "
                            f"(orchestrator-led {estimate['inline_text']} vs executors {estimate['dedicated_text']} "
                            f"{estimate['unit']}: {estimate['evidence']}) all favour it; {_LED}", estimate, checks)


# ------------------------------------------------------------------ expected budget per size class (#917)

# Expected (context tokens in one turn, wall seconds, cost USD) of one dispatch per size class. A dispatch
# observed past OVERSIZE_FACTOR x any of them is oversized for its class: a signal, never an action.
BUDGETS = {
    "XS": {"context": 60_000, "wall": 600, "cost": 0.5},
    "S": {"context": 100_000, "wall": 1_200, "cost": 1.5},
    "M": {"context": 150_000, "wall": 2_400, "cost": 4.0},
    "L": {"context": 200_000, "wall": 4_500, "cost": 8.0},
    "XL": {"context": 272_000, "wall": 9_000, "cost": 15.0},
}
OVERSIZE_FACTOR = 2.5
