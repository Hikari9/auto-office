"""Submission, gates, convergence bounds, amendments, and terminal classification."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, approved_run, task_row


EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid, "OFFICE_ROLE": "executor"}, Path(d["worktree"])


@pytest.mark.approved
def test_duplicate_submit_reuses_the_operation(env):
    # The executor submits, then its response is "lost" and it submits again.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    before = (con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], con.execute("SELECT COUNT(*) FROM gates").fetchone()[0],
              con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], con.execute("SELECT plan_version FROM runs").fetchone()[0])
    wenv, wt = _worker(env)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "already submitted" in out, out
    after = (con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], con.execute("SELECT COUNT(*) FROM gates").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], con.execute("SELECT plan_version FROM runs").fetchone()[0])
    assert before == after
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 1


@pytest.mark.approved
def test_dirty_edit_with_unchanged_head_invalidates_prior_pass(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    code, out = env.office("submit", cwd=wt, env={**wenv, "OFFICE_JOBS": "manual"})
    con = env.con()
    rid = con.execute("SELECT id FROM runs").fetchone()[0][:8]
    R1, R2 = f"R1-{rid}", f"R2-{rid}"
    assert f"rev {R1} captured" in out, out
    (wt / "calc.py").write_text(GOOD_ADD + "\n# changed without a commit\n")
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == head
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert f"rev {R2} captured" in out and f"supersedes {R1}" in out, out
    con = env.con()
    r1 = {r["kind"]: r["status"] for r in con.execute("SELECT kind, status FROM gates WHERE revision_id=?", (R1,))}
    assert r1 == {"checks": "cancelled", "code_review": "cancelled"}, r1
    assert con.execute("SELECT verdict FROM gates WHERE revision_id=? AND kind='code_review'", (R2,)).fetchone()[0] == "PASS"
    assert task_row(env)["accepted_revision_id"] == R2
    # Accepted work is closed to further edits from that session.
    (wt / "calc.py").write_text(GOOD_ADD + "\n# after acceptance\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "lease" in out


@pytest.mark.approved
def test_stale_pass_is_audit_only(env):
    from office import db, gates, review_parse, state
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    wenv, wt = _worker(env)
    env.office("submit", cwd=wt, env={**wenv, "OFFICE_JOBS": "manual"}, check=0)
    (wt / "calc.py").write_text(GOOD_ADD + "\n# v2\n")
    env.office("submit", cwd=wt, env={**wenv, "OFFICE_JOBS": "manual"}, check=0)
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    stale_gate = con.execute("SELECT id FROM gates WHERE revision_id=? AND kind='checks'", (f"R1-{run['id'][:8]}",)).fetchone()[0]
    con.execute("UPDATE gates SET status='running' WHERE id=?", (stale_gate,))
    with db.transaction(con):
        gates.ingest_task_gate(con, run, stale_gate, {"verdict": "PASS", "parsed": review_parse.Parsed(verdict="PASS"),
                                                      "route": "late"})
    row = con.execute("SELECT status, verdict FROM gates WHERE id=?", (stale_gate,)).fetchone()
    assert row["status"] == "stale" and row["verdict"] == "PASS"
    assert task_row(env)["status"] != "accepted"


@pytest.mark.approved
def test_amendment_delivered_but_not_applied_cannot_satisfy(env, monkeypatch):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("amend", "T1", "--", "also handle negative numbers the same way")
    assert code == 0 and "A1" in out and "delivering to T1" in out, out
    # The worker's next command carries the delivery; it has not applied it.
    code, out = env.office("status", cwd=wt, env=wenv)
    assert "AMENDMENT A1 delivered" in out and "office ack A1" in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "amendment pending" in out, out
    assert task_row(env)["status"] != "accepted"
    con = env.con()
    assert con.execute("SELECT status FROM deliveries").fetchone()[0] == "delivered"
    code, out = env.office("ack", "A1", cwd=wt, env=wenv)
    assert code == 0 and "applied" in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "captured" in out
    assert task_row(env)["status"] == "accepted"


@pytest.mark.approved
def test_superseded_amendment_ack_is_rejected(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    env.office("amend", "T1", "--", "first delta", check=0)
    env.office("amend", "T1", "--", "second delta", check=0)
    code, out = env.office("ack", "A1", cwd=wt, env=wenv)
    assert code == 4 and "superseded" in out and "A2" in out and "first delta" in out, out
    code, out = env.office("ack", "A2", cwd=wt, env=wenv)
    assert code == 0, out
    con = env.con()
    assert con.execute("SELECT applied_plan_version FROM dispatches WHERE id=?", (wenv["OFFICE_DISPATCH_ID"],)).fetchone()[0] == 3


@pytest.mark.approved
def test_crash_after_amendment_commit_redelivers_without_rebump(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    env.office("amend", "T1", "--", "rename nothing, just a delta", check=0)
    con = env.con()
    v = con.execute("SELECT plan_version FROM runs").fetchone()[0]
    # Lost response: the worker saw nothing. Resume/next command redelivers.
    for _ in range(2):
        code, out = env.office("status", cwd=wt, env=wenv)
        assert "AMENDMENT A1 delivered" in out
    assert con.execute("SELECT plan_version FROM runs").fetchone()[0] == v
    assert con.execute("SELECT delivered_count FROM deliveries").fetchone()[0] == 2


def test_repeated_finding_stops_after_one_escalation(env):
    finding = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | add ignores overflow | clamp the result"
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                       {"write": {"calc.py": GOOD_ADD + "# try 2\n"}, "submit": True},
                       {"write": {"calc.py": GOOD_ADD + "# try 3\n"}, "submit": True}],
        code_reviewer=[{"reply": finding}])
    env.office("dispatch", "T1", check=0)
    # Each fix round is the orchestrator's choice now (R8): rerun fresh until
    # convergence gives up.
    for _ in range(3):
        if task_row(env)["status"] != "changes_required":
            break
        env.office("rerun", "T1", "--fresh", check=0)
    t = task_row(env)
    assert t["status"] == "paused", t
    assert "escalation" in t["pause_reason"] or "exhausted" in t["pause_reason"], t["pause_reason"]
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM gates WHERE escalated=1").fetchone()[0] == 1
    reviews = [c for c in env.calls() if c["role"] == "code_reviewer"]
    assert len(reviews) <= 4, len(reviews)
    code, data = env.ojson("status")
    assert "resolve T1" in data["next"]


@pytest.mark.approved
def test_invalid_reviewer_reply_is_kept_not_substituted(env):
    # R11: an unparseable reply is the reviewer's work, not a route failure. No
    # other reviewer is tried and the gate is never UNAVAILABLE for this reason.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "Looks great to me!"}])
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert t["status"] == "blocked" and "needs attention" in t["pause_reason"], t
    con = env.con()
    g = con.execute("SELECT verdict, env_failures FROM gates WHERE kind='code_review'").fetchone()
    assert g["verdict"] == "ATTENTION" and g["env_failures"] == 0
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE role='code_reviewer'").fetchone()[0] == 1


@pytest.mark.approved
def test_quota_failure_substitutes_route(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        **{"codex:code_reviewer": [{"stderr": "Error: usage limit reached (429)", "exit": 1}],
           "claude:code_reviewer": [{"reply": "VERDICT: PASS"}], "gemini:code_reviewer": [{"reply": "VERDICT: PASS"}]})
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE role='code_reviewer' AND outcome='environment_failure'").fetchone()[0] == 1


def test_missing_check_command_is_never_pass(env):
    plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', "checks: definitely-not-a-command --x")
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert t["status"] == "blocked", t
    con = env.con()
    assert con.execute("SELECT verdict FROM gates WHERE kind='checks'").fetchone()[0] == "UNAVAILABLE"
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 0


@pytest.mark.approved
def test_process_endings_always_classified(env):
    approved_run(env, executor=[{"exit": 0}, {"exit": 3}, {"signal": "TERM"}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    rows = con.execute("SELECT terminal_classification, exit_code, signal FROM dispatches WHERE role='executor' "
                       "ORDER BY started_at").fetchall()
    kinds = [r["terminal_classification"] for r in rows]
    assert kinds[:3] == ["success", "nonzero", "signal"], kinds
    assert rows[1]["exit_code"] == 3 and rows[2]["signal"] == 15
    t = task_row(env)
    assert t["status"] == "blocked" and "without submitting" in t["pause_reason"]


def test_parallel_independent_tasks_accept_and_integrate(env):
    approved_run(env, plan=PLAN_TWO,
        executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": "def mul(a, b):\n    return a * b\n"}},
                   "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", "--parallel")
    assert code == 0 and "T1 ->" in out and "T2 ->" in out, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    con = env.con()
    integ = con.execute("SELECT landing_json FROM runs").fetchone()[0]
    assert '"status": "accepted"' in integ


def test_shared_scope_cannot_be_double_held(env):
    plan = PLAN_TWO.replace("scope: mul.py", "scope: calc.py")
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", "--parallel")
    assert code == 4 and "scope-held" in out, out


def test_stacked_dispatch_launches_the_next_task_after_acceptance(env):
    approved_run(env, plan=PLAN_TWO,
        executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": "def mul(a, b):\n    return a * b\n"}},
                   "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2")
    assert code == 0 and "T2 stacked after T1" in out, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    con = env.con()
    base_t2 = con.execute("SELECT base_commit FROM dispatches WHERE task_id='T2' AND role='executor'").fetchone()[0]
    t1_commit = con.execute("SELECT r.commit_sha FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id "
                            "WHERE t.id='T1'").fetchone()[0]
    assert base_t2 == t1_commit  # T2 built on T1's accepted revision


def test_integration_conflict_is_surfaced_not_landed(env):
    plan = PLAN_TWO.replace("scope: mul.py", "scope: mul.py, README.md").replace("scope: calc.py\n", "scope: calc.py, README.md\n")
    approved_run(env, plan=plan,
        executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD, "README.md": "one\n"},
                                     "T2": {"mul.py": "def mul(a, b):\n    return a * b\n", "README.md": "two\n"}},
                   "submit": True}],
        code_reviewer=[{"reply": "VERDICT: PASS"}], integration_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)  # not stacked: T2 starts from the run base too
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    assert "integration conflict" in data["next"], data["next"]
    code, out = env.office("close", "--handoff", "x")
    assert code == 4 and "integration conflict" in out, out


@pytest.mark.approved
def test_revision_ids_are_unique_across_runs(env):
    """revisions.id is a global primary key: a second run's first revision must not collide
    with any earlier run's R1 (regression: every run after the first failed office submit)."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    # A foreign run already holding the bare legacy id and this run's would-be id prefix pattern.
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, commit_sha, tree_sha, requirements_version, plan_version, "
                "applied_version, env_fingerprint, operation_id, status, created_at) VALUES('R1','other-run','T1',1,'x','x',1,1,1,'x','op-foreign','current','t')")
    con.commit()
    wenv, wt = _worker(env)
    code, out = env.office("submit", cwd=wt, env={**wenv, "OFFICE_JOBS": "manual"}, check=0)
    assert f"rev R1-{run_id[:8]} captured" in out, out


PLAN_NO_GATES = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', "checks: none")


def test_no_gate_revision_is_accepted_on_submit(env):
    # Direct gear funds no review and the task declares checks: none, so no gate
    # exists to finish; acceptance must be evaluated on submit (it hung "submitted").
    approved_run(env, plan=PLAN_NO_GATES, gear="direct", executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    code, out = env.office("submit", cwd=wt, env=wenv, check=0)
    assert code == 0 and "accepted (no gate required by policy)" in out, out
    assert task_row(env)["status"] == "accepted"


def test_stuck_no_gate_revision_is_accepted_by_reconcile(env):
    approved_run(env, plan=PLAN_NO_GATES, gear="direct", executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    env.office("submit", cwd=wt, env=wenv, check=0)
    # Put it back the way an older runtime left it: submitted, no gates, never evaluated.
    con = env.con()
    con.execute("UPDATE tasks SET status='submitted', accepted_revision_id=NULL WHERE id='T1'")
    con.commit()
    assert task_row(env)["status"] == "submitted"
    env.office("status", check=0)
    assert task_row(env)["status"] == "accepted"


def test_revision_with_checks_is_not_accepted_without_them(env):
    # The no-gate path must not accept a task whose checks never ran.
    approved_run(env, gear="direct", executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    env.office("submit", cwd=wt, env={**wenv, "OFFICE_JOBS": "manual"}, check=0)
    env.office("status", check=0)
    assert task_row(env)["status"] != "accepted"


@pytest.mark.approved
def test_not_logged_in_reviewer_skips_the_whole_harness(env):
    # Every route on a harness that is not signed in fails the same way; the
    # retry must not spend the bound on the same harness's other models.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        **{"codex:code_reviewer": [{"stderr": "Not logged in · Please run /login", "exit": 1}],
           "claude:code_reviewer": [{"reply": "VERDICT: PASS"}], "gemini:code_reviewer": [{"reply": "VERDICT: PASS"}]})
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    reviewers = [c["harness"] for c in env.calls() if c.get("role") == "code_reviewer"]
    assert reviewers.count("codex") == 1 and reviewers[-1] != "codex", reviewers


@pytest.mark.approved
def test_amendment_for_an_earlier_session_does_not_hold_a_relaunch(env):
    # An amendment delivered to D1, then the task relaunched as D2 (e.g. an
    # external relaunch): D2 starts from the amended contract, so its submit must
    # not sit at amendment_pending waiting on an ack only D1 could give.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("amend", "T1", "--", "also handle negative numbers the same way", check=0)
    env.office("revoke", "T1", check=0)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 0, out
    con = env.con()
    assert [r[0] for r in con.execute("SELECT status FROM deliveries")] == ["superseded"]
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "captured" in out and "amendment pending" not in out, out
    assert task_row(env)["status"] == "accepted"


@pytest.mark.approved
def test_requirements_amendment_follows_a_relaunch_to_the_new_session(env):
    # A requirements change targets a plan version past the task contract, so the
    # relaunch rule above does not supersede it. It must move to the new session
    # (the only one that can ack it) instead of holding every submit forever.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("amend", "requirements", "--quote", "also log each call", "--", "R9: log each call", check=0)
    env.office("approve", "plan", "--quote", "approve", check=0)
    con = env.con()
    # Other tasks' amendments had moved the plan past this task's contract (the
    # live run: A7 at p6 against a p2 contract).
    con.execute("UPDATE deliveries SET target_version=target_version+4 WHERE status IN ('queued','delivered')")
    con.commit()
    env.office("revoke", "T1", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    current = task_row(env)["current_dispatch_id"]
    pending = con.execute("SELECT amendment_id, dispatch_id FROM deliveries WHERE status IN ('queued','delivered')").fetchall()
    assert pending and all(r["dispatch_id"] == current for r in pending), [dict(r) for r in con.execute("SELECT amendment_id, status, dispatch_id, target_version FROM deliveries")]
    _, out = env.office("status", check=0)
    assert "T1 waiting:" in out and pending[0]["amendment_id"] in out, out
    wenv, wt = _worker(env)
    for r in pending:
        env.office("ack", r["amendment_id"], cwd=wt, env=wenv, check=0)
    (wt / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "amendment pending" not in out, out
    assert task_row(env)["status"] == "accepted"


@pytest.mark.approved
def test_current_session_can_ack_an_amendment_left_on_an_ended_session(env):
    # State an older runtime left behind: the delivery still names an ended
    # session. The task's current session acks it instead of being refused.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    first = task_row(env)["current_dispatch_id"]
    env.office("revoke", "T1", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("amend", "T1", "--", "also handle negative numbers the same way", check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at=COALESCE(ended_at, started_at), status='cancelled' WHERE id=?", (first,))
    con.execute("UPDATE deliveries SET dispatch_id=? WHERE status IN ('queued','delivered')", (first,))
    con.commit()
    aid = con.execute("SELECT amendment_id FROM deliveries WHERE status IN ('queued','delivered')").fetchone()[0]
    wenv, wt = _worker(env)
    code, out = env.office("ack", aid, cwd=wt, env=wenv)
    assert code == 0 and "applied" in out, out


@pytest.mark.approved
def test_headless_worker_past_its_wall_cap_is_stopped_and_surfaces(env):
    # agy once idled 3.5h past its own --print-timeout; the supervisor's cap
    # stops it, relaunches within the bound, then blocks with a named reason.
    approved_run(env, executor=[{"sleep": 60}, {"sleep": 60}, {"sleep": 60}])
    code, out = env.office("dispatch", "T1", env={"OFFICE_WORKER_MAX_MINUTES": "0.02"}, check=0)
    t = task_row(env)
    assert t["status"] == "blocked" and "timeout" in (t["pause_reason"] or ""), t
    con = env.con()
    assert {r[0] for r in con.execute("SELECT terminal_classification FROM dispatches WHERE role='executor'")} == {"timeout"}


def test_agy_profiles_carry_a_wall_cap():
    import sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import adapters, dispatch
    agy = adapters.load_all()["agy"]
    assert dispatch._wall_cap_seconds(adapters.profile(agy, "worker")) == 50 * 60
    assert dispatch._wall_cap_seconds(adapters.profile(adapters.load_all()["claude"], "worker")) is None


@pytest.mark.approved
def test_dispatch_of_a_task_blocked_on_an_unavailable_review_reruns_only_the_review(env):
    # rock-mcp run 2fc0f696 C5: `dispatch T2 --review-as ...` on a submitted
    # revision blocked by an UNAVAILABLE code review launched a fresh executor.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", check=0)
    blocked = task_row(env)
    assert blocked["status"] == "blocked", blocked
    executors = [c for c in env.calls() if c["role"] == "executor"]
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    producer = env.con().execute("SELECT d.model FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id").fetchone()[0]
    reviewer = "codex/gpt-6-luna@xhigh" if "claude" in producer else "claude/claude-opus-5-5@high"
    code, out = env.office("dispatch", "T1", "--review-as", reviewer)
    assert code == 0 and "code review re-run" in out and "no executor launched" in out, out
    t = task_row(env)
    assert t["current_revision_id"] == blocked["current_revision_id"]
    assert t["current_dispatch_id"] == blocked["current_dispatch_id"]
    assert [c for c in env.calls() if c["role"] == "executor"] == executors
    assert t["status"] == "accepted", t
    con = env.con()
    rows = con.execute("SELECT verdict FROM gates WHERE kind='code_review' ORDER BY created_at").fetchall()
    assert [r[0] for r in rows] == ["UNAVAILABLE", "PASS"]
    assert json.loads(t["review_override_json"])["as"] == reviewer
