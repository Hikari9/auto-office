"""#211: a visual review run outside Office, recorded by the user, stands in
for an UNAVAILABLE visual gate. Independence is per agent session, so the
producer's model family is not a bar.

This suite covers the v3.1 review contract, which every run started before #337 (and any run
started with review.contract: v3.1) keeps for its whole life; it pins its runs to that contract.
The convergence contract is covered by test_convergence_contract.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE

pytestmark = pytest.mark.review_contract("v3.1")

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PLAN = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', "checks: none").replace(
    "visual: none\n", "visual:\n  url: http://127.0.0.1:9/\n  start: true\n")
PASS = "EVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS\n"


def _blocked_on_visual(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN)
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree, route_json FROM dispatches WHERE role='executor'").fetchone())
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    env.office("submit", cwd=wt, env={"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_JOBS": "manual"},
               check=0)
    from office import db, gates, state
    gate = dict(con.execute("SELECT * FROM gates WHERE kind='visual'").fetchone())
    with db.transaction(con):
        con.execute("UPDATE outbox SET status='done' WHERE status='queued'")
        gates.ingest_task_gate(con, state.get_run(con, d["run_id"]), gate["id"],
                               {"verdict": "UNAVAILABLE", "evidence_status": "CAPTURE_BLOCKED", "summary": "server down"})
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "blocked"
    producer = json.loads(d["route_json"])["candidate"]["model_id"]
    return con, producer


def _other_family(producer: str) -> str:
    return "claude/claude-sonnet-5" if not producer.startswith("claude") else "agy/gemini-3.8-flash@low"


def test_external_pass_from_another_family_accepts_the_task(env, tmp_path):
    con, producer = _blocked_on_visual(env)
    report = tmp_path / "review.md"
    report.write_text(PASS)
    by = _other_family(producer)
    code, out = env.office("approve", "visual", "T1", "--by", by, "--report", str(report), "--quote", "use gemini")
    assert code == 0, out
    assert "T1 accepted" in out, out
    gate = dict(con.execute("SELECT * FROM gates WHERE kind='visual' ORDER BY created_at DESC LIMIT 1").fetchone())
    assert gate["verdict"] == "PASS" and gate["route"] == by
    ev = con.execute("SELECT meta_json FROM evidence WHERE gate_id=? AND kind='review_output'", (gate["id"],)).fetchone()
    assert json.loads(ev[0])["external"] is True
    assert con.execute("SELECT quote FROM authorizations WHERE kind='external-visual'").fetchone()[0] == "use gemini"


def test_external_review_from_the_producers_model_family_is_accepted(env, tmp_path):
    con, producer = _blocked_on_visual(env)
    report = tmp_path / "review.md"
    report.write_text(PASS)
    harness = json.loads(con.execute("SELECT route_json FROM dispatches WHERE role='executor'").fetchone()[0])["candidate"]["harness"]
    code, out = env.office("approve", "visual", "T1", "--by", f"{harness}/{producer}", "--report", str(report),
                           "--quote", "fresh session of the same model")
    assert code == 0 and "T1 accepted" in out, out
    assert "not-independent" not in out and "cannot approve its own work" not in out


def test_invalid_or_unavailable_report_is_refused(env, tmp_path):
    con, producer = _blocked_on_visual(env)
    report = tmp_path / "review.md"
    report.write_text("looks fine to me\n")
    by = _other_family(producer)
    code, out = env.office("approve", "visual", "T1", "--by", by, "--report", str(report), "--quote", "ok")
    assert code != 0 and "not a valid visual review" in out, out
    report.write_text("EVIDENCE_STATUS: CAPTURE_BLOCKED\nVERDICT: UNAVAILABLE\n")
    code, out = env.office("approve", "visual", "T1", "--by", by, "--report", str(report), "--quote", "ok")
    assert code != 0, out
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "blocked"
