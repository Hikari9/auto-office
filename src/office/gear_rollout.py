"""#809 read-only, evidence-aware rollout metrics from settled executor outcomes.

These metrics are observational. They neither prove counterfactual routes nor
certify permission/review gates. Live run-level telemetry and gate acceptance
must be verified separately before the controlled rollout is approved.
"""
from __future__ import annotations

import math
from collections import defaultdict

VALID_SIZES = ("XS", "S", "M", "L", "XL")


def summarize(outcomes: list[dict]) -> dict:
    """Summarize settled route_learning.derive_outcomes rows.

    Only settled dispatches can enter this function. A missing money value
    leaves cost/accepted unknown; it is never silently treated as zero.
    """
    tasks: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for o in outcomes:
        if o.get("role") not in ("executor", "worker"):
            continue
        if not o.get("run_id") or not o.get("task_id"):
            continue
        tasks[(o["run_id"], o["task_id"])].append(o)
    task_count = len(tasks)
    accepted = 0
    first_pass = 0
    rework = 0
    upward_reclassification = 0
    cost_total = 0.0
    known_cost_attempts = 0
    all_attempts = 0
    active_seconds = 0.0
    known_wall_attempts = 0
    by_size: dict[str, int] = defaultdict(int)
    for batch in tasks.values():
        batch.sort(key=lambda r: (str(r.get("ended_at") or ""), str(r.get("dispatch_id") or "")))
        first = batch[0]
        any_accepted = any(r.get("success") is True for r in batch)
        accepted += int(any_accepted)
        first_pass += int(first.get("success") is True)
        rework += max(len(batch) - 1, 0)
        sized = [r.get("size_class") or (r.get("descriptor") or {}).get("task_size")
                 for r in batch]
        upward_reclassification += int(any(
            prev in VALID_SIZES and later in VALID_SIZES
            and VALID_SIZES.index(later) > VALID_SIZES.index(prev)
            for prev, later in zip(sized, sized[1:])))
        size = sized[0]
        if size in VALID_SIZES:
            by_size[size] += 1
        for r in batch:
            all_attempts += 1
            money = r.get("money_actual")
            if isinstance(money, (float, int)) and not isinstance(money, bool) and math.isfinite(money) and money >= 0:
                cost_total += money
                known_cost_attempts += 1
            wall = r.get("wall_seconds")
            if isinstance(wall, (float, int)) and not isinstance(wall, bool) and math.isfinite(wall) and wall >= 0:
                active_seconds += wall
                known_wall_attempts += 1
    full_cost_coverage = all_attempts > 0 and known_cost_attempts == all_attempts
    full_wall_coverage = all_attempts > 0 and known_wall_attempts == all_attempts
    return {
        "schema": "xs-xl-rollout-observation-v1",
        "settled_tasks_observed": task_count,
        "accepted_tasks_observed": accepted,
        "settled_dispatches_observed": all_attempts,
        "task_sizes_first_observed": dict(sorted(by_size.items())),
        "first_pass_accepted_tasks": first_pass,
        "first_pass_acceptance_rate": (round(first_pass / task_count, 4) if task_count else None),
        "rework_dispatches_after_first": rework,
        "observed_upward_size_reclassifications": upward_reclassification,
        "money_actual_coverage": {"known": known_cost_attempts, "total": all_attempts},
        "cost_per_accepted_task_usd": (round(cost_total / accepted, 5) if accepted and full_cost_coverage else None),
        "active_dispatch_seconds": (round(active_seconds, 2) if full_wall_coverage else None),
        "active_dispatch_wall_coverage": {"known": known_wall_attempts, "total": all_attempts},
        "end_to_end_latency_seconds": None,
        "end_to_end_latency_limitation": "requires authoritative acceptance timestamp and queue/wait intervals; summed worker time is not end-to-end latency",
        "gate_violation_count": None,
        "gate_violations_certified_zero": False,
        "gate_verification_limitation": "inspect full run-level review, permission and landing receipts separately; no acceptance is inferred from routing outcomes",
        "counterfactual_route_success_proven": False,
    }
