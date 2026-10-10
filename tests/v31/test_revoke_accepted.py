"""`office revoke` keeps an accepted task accepted when its accepted revision is still its current one.

An amendment relaunch of an accepted task holds the lease with no new revision. Revoking that worker used to
pause the task with "lease revoked ... relaunch", although its accepted work was untouched: dependents stayed
blocked and the orchestrator had to rerun a task that needed nothing. Revision identity decides, not status
(`amend._deliver` already demoted the task to changes_required, a refused submit already set it blocked).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_TWO, approved_run, self_reviewed, task_row
from test_dormant_lease import PLAN_HELD, _end, _t2_submitted_and_held
from test_self_review_ledger import write_ledger

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
DELTA = "also reject non-numeric arguments with a TypeError"
# T2 builds on T1 and edits the same file, so T1's live lease holds T2's scope.
PLAN_DEPENDENT = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py, calc.py\ndepends: T1")
T1_ACCEPTED = [{"write_by_task": {"T1": {"calc.py": GOOD_ADD}}, "submit": True}]


def _accepted(env, plan=PLAN_DEPENDENT):
    approved_run(env, plan=plan, executor=T1_ACCEPTED, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    t1 = task_row(env, "T1")
    assert t1["status"] == "accepted" and t1["accepted_revision_id"] == t1["current_revision_id"], t1
    return t1


def _worker(env, tid="T1"):
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env, tid)["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _leases(env, tid="T1"):
    return [dict(r) for r in env.con().execute("SELECT * FROM leases WHERE task_id=? ORDER BY fencing", (tid,))]


def _amended_and_refused(env):
    """Accepted T1, an amendment relaunch, and that worker's submit refused (a file outside T1's scope)."""
    accepted = _accepted(env)
    code, out = env.office("amend", "T1", "--", DELTA, env=EXTERNAL)
    assert code == 0, out
    assert task_row(env, "T1")["status"] == "running" and task_row(env, "T1")["current_dispatch_id"], out
    w, wt = _worker(env)
    (wt / "stray.py").write_text("X = 1\n")
    self_reviewed(wt, "stray.py")
    code, out = env.office("submit", cwd=wt, env=w)
    assert code == 4 and "outside-scope" in out, out
    t1 = task_row(env, "T1")
    assert t1["status"] == "blocked" and t1["current_revision_id"] == accepted["accepted_revision_id"], t1
    return accepted, w, wt


def test_a_revoke_after_a_refused_amendment_submit_keeps_the_task_accepted(env):
    accepted, w, wt = _amended_and_refused(env)
    # T1's live worker still holds the scope the dependent needs.
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 4 and "scope-held" in out and task_row(env, "T2")["status"] == "planned", out
    code, out = env.office("revoke", "T1", "--reason", "worker is stuck")
    assert code == 0, out
    assert "lease revoked" not in out and "relaunch" not in out.replace("no relaunch is needed", ""), out
    assert "stays accepted" in out and "no relaunch is needed" in out.splitlines()[-1], out
    assert "office rerun T1 --resume to apply A1" in out.splitlines()[-1], out
    t1 = task_row(env, "T1")
    assert t1["status"] == "accepted" and t1["accepted_revision_id"] == accepted["accepted_revision_id"], t1
    assert not t1["pause_reason"], t1
    current = _leases(env)[-1]
    assert current["released_at"] and not current["revoked_at"], current
    # The unapplied amendment is listed, not lost.
    assert "unapplied amendments: A1" in out, out
    # The dependent starts, and a late submit from the revoked worker is rejected.
    code, out = env.office("dispatch", "T2", env=EXTERNAL)
    assert code == 0 and "T2 ->" in out, out
    (wt / "calc.py").write_text(GOOD_ADD + "\n")
    self_reviewed(wt, "calc.py")
    code, out = env.office("submit", cwd=wt, env=w)
    assert code == 4 and "lease-lost" in out, out
    assert task_row(env, "T1")["status"] == "accepted"


def test_a_revoke_of_an_ordinary_amendment_relaunch_keeps_the_task_accepted(env):
    """An ordinary amendment bumps the task's contract_version past its accepted revision; that alone does not demote."""
    accepted = _accepted(env)
    env.office("amend", "T1", "--", DELTA, env=EXTERNAL, check=0)
    rev = env.con().execute("SELECT applied_version FROM revisions WHERE id=?", (accepted["accepted_revision_id"],)).fetchone()[0]
    assert task_row(env, "T1")["contract_version"] > rev and task_row(env, "T1")["status"] == "running"
    code, out = env.office("revoke", "T1", "--reason", "ack-only session is not needed")
    t1 = task_row(env, "T1")
    assert code == 0 and t1["status"] == "accepted" and not t1["pause_reason"], (t1, out)
    assert "unapplied amendments: A1" in out and "lease revoked" not in out, out
    last = _leases(env)[-1]
    assert last["released_at"] and not last["revoked_at"], last


def test_a_revoke_with_an_unapplied_contract_amendment_is_changes_required_naming_it(env):
    accepted = _accepted(env)
    env.write_plan(PLAN_DEPENDENT.replace("scope: calc.py\ndepends: none", "scope: calc.py, util.py\ndepends: none"))
    code, out = env.office("amend", "T1", "--contract", "--", "T1 also owns util.py", env=EXTERNAL)
    assert code == 0, out
    con = env.con()
    assert con.execute("SELECT class FROM amendments WHERE id LIKE '%:A1'").fetchone()[0] == "contract"
    code, out = env.office("revoke", "T1", "--reason", "contract moved")
    t1 = task_row(env, "T1")
    assert code == 0 and t1["status"] == "changes_required" and "A1" in (t1["pause_reason"] or ""), (t1, out)
    assert t1["accepted_revision_id"] == accepted["accepted_revision_id"]
    assert "stays accepted" not in out and "office rerun T1 --resume to apply A1" in out.splitlines()[-1], out
    last = _leases(env)[-1]
    assert last["revoked_at"] and not last["released_at"], last


def test_an_ordinary_and_a_contract_amendment_together_name_only_the_contract_one(env):
    _accepted(env)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    for i, (aid, cls) in enumerate((("A8", "ordinary"), ("A9", "contract"))):
        con.execute("INSERT INTO amendments(id, run_id, seq, class, scope_json, delta, requested_by, status, created_at) "
                    "VALUES(?,?,?,?,'[]','d','test','applied','2026-01-01')", (f"{run_id[:8]}:{aid}", run_id, 90 + i, cls))
        con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, content, "
                    "created_at) VALUES(?,?,?,'T1','Dx',?,'applied','c','2026-01-01')", (f"d{i}", run_id, aid, 5 + i))
    con.commit()
    code, out = env.office("revoke", "T1", "--reason", "x")
    t1 = task_row(env, "T1")
    assert code == 0 and t1["status"] == "changes_required" and t1["pause_reason"] == "amended by A9", (t1, out)


def test_a_revoke_never_turns_a_cancelled_task_into_paused(env):
    _accepted(env)
    env.office("amend", "T1", "--", DELTA, env=EXTERNAL, check=0)
    con = env.con()
    con.execute("UPDATE tasks SET status='cancelled' WHERE id='T1'")
    con.commit()
    code, out = env.office("revoke", "T1", "--reason", "cancelled work")
    assert code == 0, out
    t1 = task_row(env, "T1")
    assert t1["status"] == "cancelled" and not t1["pause_reason"], t1
    assert _leases(env)[-1]["revoked_at"], "the lease is still taken back"
    assert "relaunch" not in out, out


def test_a_revoke_of_a_plain_accepted_task_lists_unapplied_ordinary_amendments_and_keeps_it_accepted(env):
    accepted = _accepted(env)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    con.execute("INSERT INTO amendments(id, run_id, seq, class, scope_json, delta, requested_by, status, created_at) "
                "VALUES(?, ?, 90, 'ordinary', '[]', 'd', 'test', 'applied', '2026-01-01')", (f"{run_id[:8]}:A9", run_id))
    con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, content, "
                "created_at) VALUES('x', ?, 'A9', 'T1', ?, 5, 'queued', 'c', '2026-01-01')",
                (run_id, accepted["current_dispatch_id"]))
    con.commit()
    code, out = env.office("revoke", "T1", "--reason", "tidy")
    assert code == 0 and "unapplied amendments: A9" in out and "stays accepted" in out, out
    assert task_row(env, "T1")["status"] == "accepted"


def test_a_resubmitted_revision_is_not_the_accepted_one_so_revoke_still_pauses(env):
    _accepted(env)
    env.office("amend", "T1", "--", DELTA, env=EXTERNAL, check=0)
    con = env.con()
    # A newer revision than the accepted one: identity no longer holds.
    con.execute("UPDATE tasks SET current_revision_id='Rnew' WHERE id='T1'")
    con.commit()
    code, out = env.office("revoke", "T1", "--reason", "x")
    assert code == 0 and task_row(env, "T1")["status"] != "accepted", out
    assert "stays accepted" not in out, out


@pytest.mark.review_contract("v3.1")
def test_an_unchanged_tree_resubmit_after_revoke_and_rerun_is_ready(env):
    """T2 submitted while T1 is back in changes, so T2's revision stays unaccepted with nothing open against it."""
    from office import paths
    w2 = _t2_submitted_and_held(env)
    rev = task_row(env, "T2")["current_revision_id"]
    code, out = env.office("revoke", "T2", "--reason", "pane died")
    assert code == 0 and "lease revoked" in out and task_row(env, "T2")["status"] == "paused", out
    _end(env, w2["OFFICE_DISPATCH_ID"])
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0, out
    wenv, wt = _worker(env, "T2")
    pkt = json.loads((paths.run_dir(wenv["OFFICE_RUN_ID"]) / "dispatches" / wenv["OFFICE_DISPATCH_ID"] / "packet.json").read_text())
    assert pkt["fix_of"] == rev, pkt
    write_ledger(wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "PREFLIGHT ready" in out and f"resubmit: {rev} is not accepted" in out, out
    assert "no open findings or amendments" not in out, out
