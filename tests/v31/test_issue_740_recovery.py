"""Deterministic recovery regressions for #735, #718, #699, #665 and #709."""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, task_row
from test_convergence_contract import APPROVED, PLAN_GEAR, PLAN_SHARED, _gates, _run_row, _scope, _start, finding, recheck
from test_rerun import NO_RESUME, RESUMABLE
from test_stacked_restack import _commit, _rev_commit, _t2_on_superseded_t1
from test_wait import EXTERNAL, _go


def test_735_refused_plan_amend_does_not_leave_wait_without_a_worker(env):
    _start(env, gear=PLAN_GEAR, plan_reviewer=[{"reply": recheck(
        "FINDING P1 | high | blocking | T1 | acceptance needs clarification | amend the plan")}])
    assert _run_row(env)["plan_review"]["status"] == "recheck"
    assert env.office("wait", "--timeout", "0")[0] == 0
    env.write_plan("# Invalid draft\n\n## Tasks\n### T1: broken\ndepends: T404\n")
    code, _ = env.office("amend", "plan", "--contract", "--", "clarify task acceptance")
    assert code != 0
    code, out = env.office("wait", "--timeout", "0")
    assert code == 3 and "orchestrator action" in out and "amend" in out, out
    assert "nothing new" not in out


@pytest.mark.approved
def test_735_repeated_action_keeps_one_notification_and_returns_your_move(env):
    _go(env)
    env.office("revoke", "T1", check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at WHERE role='executor'")
    con.commit()
    assert env.office("wait", "--timeout", "0")[0] == 0
    for _ in range(2):
        code, out = env.office("wait", "--timeout", "0")
        assert code == 3 and "orchestrator action" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='wait.action_reported'").fetchone()[0] == 1


def _stranded_ack(env, adapter="agy", session=None):
    from office import state
    _go(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    did = task_row(env)["current_dispatch_id"]
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, adapter_id=?, session_id=? WHERE id=?",
                (adapter, session, did))
    con.execute("UPDATE tasks SET status='changes_required', pause_reason=NULL WHERE id='T1'")
    con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, "
                "content, created_at) VALUES('DLdead', ?, 'A19', 'T1', ?, 2, 'delivered', 'apply amended contract', '2026-01-01')",
                (run["id"], did))
    con.commit()
    return con, run, did


@pytest.mark.approved
@pytest.mark.parametrize("adapter,session", [("agy", "stored-but-not-resumable"), ("codex", None)])
def test_718_dead_amendment_recipient_is_a_stall_with_fresh_recovery(env, adapter, session):
    con, run, did = _stranded_ack(env, adapter, session)
    code, out = env.office("status")
    assert code == 0 and "blocker:" in out and "A19" in out and did in out, out
    assert "office rerun T1 --fresh" in out.splitlines()[-1] and "--resume" not in out.splitlines()[-1], out
    code, out = env.office("wait", "--timeout", "0")
    assert code == 3 and "stall:" in out and "A19" in out, out
    con.execute("UPDATE deliveries SET status='applied', applied_at='2026-01-02'")
    con.commit()
    from office import guide
    assert not any("A19" in line for line in guide.stalls(con, run))


@pytest.mark.approved
def test_718_live_nonresumable_worker_can_ack_without_a_false_stall(env):
    from office import guide
    con, run, did = _stranded_ack(env)
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE id=?", (did,))
    con.commit()
    assert not any("A19" in line for line in guide.stalls(con, run))


@pytest.mark.parametrize("adapter,session,expected", [(NO_RESUME, "S1", "--fresh"),
                                                        (RESUMABLE, None, "--fresh"),
                                                        (RESUMABLE, "S1", "--resume|--fresh")])
def test_699_rerun_guidance_uses_actual_adapter_resume_capability(monkeypatch, adapter, session, expected):
    from office import adapters, convergence, rerun
    parent = {"session_id": session, "adapter_id": "claude", "model": "m", "effort": "high"}
    monkeypatch.setattr(rerun, "_last_ended_executor", lambda con, run, tid: parent)
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": adapter})
    assert convergence._rerun_modes(None, {}, "T4") == expected


@pytest.mark.review_contract("v3.1")
def test_699_stale_status_wait_and_resume_refusal_offer_only_fresh(env, monkeypatch):
    import copy
    from office import adapters
    _, _, w2, wt = _t2_on_superseded_t1(env)
    _commit(env, wt, {"mul.py": GOOD_MUL}, "dependent awaiting restack")
    env.office("submit", cwd=wt, env=w2, check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, session_id='stored-but-not-resumable', "
                "adapter_id='claude' WHERE id=?", (w2["OFFICE_DISPATCH_ID"],))
    con.commit()
    current = copy.deepcopy(adapters.load_all())
    current["claude"]["office_profiles"]["worker"]["interactive"].pop("resume_argv", None)
    monkeypatch.setattr(adapters, "load_all", lambda: current)
    for args in (("status",), ("wait", "--timeout", "0")):
        _, out = env.office(*args)
        assert "office rerun T2 --fresh" in out and "--resume" not in out, out
    code, out = env.office("rerun", "T2", "--resume", env=EXTERNAL)
    assert code == 4 and "resume-impossible" in out, out
    assert out.splitlines()[-1] == "next: office rerun T2 --fresh", out


@pytest.mark.review_contract("v3.1")
def test_709_stale_recorded_base_is_never_presented_as_actual_ancestry(env):
    old, accepted, w2, wt = _t2_on_superseded_t1(env)
    _commit(env, wt, {"mul.py": "def mul(a, b): return a * b\n"}, "dependent")
    env.office("submit", cwd=wt, env=w2, check=0)
    con = env.con()
    con.execute("UPDATE revisions SET base_commit=? WHERE id=?", (_rev_commit(env, accepted), task_row(env, "T2")["current_revision_id"]))
    con.commit()
    code, out = env.office("status")
    assert code == 0
    assert f"built on T1 {accepted}, but T1 was accepted on {accepted}" not in out, out
    assert _rev_commit(env, old)[:7] in out, out
    assert _rev_commit(env, accepted)[:7] in out, out


@pytest.mark.approved
def test_709_quota_blocked_route_is_not_repeated_in_recovery_guidance(env, monkeypatch):
    from office import guide, rerun, state
    _go(env)
    con = env.con()
    did = task_row(env)["current_dispatch_id"]
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, session_id='old-session' WHERE id=?", (did,))
    con.execute("UPDATE tasks SET status='changes_required', pause_reason='restack: parent changed' WHERE id='T1'")
    con.commit()
    monkeypatch.setattr(rerun, "_recovery_route", lambda *args: (None, "projected quota crosses reserve"))
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    hint = guide.next_action(con, run)
    assert "office inspect route T1" in hint and "projected quota crosses reserve" in hint, hint
    assert "office rerun T1 --resume" not in hint and "office rerun T1 --fresh" not in hint, hint


def test_recovery_hint_reads_pending_trust_changes_without_committing_them(env):
    from office import db, rerun, state
    from test_issue_707_route_authority import FB1, _approve, _ended, _latest, _quota
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, env=_quota(env), check=0)
    _ended(env)
    con = env.con()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])
    before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple LIKE 'codex@%'").fetchone()[0]
    assert before > 0
    with pytest.raises(RuntimeError, match="roll back the caller"):
        with db.transaction(con):
            con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
            hint = rerun.recovery_hint(con, run, "T1")
            assert "office inspect route T1" in hint and "trust" in hint, hint
            assert con.in_transaction
            raise RuntimeError("roll back the caller")
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple LIKE 'codex@%'").fetchone()[0] == before
    assert not con.in_transaction


def test_recovery_hint_rechecks_a_changed_future_pin_before_offering_fresh(env):
    from office import rerun, state
    from test_issue_721_accepted_route import NEW, QUOTE, _accepted
    accepted = _accepted(env)
    env.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL, check=0)
    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    (env.tmp / "user-config.yaml").write_text(f"routing:\n  user_policy:\n    denied_models: [{NEW}]\n")
    run = state.get_run(con, accepted["run_id"])
    hint = rerun.recovery_hint(con, run, "T1")
    assert "office inspect route T1" in hint and "denied" in hint and "gpt-6-astra" in hint, hint
    assert "office rerun T1" not in hint, hint


@pytest.mark.review_contract("v3.1")
def test_709_conflicting_restack_is_in_resumed_prompt_and_updated_brief(env, monkeypatch):
    from office import adapters, rerun
    old, accepted, w2, wt = _t2_on_superseded_t1(env)
    _commit(env, wt, {"calc.py": GOOD_ADD + "# dependent conflicting change\n"}, "conflict")
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at=started_at, status='exited', session_id='resumable', adapter_id='claude' WHERE id=?",
                (w2["OFFICE_DISPATCH_ID"],))
    con.commit()
    monkeypatch.setattr(rerun, "agent_alive", lambda d: False)
    monkeypatch.setattr(rerun, "_sticky_check", lambda *args: None)
    monkeypatch.setattr(adapters, "resume_argv", lambda *args, **kwargs: (["--resume", "resumable"], "claude"))
    env.office("rerun", "T2", "--resume", env=EXTERNAL, check=0)
    did = task_row(env, "T2")["current_dispatch_id"]
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert "RESTACK FIRST" in resume["findings"] and "resolve conflicts" in resume["findings"]
    assert _rev_commit(env, accepted) in resume["findings"]
    brief = next((env.state / "runs").glob(f"*/dispatches/{did}/brief.md")).read_text()
    assert f"git merge {_rev_commit(env, accepted)}" in brief and "Never rebase or force-push" in brief


def test_665_last_recheck_dismissal_uses_remaining_round_without_waiver(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["status"] == "recheck"
    env.office("disposition", "L-T1:F1", "dismissed", "--quote", "the requested behavior is intentionally deferred", check=0)
    reviews = _gates(env, "convergence_review")
    assert len(reviews) == 2 and (reviews[-1]["cycle"], reviews[-1]["round"], reviews[-1]["verdict"]) == (1, 2, "APPROVED")
    assert not env.con().execute("SELECT 1 FROM authorizations WHERE kind='waiver'").fetchone()


def test_665_dismissal_releases_a_shared_scope_with_an_existing_approval(env):
    from office import convergence
    _start(env, plan=PLAN_SHARED,
           executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", check=0)
    con = env.con()
    shared = next(s["id"] for s in convergence.summary(con, _run_row(env)) if s["shared"])
    assert _scope(env, shared)["status"] == "approved"
    env.write_plan(PLAN_SHARED.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    env.script(executor=[{"write": {"calc.py": GOOD_ADD + "# amended contract\n"}, "submit": True}],
               convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("amend", "T1", "--contract", "--", "allow helpers while keeping integer behavior", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] == "recheck"
    before = len([g for g in _gates(env, "convergence_review") if g["scope"] == "L-T1"])
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("disposition", "L-T1:F1", "dismissed", "--quote", "integer behavior is intentional", check=0)
    reviews = [g for g in _gates(env, "convergence_review") if g["scope"] == "L-T1"]
    assert len(reviews) == before + 1 and reviews[-1]["verdict"] == "APPROVED"
    states = {s["id"]: s["status"] for s in convergence.summary(con, _run_row(env))}
    assert all(status in convergence.CONVERGED for status in states.values()), states
    _, out = env.office("status")
    assert f"{shared} pending" not in out and "lane convergence is running" not in out, out


def test_665_resume_recovers_a_preexisting_stranded_dismissal_without_new_consent(env, monkeypatch):
    from office import convergence
    gap = ("VERDICT: INTAKE_GAP\n" + finding("F1") +
           "\nDECISION accept floats?\nWHY only ints are specified\nAFFECTS T1\nNEXT ask")
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": gap}])
    env.office("dispatch", "T1", check=0)
    env.office("decide", "L-T1", "continue", "--quote", "continue with integers", check=0)
    # Recreate the older runtime's transaction: disposition and acceptance are
    # durable, but it never enqueued the next cycle's review.
    with monkeypatch.context() as m:
        m.setattr(convergence, "_queue_dismissal_review", lambda *args: None)
        env.office("disposition", "L-T1:F1", "dismissed", "--quote", "integer-only is intentional", check=0)
    assert _scope(env, "L-T1")["status"] == "recheck"
    assert task_row(env)["status"] == "accepted"
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("resume", check=0)
    assert _scope(env, "L-T1")["status"] == "approved"
    assert len(_gates(env, "convergence_review")) == 2
    assert env.con().execute("SELECT COUNT(*) FROM authorizations WHERE kind='decision'").fetchone()[0] == 1


@pytest.mark.parametrize("verdict", ["INTAKE_GAP", "RECHECK"])
def test_665_last_dismissal_in_authorized_new_cycle_queues_independent_review(env, verdict):
    gap = ("VERDICT: INTAKE_GAP\n" + finding("F1") +
           "\nDECISION should add support floats?\nWHY requirements name ints only\nAFFECTS T1\nNEXT ask")
    replies = ([{"reply": gap}] if verdict == "INTAKE_GAP" else [{"reply": recheck(finding("F1"))}] * 3)
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=replies)
    env.office("dispatch", "T1", check=0)
    if verdict == "RECHECK":
        for i in range(2):
            env.script(executor=[{"write": {"calc.py": GOOD_ADD + f"# repair{i}\n"}, "submit": True}],
                       convergence_reviewer=[{"reply": recheck(finding("F1"))}])
            env.office("rerun", "T1", "--fresh", check=0)
    env.office("decide", "L-T1", "continue", "--quote", "continue within the existing contract", check=0)
    assert _scope(env, "L-T1")["cycle"] == 2
    before = len(_gates(env, "convergence_review"))
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("disposition", "L-T1:F1", "dismissed", "--quote", "defer float support, integers remain the requirement", check=0)
    reviews = _gates(env, "convergence_review")
    assert len(reviews) == before + 1, reviews
    assert (reviews[-1]["cycle"], reviews[-1]["round"], reviews[-1]["verdict"]) == (2, 1, "APPROVED")
    assert _scope(env, "L-T1")["status"] == "approved"
    assert not env.con().execute("SELECT 1 FROM authorizations WHERE kind='waiver'").fetchone()
    env.office("resume", check=0)
    assert len(_gates(env, "convergence_review")) == before + 1
