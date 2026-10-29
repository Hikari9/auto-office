"""Shadow-only delegation advice for #502.

This module never launches, vetoes, reroutes, or schedules a task. The existing
orchestrator and its authorization/review gates retain complete control.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from office import planfile

SHADOW_VERSION = "delegation-shadow-1"


def _paths(task: Mapping) -> list[str] | None:
    raw = task.get("scope")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    if any(not isinstance(p, str) or not p.strip() or p.strip().startswith("/") for p in raw):
        return None
    paths = [p.strip().strip("/") for p in raw if isinstance(p, str) and p.strip()]
    return paths if paths and all(paths) and len(paths) == len(raw) else None


def propose(tasks: Sequence[Mapping], *, parallel_requested: bool,
            cache_policy: Mapping | None = None) -> dict:
    """Evaluate *selected* tasks before fan-out; return advisory evidence only.

    Unknown scopes/cost/cache policy never authorize parallelism and never
    veto the operator's actual dispatch. Monetary comparison remains unknown
    without matched, complete evidence of whole-run alternatives.
    """
    ids = [str(t.get("id") or "") for t in tasks]
    scopes = [_paths(t) for t in tasks]
    edges = []
    missing = []
    for i, task in enumerate(tasks):
        if scopes[i] is None:
            missing.append(ids[i] or f"task-{i + 1}")
        depends = task.get("depends")
        if not isinstance(depends, (list, tuple)):
            missing.append(ids[i] or f"task-{i + 1}")
            continue
        for j in range(i):
            other = tasks[j]
            other_depends = other.get("depends")
            if not isinstance(other_depends, (list, tuple)):
                missing.append(ids[j] or f"task-{j + 1}")
                continue
            if ids[j] in depends or ids[i] in other_depends:
                edges.append({"tasks": [ids[j], ids[i]], "kind": "dependency"})
            if scopes[i] is not None and scopes[j] is not None and planfile.scopes_overlap(scopes[i], scopes[j]):
                edges.append({"tasks": [ids[j], ids[i]], "kind": "scope-overlap"})
    if len(tasks) <= 1:
        proposal, why = "single-producer", "one selected task; fan-out is not applicable"
    elif missing or any(not t for t in ids) or len(set(ids)) != len(ids):
        proposal, why = "unresolved", "missing/ambiguous task identity, scope or dependency evidence"
    elif edges:
        proposal, why = "cohesive-or-serial", "selected tasks share dependencies or ownership envelopes"
    else:
        proposal, why = "independent-lanes", "selected tasks have distinct scopes and no direct dependencies"
    cache = cache_policy or {}
    verified_cache = cache.get("verified") is True and cache.get("auth_mode") and cache.get("source")
    return {
        "version": SHADOW_VERSION, "mode": "shadow", "proposal": proposal,
        "actual": "parallel" if parallel_requested and len(tasks) > 1 else "serial",
        "task_ids": ids, "conflicts": edges, "missing_scope_or_dependencies": sorted(set(missing)),
        "reason": why,
        "whole_run_cost": "unknown: no matched whole-run strategy evidence supplied",
        "cache_policy": ("verified-soft-context-only" if verified_cache else "unknown-neutral"),
        "cache_source": cache.get("source") if verified_cache else None,
        "policy_effect": "none: actual dispatch, routing, authorization, quota and review unchanged",
    }
