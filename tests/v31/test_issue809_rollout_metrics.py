"""#809 must not turn incomplete rollout observations into fake successes."""
import json
import subprocess
import sys
from pathlib import Path

from office import db, gear_rollout

ROOT = Path(__file__).resolve().parents[2]


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
    assert report["rework_dispatches_by_first_size"] == {"S": 0, "XS": 1}
    assert report["first_pass_acceptance_rate"] == 0.5
    assert report["undersized_tasks"] == 1
    assert report["cost_per_accepted_task_usd"] == 1.0
    assert report["active_dispatch_seconds"] == 30.0
    assert report["counterfactual_route_success_proven"] is False
    assert report["counterfactual_routes_measured"] is False
    assert "not measured" in report["counterfactual_note"]


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


def _seed(con):
    """One XL run: T1 accepted in 120s, T2 accepted without an event, plus one of each violation signal."""
    con.execute("INSERT INTO runs(id, gear, phase, created_at) VALUES('r1','XL','closed','2026-10-27T00:00:00+00:00')")
    con.execute("INSERT INTO runs(id, gear, phase, created_at) VALUES('legacy','quick','closed','2026-10-27T00:00:00+00:00')")
    for tid, accepted in (("T1", "rev1"), ("T2", "rev2"), ("T3", None)):
        con.execute("INSERT INTO tasks(run_id,id,title,role,scope_json,depends_json,accept_json,checks_json,status,"
                    "introduced_plan_version,contract_version,acceptance_version,accepted_revision_id,created_at,updated_at) "
                    "VALUES('r1',?,'t','executor','[]','[]','[]','[]','accepted',1,1,1,?,'2026-10-27T01:00:00+00:00',"
                    "'2026-10-27T01:00:00+00:00')", (tid, accepted))

    def event(kind, created, task=None, payload=None, run="r1", summary=None):
        con.execute("INSERT INTO events(run_id,kind,audience,task_id,summary,payload_json,office_version,created_at) "
                    "VALUES(?,?,'runtime',?,?,?,'t',?)",
                    (run, kind, task, summary or kind, payload if isinstance(payload, str) else json.dumps(payload or {}),
                     created))

    event("task.accepted", "2026-10-27T01:01:00+00:00", "T1")
    event("task.accepted", "2026-10-27T01:02:00+00:00", "T1")
    event("review.degraded_fallback", "2026-10-27T01:02:00+00:00", "T1")
    event("submit.refused", "2026-10-27T01:02:00+00:00", "T1", {"code": "outside-scope"})
    event("submit.refused", "2026-10-27T01:02:00+00:00", "T1", {"code": "other"})
    event("pr.merged", "2026-10-27T02:00:00+00:00", summary="T1 #7 merged into main (squash)")
    # a PR a human merged on GitHub and Office adopted is not an Office landing violation
    event("pr.merged", "2026-10-27T02:10:00+00:00", summary="T2 #8 already merged on GitHub; adopted by Office")
    # a historical row whose payload is not JSON must not abort the whole report
    event("submit.refused", "2026-10-27T02:20:00+00:00", "T1", "not json{")
    event("pr.merged", "2026-10-27T03:00:00+00:00", run="legacy", summary="T1 #9 merged into main (squash)")
    con.execute("INSERT INTO authorizations(id,run_id,kind,authorized_by,quote,created_at) "
                "VALUES('A1','r1','merge','user','q','2026-10-27T02:30:00+00:00')")


def test_evidence_collects_latency_and_gate_violation_signals_from_runs_db(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    _seed(con)
    evidence = gear_rollout.collect_run_evidence(con, {"r1"})
    assert evidence["latency"] == {("r1", "T1"): 120.0}
    assert evidence["latency_unmeasured_tasks"] == 1
    # the merge at 02:00 predates the 02:30 authorization; the legacy run is outside the cohort
    assert evidence["gate_violations"] == {"review": 1, "permission": 1, "landing": 1}
    report = gear_rollout.summarize([], evidence)
    assert report["evidence_collected"] is True
    assert report["end_to_end_latency_seconds"]["mean"] == 120.0
    assert report["end_to_end_latency_seconds"]["unmeasured_accepted_tasks"] == 1
    assert report["gate_violation_count"] == 3
    assert report["gate_violations"]["total"] == 3
    assert report["gate_violations_certified_zero"] is False


def test_rollout_script_reports_recorded_latency_and_violations(tmp_path):
    path = tmp_path / "runs.db"
    con = db.connect(path)
    _seed(con)
    con.close()
    out = subprocess.run([sys.executable, str(ROOT / "scripts/issue809_rollout_report.py"), "--runs-db", str(path)],
                         capture_output=True, text=True, check=True)
    report = json.loads(out.stdout)
    assert report["new_gear_runs_in_copy"] == 1
    assert report["end_to_end_latency_seconds"]["samples"] == 1
    assert report["gate_violation_count"] == 3
    assert report["counterfactual_routes_measured"] is False
