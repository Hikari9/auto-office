"""#453: a lane gate whose reviewer is revoked or unavailable is never gate-busy, `office revoke
<scope>:<kind>` cancels a running lane review, and the `next:` fallback hint and `office review
--report` agree. #454 (lane side): a visual failure whose cause is the plan's spec or the capture is
no blocking producer finding. Reviewers are scripted fake harnesses; no real agent runs."""
from __future__ import annotations

import json

from conftest import GOOD_ADD
from test_convergence_contract import (APPROVED, PLAN_VISUAL, _fake_capture, _gates, _q, _run_row, _scope, _start,
                                       _status, recheck, finding)

FAR = "2999-01-01T00:00:00+00:00"
WAIVE = ("approve", "waive", "L-T1:convergence", "--quote", "ship it", "--reason", "the reviewer was revoked")


def _unavailable_lane(env, kind="convergence_review", **script):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"exit": 1}], **script)
    env.office("dispatch", "T1", check=0)
    assert _gates(env, kind)[0]["review_status"] == "UNAVAILABLE"


def _rewind_to_running(env, *, live_reviewer=True, queued_job=True):
    """The lane review as it stands mid-flight: gate running, a live reviewer, a queued review job."""
    con = env.con()
    g = dict(con.execute("SELECT * FROM gates WHERE kind='convergence_review'").fetchone())
    con.execute("UPDATE gates SET status='running', verdict=NULL, review_status=NULL, summary=NULL, finished_at=NULL "
                "WHERE id=?", (g["id"],))
    row = con.execute("SELECT id, landing_json FROM runs").fetchone()
    landing = json.loads(row["landing_json"])
    landing["convergence"]["L-T1"].update(status="reviewing", fallback_available=False, fallback_kind=None)
    con.execute("UPDATE runs SET landing_json=? WHERE id=?", (json.dumps(landing), row["id"]))
    con.execute("DELETE FROM dispatches WHERE gate_id=?", (g["id"],))
    con.execute("DELETE FROM outbox WHERE kind='convergence_review'")
    con.execute("INSERT INTO dispatches(id, run_id, role, started_at, kind, status, launcher, gate_id, ended_at) "
                "VALUES('Drev', ?, 'code_reviewer', ?, 'reviewer', ?, NULL, ?, ?)",
                (row["id"], g["created_at"], "running" if live_reviewer else "failed", g["id"],
                 None if live_reviewer else g["created_at"]))
    if queued_job:
        con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, attempts, "
                    "max_attempts, not_before, created_at) VALUES('Jrev', ?, 'convergence_review', 'k-rev', ?, ?, 'queued', "
                    "0, 2, ?, ?)", (row["id"], json.dumps({"gate_id": g["id"], "scope": "L-T1"}),
                                    _run_row(env)["office_version"], FAR, g["created_at"]))
    con.commit()
    return g["id"]


def _gate(env, gid):
    return _q(env, "SELECT * FROM gates WHERE id=?", (gid,))[0]


def test_revoking_a_lane_review_ends_its_reviewer_and_job_and_closes_the_gate_unavailable(env, tmp_path):
    """#453: revoke the reviewer, waive, fall back: the whole sequence, with no gate-busy."""
    _unavailable_lane(env)
    gid = _rewind_to_running(env)
    code, out = env.office("approve", *WAIVE[1:])
    assert code != 0 and "gate-busy" in out, out  # a gate with a live reviewer and a queued job is busy
    code, out = env.office("revoke", "L-T1:convergence")
    assert code == 0 and "Drev" in out and "Jrev" in out, out
    gate = _gate(env, gid)
    assert (gate["status"], gate["review_status"], gate["verdict"]) == ("done", "UNAVAILABLE", None), gate
    assert "revoked" in gate["summary"]
    assert _q(env, "SELECT status, terminal_classification FROM dispatches WHERE id='Drev'")[0]["terminal_classification"] \
        == "revoked"
    job = _q(env, "SELECT status, error FROM outbox WHERE id='Jrev'")[0]
    assert job["status"] == "failed" and "revoked" in job["error"], job
    st = _scope(env, "L-T1")
    assert st["status"] == "unavailable" and st["fallback_available"] is True, st
    assert "office review L-T1:convergence --report" in _status(env)["next"], _status(env)["next"]
    # waive is not gate-busy on a revoked gate, and settles the scope
    code, out = env.office(*WAIVE)
    assert code == 0, out
    assert _scope(env, "L-T1")["status"] == "waived"
    # the waiver covers the gate: the fallback is refused, and the hint stops offering it
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out, out
    assert "no specialist reviewer returned a verdict" not in (_status(env).get("next") or "")


def test_a_revoked_lane_review_can_be_reviewed_by_the_orchestrator_instead(env, tmp_path):
    _unavailable_lane(env)
    _rewind_to_running(env)
    env.office("revoke", "L-T1:convergence", check=0)
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    env.office("review", "L-T1:convergence", "--report", str(report), check=0)
    assert _scope(env, "L-T1")["status"] == "approved"


def test_a_lane_gate_whose_reviewer_ended_is_reaped_unavailable_and_is_not_gate_busy_for_waive(env):
    _unavailable_lane(env)
    gid = _rewind_to_running(env, live_reviewer=False, queued_job=False)
    code, out = env.office(*WAIVE)
    assert code == 0, out
    assert _gate(env, gid)["review_status"] == "UNAVAILABLE"
    assert _scope(env, "L-T1")["status"] == "waived"


def test_an_orphaned_lane_gate_is_closed_unavailable_and_settles_the_scope(env):
    """The reaper path (dispatch._close_orphaned_gate -> gates.mark_unavailable) sets review_status too."""
    _unavailable_lane(env)
    gid = _rewind_to_running(env, live_reviewer=False, queued_job=False)
    con = env.con()
    from office import gates
    with con:
        gates.mark_unavailable(con, _run_row(env), gid, "its reviewer ended and no job is left to finish it")
    gate = _gate(env, gid)
    assert (gate["status"], gate["review_status"], gate["verdict"]) == ("done", "UNAVAILABLE", None)
    st = _scope(env, "L-T1")
    assert st["status"] == "unavailable" and st["fallback_available"] is True, st


def test_revoke_of_a_closed_or_unknown_lane_gate_changes_nothing(env):
    _unavailable_lane(env)
    code, out = env.office("revoke", "L-T1:convergence")
    assert code == 0 and "nothing to revoke" in out, out
    assert _gates(env, "convergence_review")[0]["review_status"] == "UNAVAILABLE"
    code, out = env.office("revoke", "L-T1:visual")
    assert code == 4 and "nothing-to-revoke" in out, out
    code, out = env.office("revoke", "L-T9:convergence")
    assert code == 2 and "unknown-scope" in out, out
    code, out = env.office("revoke", "L-T1:bogus")
    assert code == 2 and "bad-revoke-target" in out, out


def test_a_task_id_still_routes_to_the_task_revoke(env):
    _unavailable_lane(env)
    code, out = env.office("revoke", "T1")
    assert code == 0 and "lease revoked" in out, out


def test_the_hint_and_the_review_agree_on_the_current_cycle(env, tmp_path):
    """One predicate: a gate of an earlier cycle is neither hinted nor accepted by `office review`."""
    _unavailable_lane(env)
    assert "office review L-T1:convergence --report" in _status(env)["next"]
    con = env.con()
    row = con.execute("SELECT id, landing_json FROM runs").fetchone()
    landing = json.loads(row["landing_json"])
    landing["convergence"]["L-T1"]["cycle"] = 2  # `decide` moved on; no review of cycle 2 has run
    con.execute("UPDATE runs SET landing_json=? WHERE id=?", (json.dumps(landing), row["id"]))
    con.commit()
    assert "no specialist reviewer returned a verdict" not in (_status(env).get("next") or "")
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out, out


def test_the_fallback_flag_is_cleared_on_recompose_and_on_decide(env, monkeypatch):
    from office import convergence, jobs
    _unavailable_lane(env)
    assert _scope(env, "L-T1")["fallback_available"] is True
    con = env.con()
    run = _run_row(env)
    scope = convergence.find_scope(con, run, "L-T1")
    with con:  # a task re-accepted: the key moves, the scope recomposes
        convergence._set_scope(con, run, "L-T1", key="stale")
        convergence._consider(con, run, scope)
    st = _scope(env, "L-T1")
    assert st["status"] == "pending" and not st["fallback_available"], st
    with con:  # an escalated scope that still carries the flag
        convergence._set_scope(con, _run_row(env), "L-T1", status="escalated", fallback_available=True, fallback_kind="visual")
    monkeypatch.setattr(jobs, "kick", lambda *a, **k: 0)  # the next review would be unavailable again
    with con:
        convergence.decide(con, _run_row(env), "L-T1", "review", quote="review it again")
    st = _scope(env, "L-T1")
    assert st["cycle"] == 2 and not st["fallback_available"] and not st.get("fallback_kind"), st


# ------------------------------------------------------------------ #454: visual failures that are not the producer's


def _capture_with(monkeypatch, tmp_path, failures):
    shot = _fake_capture(monkeypatch, tmp_path)
    from office import visual
    inner = visual.capture_all

    def capture_all(con, run, task, rev, gate, worktree):
        return {**inner(con, run, task, rev, gate, worktree), "product_failures": failures}

    monkeypatch.setattr(visual, "capture_all", capture_all)
    return shot


def _visual_run(env):
    _start(env, plan=PLAN_VISUAL, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": "EVIDENCE_STATUS: COMPARABLE\n" + APPROVED}],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)


def test_a_product_failure_is_still_a_blocking_producer_finding(env, monkeypatch, tmp_path):
    _capture_with(monkeypatch, tmp_path, [{"location": "http://localhost:3999/ @ desktop", "summary": "page returned HTTP 500"}])
    _visual_run(env)
    gate = _gates(env, "visual")[0]
    assert gate["verdict"] == "RECHECK"
    assert _q(env, "SELECT task_id, blocking FROM findings WHERE gate_kind='visual' AND state='open'")[0]["blocking"] == 1


def test_a_spec_failure_is_unavailable_with_the_spec_named_and_no_producer_finding(env, monkeypatch, tmp_path):
    _capture_with(monkeypatch, tmp_path, [{"cause": "spec", "location": "button#buy @ desktop",
                                           "summary": "selector button#buy not found on the page"}])
    _visual_run(env)
    gate = _gates(env, "visual")[0]
    assert (gate["status"], gate["review_status"], gate["verdict"]) == ("done", "UNAVAILABLE", None), gate
    assert "visual spec defect" in gate["summary"] and "button#buy" in gate["summary"], gate["summary"]
    assert not _q(env, "SELECT 1 FROM findings WHERE gate_kind='visual'")
    assert _q(env, "SELECT status FROM tasks WHERE id='T1'")[0]["status"] != "changes_required"
    assert _scope(env, "L-T1")["status"] in ("unavailable", "reviewing")


def test_a_capture_failure_is_a_capture_problem_and_no_producer_finding(env, monkeypatch, tmp_path):
    _capture_with(monkeypatch, tmp_path, [{"cause": "capture", "location": "http://localhost:3999/ @ desktop",
                                           "summary": "state 'open' captured identical to default"}])
    _visual_run(env)
    gate = _gates(env, "visual")[0]
    assert (gate["status"], gate["review_status"], gate["evidence_status"]) == ("done", "EVIDENCE_BLOCKED", "CAPTURE_BLOCKED")
    assert "identical to default" in gate["summary"]
    assert not _q(env, "SELECT 1 FROM findings WHERE gate_kind='visual'")
    assert _q(env, "SELECT status FROM tasks WHERE id='T1'")[0]["status"] != "changes_required"
