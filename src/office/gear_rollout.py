"""#809 read-only rollout metrics from settled executor outcomes and recorded run evidence.

Everything here is observational and comes from rows already in a runs.db COPY.
Counterfactual routes (the model a task did not run on) are never executed or
inferred, so no claim about them is measured.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict

from office.util import parse_iso

VALID_SIZES = ("XS", "S", "M", "L", "XL")
COUNTERFACTUAL_NOTE = ("unexecuted counterfactual routes are not measured: this report observes only the routes "
                       "that actually ran and cannot say how another model would have performed")
GATE_SIGNALS = (
    ("review", "events review.degraded_fallback: a non-independent reviewer stood in for the funded independent review"),
    ("permission", "events submit.refused with code outside-scope: a worker changed files outside its leased scope"),
    ("landing", "events pr.merged with no merge authorization recorded at or before the merge"),
)


def _seconds(start: str | None, end: str | None) -> float | None:
    try:
        delta = (parse_iso(end) - parse_iso(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return delta if math.isfinite(delta) and delta >= 0 else None


def collect_run_evidence(con: sqlite3.Connection, run_ids: set[str]) -> dict:
    """End-to-end task latency and gate-violation counts recorded for `run_ids`.

    Latency runs from the task row's creation (plan acceptance, so queue and wait
    time count) to its last `task.accepted` event. A task with no usable pair of
    timestamps is counted as unmeasured, never as zero.
    """
    latency: dict[tuple[str, str], float] = {}
    unmeasured = 0
    violations = {name: 0 for name, _ in GATE_SIGNALS}
    for run_id in sorted(run_ids):
        accepted_at = {r[0]: r[1] for r in con.execute(
            "SELECT task_id, MAX(created_at) FROM events WHERE run_id=? AND kind='task.accepted' "
            "AND task_id IS NOT NULL GROUP BY task_id", (run_id,))}
        for task_id, created in con.execute(
                "SELECT id, created_at FROM tasks WHERE run_id=? AND accepted_revision_id IS NOT NULL", (run_id,)):
            seconds = _seconds(created, accepted_at.get(task_id))
            if seconds is None:
                unmeasured += 1
            else:
                latency[(run_id, task_id)] = seconds
        violations["review"] += con.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND kind='review.degraded_fallback'", (run_id,)).fetchone()[0]
        violations["permission"] += con.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=? AND kind='submit.refused' "
            "AND json_extract(payload_json, '$.code')='outside-scope'", (run_id,)).fetchone()[0]
        violations["landing"] += con.execute(
            "SELECT COUNT(*) FROM events e WHERE e.run_id=? AND e.kind='pr.merged' AND NOT EXISTS("
            "SELECT 1 FROM authorizations a WHERE a.run_id=e.run_id AND a.kind='merge' "
            "AND a.revoked_at IS NULL AND a.created_at<=e.created_at)", (run_id,)).fetchone()[0]
    return {"latency": latency, "latency_unmeasured_tasks": unmeasured, "gate_violations": violations}


def _latency_summary(latency: dict[tuple[str, str], float], unmeasured: int) -> dict:
    values = sorted(latency.values())
    return {
        "samples": len(values),
        "unmeasured_accepted_tasks": unmeasured,
        "mean": round(statistics.fmean(values), 2) if values else None,
        "median": round(statistics.median(values), 2) if values else None,
        "max": round(values[-1], 2) if values else None,
        "from": "task row creation (plan acceptance) to the last task.accepted event, queue and wait included",
    }


def summarize(outcomes: list[dict], evidence: dict | None = None) -> dict:
    """Summarize settled route_learning.derive_outcomes rows plus `collect_run_evidence`.

    A missing money value leaves cost/accepted unknown; it is never treated as zero.
    Without `evidence` the latency and gate-violation sections report zero samples
    and zero counts and `evidence_collected` is False.
    """
    collected = evidence is not None
    evidence = evidence or {"latency": {}, "latency_unmeasured_tasks": 0,
                            "gate_violations": {name: 0 for name, _ in GATE_SIGNALS}}
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
    undersized = 0
    cost_total = 0.0
    known_cost_attempts = 0
    all_attempts = 0
    active_seconds = 0.0
    known_wall_attempts = 0
    by_size: dict[str, int] = defaultdict(int)
    rework_by_size: dict[str, int] = defaultdict(int)
    for batch in tasks.values():
        batch.sort(key=lambda r: (str(r.get("ended_at") or ""), str(r.get("dispatch_id") or "")))
        first = batch[0]
        any_accepted = any(r.get("success") is True for r in batch)
        accepted += int(any_accepted)
        first_pass += int(first.get("success") is True)
        extra = max(len(batch) - 1, 0)
        rework += extra
        sized = [r.get("size_class") or (r.get("descriptor") or {}).get("task_size") for r in batch]
        undersized += int(any(
            prev in VALID_SIZES and later in VALID_SIZES
            and VALID_SIZES.index(later) > VALID_SIZES.index(prev)
            for prev, later in zip(sized, sized[1:])))
        size = sized[0]
        if size in VALID_SIZES:
            by_size[size] += 1
            rework_by_size[size] += extra
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
    violations = dict(evidence["gate_violations"])
    return {
        "schema": "xs-xl-rollout-observation-v2",
        "evidence_collected": collected,
        "settled_tasks_observed": task_count,
        "accepted_tasks_observed": accepted,
        "settled_dispatches_observed": all_attempts,
        "task_sizes_first_observed": dict(sorted(by_size.items())),
        "first_pass_accepted_tasks": first_pass,
        "first_pass_acceptance_rate": (round(first_pass / task_count, 4) if task_count else None),
        "rework_dispatches_after_first": rework,
        "rework_dispatches_by_first_size": dict(sorted(rework_by_size.items())),
        "undersized_tasks": undersized,
        "observed_upward_size_reclassifications": undersized,
        "money_actual_coverage": {"known": known_cost_attempts, "total": all_attempts},
        "cost_per_accepted_task_usd": (round(cost_total / accepted, 5) if accepted and full_cost_coverage else None),
        "active_dispatch_seconds": (round(active_seconds, 2) if full_wall_coverage else None),
        "active_dispatch_wall_coverage": {"known": known_wall_attempts, "total": all_attempts},
        "end_to_end_latency_seconds": _latency_summary(evidence["latency"], evidence["latency_unmeasured_tasks"]),
        "gate_violations": {**violations, "total": sum(violations.values())},
        "gate_violation_count": sum(violations.values()),
        "gate_violation_signals": {name: text for name, text in GATE_SIGNALS},
        "gate_violations_certified_zero": False,
        "gate_verification_limitation": ("counts are the violation signals present in runs.db; a zero count is the "
                                         "absence of a recorded signal, not proof that review, permission and landing "
                                         "receipts were checked"),
        "counterfactual_routes_measured": False,
        "counterfactual_route_success_proven": False,
        "counterfactual_note": COUNTERFACTUAL_NOTE,
    }
