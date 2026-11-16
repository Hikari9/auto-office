"""scripts/review_no_verdict_rate.py: done review gates and the no-verdict share, split by reason, read-only."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import review_no_verdict_rate as rate
from office import db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "runs.db")
    yield c
    c.close()


def gate(con, gid, *, kind="code_review", status="done", verdict=None, review_status=None, evidence_status=None,
         finished="2026-11-10T12:00:00Z"):
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, verdict, review_status, evidence_status,"
                " created_at, finished_at) VALUES(?, 'R', 'T1', ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, kind, gid, status, verdict, review_status, evidence_status, finished, finished))


def attempt(con, did, gid, failure_class=None, kind="reviewer"):
    meta = {"review_attempt": {"epoch": 0, **({"failure": {"class": failure_class, "reason": "x"}} if failure_class else {})}}
    con.execute("INSERT INTO dispatches(id, run_id, gate_id, kind, route_json) VALUES(?, 'R', ?, ?, ?)",
                (did, gid, kind, json.dumps(meta)))


def seeded(con):
    gate(con, "ok1", verdict="PASS")
    gate(con, "ok2", kind="convergence_review", verdict="APPROVED", review_status="COMPLETED")
    gate(con, "rev1", verdict="REVOKED")
    gate(con, "rev2", kind="convergence_review", review_status="REVOKED")
    gate(con, "inv1", kind="convergence_review", review_status="INVALID_RESULT")
    gate(con, "inv2", verdict="ATTENTION")
    gate(con, "launch1", kind="convergence_review", review_status="UNAVAILABLE")
    attempt(con, "D1", "launch1", "wall")
    gate(con, "launch2", verdict="UNAVAILABLE")
    attempt(con, "D2", "launch2", "launch")
    gate(con, "other1", verdict="UNAVAILABLE")
    attempt(con, "D3", "other1", "invalid")
    gate(con, "blocked1", kind="visual", evidence_status="EVIDENCE_BLOCKED")
    gate(con, "plan1", kind="plan_review", verdict="REVOKED")
    gate(con, "plan2", kind="plan_review", verdict="APPROVED")
    gate(con, "int1", kind="integration_review", review_status="UNAVAILABLE")
    attempt(con, "D5", "int1", "launch")
    gate(con, "silent1", review_status="UNAVAILABLE")
    attempt(con, "D6", "silent1", "silent")
    # not counted: wrong kind, not done, a reviewer attempt of another gate
    gate(con, "chk", kind="checks", verdict="UNAVAILABLE")
    gate(con, "open", status="running", verdict="UNAVAILABLE")
    attempt(con, "D4", "ok1", "launch")


def test_counts_split_by_reason(con):
    seeded(con)
    m = rate.measure(con)
    assert m["done"] == 14
    assert m["by_reason"] == {"revoked": 3, "launch": 4, "invalid": 2, "evidence_blocked": 1, "other": 1}
    assert m["no_verdict"] == 11
    assert m["share"] == round(11 / 14, 4)


def test_plan_and_integration_reviews_are_in_the_population(con):
    gate(con, "p", kind="plan_review", verdict="REVOKED")
    gate(con, "i", kind="integration_review", verdict="APPROVED")
    m = rate.measure(con)
    assert (m["done"], m["no_verdict"]) == (2, 1)


def test_evidence_blocked_is_a_no_verdict_reason(con):
    gate(con, "b", evidence_status="EVIDENCE_BLOCKED")
    m = rate.measure(con)
    assert (m["no_verdict"], m["by_reason"]["evidence_blocked"]) == (1, 1)


def test_silent_failure_is_a_launch_failure(con):
    gate(con, "s", verdict="UNAVAILABLE")
    attempt(con, "D1", "s", "silent")
    assert rate.measure(con)["by_reason"]["launch"] == 1


def test_a_launch_failure_on_a_gate_that_has_a_verdict_is_not_counted(con):
    gate(con, "ok1", verdict="PASS")
    attempt(con, "D1", "ok1", "launch")
    m = rate.measure(con)
    assert (m["done"], m["no_verdict"]) == (1, 0)


def test_non_reviewer_dispatch_does_not_make_a_launch_failure(con):
    gate(con, "u1", verdict="UNAVAILABLE")
    attempt(con, "D1", "u1", "launch", kind="executor")
    assert rate.measure(con)["by_reason"] == {"revoked": 0, "launch": 0, "invalid": 0, "evidence_blocked": 0, "other": 1}


def test_window_is_inclusive_since_and_exclusive_until(con):
    gate(con, "a", verdict="REVOKED", finished="2026-11-01T00:00:00Z")
    gate(con, "b", verdict="PASS", finished="2026-11-05T00:00:00Z")
    gate(con, "c", verdict="REVOKED", finished="2026-11-09T00:00:00Z")
    m = rate.measure(con, "2026-11-01", "2026-11-09")
    assert (m["done"], m["no_verdict"]) == (2, 1)
    assert rate.measure(con, "2026-11-02")["done"] == 2


def test_empty_window_has_no_share(con):
    m = rate.measure(con)
    assert (m["done"], m["no_verdict"], m["share"]) == (0, 0, None)
    assert "n/a" in rate.render_text(m)


def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "review_no_verdict_rate.py"), *args],
                          capture_output=True, text=True)


def test_cli_json_and_read_only(con, tmp_path):
    seeded(con)
    con.close()
    path = tmp_path / "runs.db"
    before = path.read_bytes()
    out = run_cli("--db", str(path), "--format", "json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["no_verdict"] == 11
    assert path.read_bytes() == before
    text = run_cli("--db", str(path))
    assert "no verdict: 11 (78.6%)" in text.stdout and "launch: 4" in text.stdout


def test_cli_missing_db_is_not_created_and_exits_2(tmp_path):
    path = tmp_path / "absent.db"
    out = run_cli("--db", str(path))
    assert out.returncode == 2
    assert not path.exists()
