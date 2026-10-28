"""#629 integrity regressions; use only isolated fake Office fixtures."""
from __future__ import annotations

import json
import pytest
from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row
from test_plan_review_lifecycle import _start, _revise, _ingest, _plan_gates, _run, _q, recheck

OLD = 'python3 -c "print(chr(70)+chr(65)+chr(73)+chr(76)+chr(69)+chr(68), 1); raise SystemExit(1)"'
FAIL_NEW = 'python3 -c "import calc; assert calc.add(2, 3) == 999"'
CHECK = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'

def _rows(env, sql, args=()):
    c = env.con()
    try:
        return [dict(x) for x in c.execute(sql,args)]
    finally:
        c.close()

@pytest.mark.approved
def test_accepted_new_failing_check_revalidates_and_revokes_landing(env):
    approved_run(env, executor=[{"write":{"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply":"VERDICT: PASS"}])
    env.office("dispatch","T1",check=0)
    assert task_row(env)["status"] == "accepted"
    previous = len(_rows(env,"SELECT * FROM gates WHERE kind='checks'"))
    rc,out = env.office("amend","T1","--add-check",FAIL_NEW,"--","New required behavior",check=0)
    assert rc==0,out
    rows = _rows(env,"SELECT * FROM gates WHERE kind='checks' ORDER BY rowid")
    assert len(rows)==previous+1, rows
    assert rows[-1]["status"]=="done", rows[-1]
    assert rows[-1]["verdict"]=="RECHECK", rows[-1]
    assert task_row(env)["status"]!="accepted",task_row(env)
    rc,data = env.ojson("status")
    assert "land it" not in json.dumps(data).lower(),data

def test_amended_new_check_is_not_preexisting_even_when_legacy_check_is(env):
    plan=PLAN_ONE.replace(f"checks: {CHECK}",f"checks: {OLD}")
    from test_preexisting_checks import _start as start_custom
    start_custom(env,plan,executor=[{"write":{"calc.py":GOOD_ADD},"submit":True}],
                 convergence_reviewer=[{"reply":"VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch","T1",check=0)
    assert task_row(env)["status"]=="accepted",task_row(env)
    rc,out=env.office("amend","T1","--add-check",FAIL_NEW,"--","Require strict result",check=0)
    assert rc==0,out
    latest=_rows(env,"SELECT * FROM gates WHERE kind='checks' ORDER BY rowid")[-1]
    assert latest["status"]=="done",latest
    assert latest["verdict"]=="RECHECK",latest
    assert task_row(env)["status"]!="accepted",task_row(env)

@pytest.mark.approved
def test_pending_amended_check_blocks_landing_before_job_runs(env):
    approved_run(env, executor=[{"write":{"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply":"VERDICT: PASS"}])
    env.office("dispatch","T1",check=0)
    assert task_row(env)["status"] == "accepted"
    rc,out=env.office("amend","T1","--add-check",FAIL_NEW,"--","New required check",
                     env={"OFFICE_JOBS":"manual"},check=0)
    assert rc==0,out
    assert task_row(env)["status"]!="accepted",task_row(env)
    pending=_rows(env,"SELECT * FROM gates WHERE kind='checks' ORDER BY rowid")[-1]
    assert pending["status"]=="queued", pending
    rc,data=env.ojson("status",env={"OFFICE_JOBS":"manual"})
    assert "land it" not in json.dumps(data).lower(),data


@pytest.mark.approved
def test_new_successful_check_restores_accepted_task_without_relaunch(env):
    approved_run(env, executor=[{"write":{"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply":"VERDICT: PASS"}])
    env.office("dispatch","T1",check=0)
    dispatch_before=task_row(env)["current_dispatch_id"]
    check='python3 -c "import calc; assert calc.add(10, -5) == 5"'
    rc,out=env.office("amend","T1","--add-check",check,"--","Reverify negative inputs",check=0)
    assert rc==0,out
    assert task_row(env)["status"]=="accepted",task_row(env)
    assert task_row(env)["current_dispatch_id"]==dispatch_before
    rows=_rows(env,"SELECT * FROM gates WHERE kind='checks' ORDER BY rowid")
    assert rows[-1]["status"]=="done" and rows[-1]["verdict"] in ("APPROVED", "PASS"),rows[-1]


def test_superseded_plan_verdict_cannot_erase_newer_recheck_or_spend_round(env):
    from office import db,plans,review_parse,state
    _start(env)
    first=_plan_gates(env)[0]["id"]
    _revise(env,PLAN_ONE.replace("- add() returns the sum","- add() returns the sum (p2)"))
    second=_plan_gates(env)[-1]["id"]
    _ingest(env,recheck(code="P2",what="new plan blocker"),gate_id=second)
    c=env.con()
    try:
        run=state.get_run(c,_run(env)["id"])
        reply=review_parse.parse(recheck(code="P1",what="superseded"),plan_review=True,contract="convergence-v1")
        with db.transaction(c):
            plans.ingest_plan_review(c,run,first,{"status":"COMPLETED","verdict":"RECHECK","parsed":reply,
                                                  "route":"fake/old","summary":"old plan"})
        assert {f["code"] for f in plans.blocking_findings(c,run)}=={"P2"}
        assert plans.substantive_rounds(c,run)==1
        assert next(g for g in plans.plan_gates(c,run["id"]) if g["id"]==first)["status"]=="stale"
    finally:
        c.close()

def test_p2_p3_review_reversal_never_spends_a_stale_round(env):
    from office import db,plans,review_parse,state
    _start(env)
    _ingest(env,recheck(code="P1",what="p1 blocker"))
    _revise(env,PLAN_ONE.replace("- add() returns the sum","- add() returns the sum p2"))
    p2=_plan_gates(env)[-1]["id"]
    _revise(env,PLAN_ONE.replace("- add() returns the sum","- add() returns the sum p3"))
    p3=_plan_gates(env)[-1]["id"]
    _ingest(env,recheck(code="P3",what="current blocker"),gate_id=p3)
    c=env.con()
    try:
        run=state.get_run(c,_run(env)["id"])
        old=review_parse.parse("VERDICT: APPROVED\\nNEXT proceed",plan_review=True,contract="convergence-v1")
        with db.transaction(c):
            plans.ingest_plan_review(c,run,p2,{"status":"COMPLETED","verdict":"APPROVED","parsed":old,
                                                  "route":"fake/stale","summary":"stale approval"})
        assert {f["code"] for f in plans.blocking_findings(c,run)}=={"P3"}
        assert plans.substantive_rounds(c,run)==2
        assert state.get_run(c,run["id"])["plan_review"]["status"]=="recheck"
    finally:
        c.close()


@pytest.mark.approved
def test_mutating_check_reports_exact_source_and_paths(env):
    from office import gates,state,db
    approved_run(env,executor=[{"write":{"calc.py":GOOD_ADD},"submit":True}],
                 code_reviewer=[{"reply":"VERDICT: PASS"}])
    env.office("dispatch","T1",check=0)
    c=env.con()
    try:
        run=state.get_run(c,_rows(env,"SELECT id FROM runs")[0]["id"])
        task=state.get_task(c,run["id"],"T1")
        rev=dict(c.execute("SELECT * FROM revisions WHERE id=?",(task["current_revision_id"],)).fetchone())
        d=state.get_dispatch(c,rev["dispatch_id"])
        with db.transaction(c):
            gid=gates._new_gate(c,run,task,rev["id"],"checks",f"mutation:{rev['id']}","queued")
        outcome=gates.run_commands(c,run,['printf mutated >> calc.py'],__import__("pathlib").Path(d["worktree"]),rev,{"id":gid,"task_id":"T1"})
        assert outcome["verdict"]=="STALE",outcome
        assert "calc.py" in outcome["summary"],outcome
        assert "mutat" in outcome["summary"].lower(),outcome
    finally:
        c.close()



@pytest.mark.approved
def test_restored_tracked_file_during_check_still_invalidates_gate(env):
    from pathlib import Path
    from office import gates, state, db
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    c = env.con()
    try:
        run = state.get_run(c, _rows(env, "SELECT id FROM runs")[0]["id"])
        task = state.get_task(c, run["id"], "T1")
        rev = dict(c.execute("SELECT * FROM revisions WHERE id=?", (task["current_revision_id"],)).fetchone())
        d = state.get_dispatch(c, rev["dispatch_id"])
        cwd = Path(d["worktree"])
        original = (cwd / "calc.py").read_bytes()
        with db.transaction(c):
            gid = gates._new_gate(c, run, task, rev["id"], "checks", f"restored:{rev['id']}", "queued")
        command = 'python3 -c "from pathlib import Path; p=Path(\'calc.py\'); original=p.read_bytes(); p.write_bytes(original+b\'X\'); p.write_bytes(original)"'
        outcome = gates.run_commands(c, run, [command], cwd, rev, {"id": gid, "task_id": "T1"})
        assert (cwd / "calc.py").read_bytes() == original  # final diff alone would miss this
        assert outcome["verdict"] == "STALE", outcome
        assert "calc.py" in outcome["mutating_check"]["paths"], outcome
    finally:
        c.close()


def test_integration_check_mutation_blocks_composed_acceptance(env):
    from office import state
    # Integration uses check_tree=False to bypass a task revision match, but
    # it must still detect that a command changed the composed source tree.
    plan = PLAN_ONE.replace("blast_radius: repo\n",
        "blast_radius: repo\nchecks: printf mutated >> calc.py\n")
    approved_run(env, plan=plan,
                 executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    landing = json.loads(_rows(env, "SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"] == "blocked", landing
    checks = _rows(env, "SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert checks and checks[-1]["verdict"] == "STALE", checks
    assert "calc.py" in checks[-1]["summary"], checks


def test_integration_check_identical_failure_on_base_is_disclosed_nonblocking(env):
    plan=PLAN_ONE.replace("blast_radius: repo\n",f"blast_radius: repo\nchecks: {OLD}\n")
    approved_run(env,plan=plan,executor=[{"write":{"calc.py":GOOD_ADD},"submit":True}],
                 convergence_reviewer=[{"reply":"VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch","T1",check=0)
    run_id=_rows(env,"SELECT id FROM runs")[0]["id"]
    landing=json.loads(_rows(env,"SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"]=="accepted",landing
    checks=_rows(env,"SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert len(checks)==1 and checks[0]["verdict"]=="PASS",checks
    assert "pre-existing" in checks[0]["summary"].lower(),checks
    assert _rows(env,"SELECT * FROM evidence WHERE kind='preexisting_check_output'"),"no baseline proof"
    assert _rows(env,"SELECT * FROM events WHERE kind='integration.preexisting'"),"no visible classification"


def test_integration_mixed_base_and_introduced_failure_remains_blocked(env):
    changed='python3 -c "from pathlib import Path; p=Path(\\x27calc.py\\x27).read_text(); print(\\x27FAILED new\\x27) if \\x27return a + b\\x27 in p else None; raise SystemExit(1 if \\x27return a + b\\x27 in p else 0)"'
    plan=PLAN_ONE.replace("blast_radius: repo\n",
        f"blast_radius: repo\nchecks:\n- {OLD}\n- {changed}\n")
    approved_run(env,plan=plan,executor=[{"write":{"calc.py":GOOD_ADD},"submit":True}],
                 convergence_reviewer=[{"reply":"VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch","T1",check=0)
    landing=json.loads(_rows(env,"SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"]=="blocked",landing
    assert "introduced failures" in landing["integration"]["detail"],landing
    checks=_rows(env,"SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert checks[-1]["verdict"]=="CHANGES_REQUIRED",checks
