"""#809 must not turn incomplete rollout observations into fake successes."""
from office import gear_rollout


def row(task, *, success, size, ended, money=0.5, wall=10.0):
    return {"run_id": "run", "task_id": task, "dispatch_id": f"{task}-{ended}",
            "role": "executor", "success": success, "size_class": size,
            "ended_at": str(ended), "money_actual": money, "wall_seconds": wall}


def test_rollout_cost_and_rework_from_settled_attempts():
    report = gear_rollout.summarize([
        row("T1", success=True, size="S", ended=1, money=1.0),
        row("T2", success=False, size="XS", ended=1, money=0.5),
        row("T2", success=True, size="M", ended=2, money=0.5),
    ])
    assert report["settled_tasks_observed"] == 2
    assert report["accepted_tasks_observed"] == 2
    assert report["rework_dispatches_after_first"] == 1
    assert report["first_pass_acceptance_rate"] == 0.5
    assert report["observed_upward_size_reclassifications"] == 1
    assert report["cost_per_accepted_task_usd"] == 1.0
    assert report["active_dispatch_seconds"] == 30.0
    assert report["end_to_end_latency_seconds"] is None
    assert report["gate_violation_count"] is None
    assert report["counterfactual_route_success_proven"] is False


def test_missing_money_cannot_be_reported_as_zero_or_complete():
    rows = [row("T1", success=False, size="S", ended=1, money=None),
            row("T1", success=True, size="S", ended=2, money=1.0)]
    report = gear_rollout.summarize(rows)
    assert report["money_actual_coverage"] == {"known": 1, "total": 2}
    assert report["cost_per_accepted_task_usd"] is None
    assert report["gate_violations_certified_zero"] is False


def test_empty_and_unsettled_outside_gear_are_not_claimed_successful():
    assert gear_rollout.summarize([])["first_pass_acceptance_rate"] is None
    assert gear_rollout.summarize([])["cost_per_accepted_task_usd"] is None
    assert gear_rollout.summarize([{"role": "planner", "run_id": "r", "task_id": "t"}])["settled_tasks_observed"] == 0
