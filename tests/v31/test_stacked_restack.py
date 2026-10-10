"""A dependent built on a dependency revision that review later superseded (#236, #245).

Runs pin the v3.1 review contract (restacks triggered by per-task review supersession); every run started before #337 keeps it.
"""
from __future__ import annotations

import pytest

from pathlib import Path

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run, task_row

pytestmark = pytest.mark.review_contract("v3.1")


EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
CHANGES = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | needs a comment | add one"
PLAN_STACK = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1")


def _worker(env, tid):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _commit(env, wt, files, msg):
    for name, text in files.items():
        (wt / name).write_text(text)
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", msg, cwd=wt)


def _rev_commit(env, rev_id):
    return env.con().execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone()[0]


def _t2_on_superseded_t1(env):
    """T2 starts on T1's first revision; T1 is then accepted on its second."""
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[{"reply": CHANGES}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    w1, wt1 = _worker(env, "T1")
    _commit(env, wt1, {"calc.py": GOOD_ADD}, "t1 r1")
    env.office("submit", cwd=wt1, env=w1, check=0)
    t1_r1 = task_row(env, "T1")["current_revision_id"]
    assert task_row(env, "T1")["status"] == "changes_required"
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    _commit(env, wt1, {"calc.py": GOOD_ADD + "# reviewed\n"}, "t1 r2")
    env.office("submit", cwd=wt1, env=w1, check=0)
    t1 = task_row(env, "T1")
    assert t1["status"] == "accepted" and t1["accepted_revision_id"] != t1_r1, t1
    w2, wt2 = _worker(env, "T2")
    assert env.con().execute("SELECT base_commit FROM dispatches WHERE id=?",
                             (w2["OFFICE_DISPATCH_ID"],)).fetchone()[0] == _rev_commit(env, t1_r1)
    return t1_r1, t1["accepted_revision_id"], w2, wt2


def test_stale_dependent_names_its_blocker_and_rerun_restacks(env):
    t1_r1, t1_acc, w2, wt2 = _t2_on_superseded_t1(env)
    _commit(env, wt2, {"mul.py": GOOD_MUL}, "t2")
    env.office("submit", cwd=wt2, env=w2, check=0)
    t2 = task_row(env, "T2")
    assert t2["status"] == "submitted" and t2["accepted_revision_id"] is None, t2
    code, out = env.office("status")
    assert f"T2 waiting: built on T1 {t1_r1}, but T1 was accepted on {t1_acc}" in out, out
    assert "office rerun T2" in out.splitlines()[-1], out
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='task.restack_needed' AND task_id='T2'").fetchone()[0] == 1

    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?",
                (w2["OFFICE_DISPATCH_ID"],))
    con.commit()
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and f"T2 restacked onto T1 {t1_acc}" in out, out
    acc_commit = _rev_commit(env, t1_acc)
    env.git("merge-base", "--is-ancestor", acc_commit, "HEAD", cwd=wt2)  # raises if not merged
    w2b, _ = _worker(env, "T2")
    assert env.con().execute("SELECT base_commit FROM dispatches WHERE id=?",
                             (w2b["OFFICE_DISPATCH_ID"],)).fetchone()[0] == acc_commit
    brief = (env.state / "runs").glob(f"*/dispatches/{w2b['OFFICE_DISPATCH_ID']}/brief.md")
    assert any(f"RESTACKED Office merged T1 {t1_acc}" in b.read_text() for b in brief)
    code, out = env.office("submit", cwd=wt2, env=w2b)
    assert code == 0, out
    assert task_row(env, "T2")["status"] == "accepted"


def test_hand_merged_dependency_is_not_outside_scope(env):
    t1_r1, t1_acc, w2, wt2 = _t2_on_superseded_t1(env)
    _commit(env, wt2, {"mul.py": GOOD_MUL}, "t2")
    env.git("merge", "--no-edit", "-q", _rev_commit(env, t1_acc), cwd=wt2)
    code, out = env.office("submit", cwd=wt2, env=w2)
    assert code == 0 and "outside" not in out, out
    t2 = task_row(env, "T2")
    assert t2["status"] == "accepted", t2
    rev = env.con().execute("SELECT base_commit, changed_json FROM revisions WHERE id=?", (t2["current_revision_id"],)).fetchone()
    assert rev["base_commit"] == _rev_commit(env, t1_acc)  # reviewers diff against the merged T1


def test_submit_from_task_worktree_without_dispatch_env(env):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _, wt1 = _worker(env, "T1")
    _commit(env, wt1, {"calc.py": GOOD_ADD}, "t1")
    code, out = env.office("submit", cwd=wt1)  # a restarted session lost OFFICE_DISPATCH_ID
    assert code == 0 and "captured" in out, out
    assert task_row(env, "T1")["status"] == "accepted"


def test_plan_review_status_names_its_reviewer(env):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    con = env.con()
    run = dict(con.execute("SELECT id, plan_review_json FROM runs").fetchone())
    con.execute("UPDATE runs SET plan_review_json=? WHERE id=?", ('{"required": true}', run["id"]))
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, round, escalated, created_at) "
                "VALUES('Gq', ?, 'plan', 1, 'plan_review', 'plan:1', 'running', 1, 0, '2026-01-01')", (run["id"],))
    con.commit()
    code, out = env.office("status")
    assert "plan review queued (p1 Gq; no reviewer running yet)" in out, out
    con.execute("INSERT INTO dispatches(id, run_id, role, started_at, status, gate_id, pane_id) "
                "VALUES('Drev', ?, 'plan_reviewer', '2026-01-01', 'running', 'Gq', 'w1:p2')", (run["id"],))
    con.commit()
    code, out = env.office("status")
    assert "plan review running (p1 reviewer Drev pane w1:p2)" in out, out


def test_a_conflicting_rerun_carries_every_unmerged_dependency_to_the_worker(env):
    # R3-1: rerun dropped `unmerged`, so preflight only ever required the conflicting one.
    import json
    t1_r1, t1_acc, w2, wt2 = _t2_on_superseded_t1(env)
    _commit(env, wt2, {"calc.py": GOOD_ADD + "# t2 edit\n"}, "t2 conflicting")
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?",
                (w2["OFFICE_DISPATCH_ID"],))
    con.commit()
    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and "conflicts" in out, out
    w2b, _ = _worker(env, "T2")
    pkt = next((env.state / "runs").glob(f"*/dispatches/{w2b['OFFICE_DISPATCH_ID']}/packet.json"))
    restack = json.loads(pkt.read_text())["restack"]
    assert [u["commit"] for u in restack.get("unmerged") or []] == [_rev_commit(env, t1_acc)], restack
