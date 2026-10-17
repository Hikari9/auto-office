"""An upstream task accepted again reopens the accepted tasks built on its old revision (#294).

A dependent is accepted only on a revision that contains its dependency's accepted revision. When the
dependency is accepted on a newer one, every accepted dependent that lacks it is reopened to
changes_required (its accepted evidence stays recorded) and `office rerun <T> --resume` restacks it.
Integration refuses a composition whose accepted revisions do not contain their dependencies, and
convergence never reviews a lane holding such a task. Both review contracts.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run, task_row, write_clean_ledger

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PASS = {"reply": "VERDICT: PASS"}
PLAN_STACK = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1")
PLAN_CHAIN = PLAN_STACK + """
### T3: Implement sub
scope: sub.py
depends: T2
checks: python3 -c "import sub; assert sub.sub(3, 2) == 1"
accept:
- sub.sub(3, 2) == 1
visual: none
"""
GOOD_SUB = "def sub(a, b):\n    return a - b\n"
CONTRACTS = [pytest.param("v3.1", marks=pytest.mark.review_contract("v3.1"), id="v3.1"),
             pytest.param("convergence", id="convergence")]


def _run_id(env) -> str:
    return env.con().execute("SELECT id FROM runs").fetchone()[0]


def _base(env) -> str:
    return env.con().execute("SELECT base_sha FROM runs").fetchone()[0]


def _commit_on(env, base: str, files: dict, msg: str = "work") -> str:
    """A commit of `files` on top of `base`, made in a scratch worktree so no checkout moves."""
    scratch = env.tmp / f"scratch-{uuid.uuid4().hex[:6]}"
    env.git("worktree", "add", "-q", "--detach", str(scratch), base)
    try:
        for name, text in files.items():
            (scratch / name).write_text(text)
        env.git("add", "-A", cwd=scratch)
        env.git("commit", "-qm", msg, cwd=scratch)
        return env.git("rev-parse", "HEAD", cwd=scratch).strip()
    finally:
        env.git("worktree", "remove", "--force", str(scratch))


def _accepted_commit(env, tid: str) -> str:
    return env.con().execute("SELECT r.commit_sha FROM revisions r JOIN tasks t ON t.accepted_revision_id=r.id "
                             "WHERE t.id=?", (tid,)).fetchone()[0]


def _is_ancestor(env, ancestor: str, descendant: str, cwd=None) -> bool:
    return subprocess.run(["git", "-C", str(cwd or env.repo), "merge-base", "--is-ancestor", ancestor, descendant],
                          capture_output=True).returncode == 0


def _insert_revision(con, run: dict, task: dict, commit: str, tree: str, *, accepted: bool) -> str:
    rev_id = f"Rfab-{task['id']}-{commit[:7]}"
    seq = con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] + 1
    con.execute("UPDATE revisions SET status='superseded' WHERE run_id=? AND task_id=? AND status='current'",
                (run["id"], task["id"]))
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, commit_sha, tree_sha, base_commit, dispatch_id, "
                "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rev_id, run["id"], task["id"], seq, commit, tree, run["base_sha"], task.get("current_dispatch_id"),
                 run["requirements_version"], run["plan_version"], task["contract_version"], "fab", uuid.uuid4().hex,
                 "current", "2026-01-01T00:00:00+00:00"))
    con.execute("UPDATE tasks SET status=?, current_revision_id=?, accepted_revision_id=? WHERE run_id=? AND id=?",
                ("accepted" if accepted else "submitted", rev_id, rev_id if accepted else task.get("accepted_revision_id"),
                 run["id"], task["id"]))
    return rev_id


def accept_fab(env, tid: str, commit: str) -> str:
    """`tid` accepted on a revision at `commit`, with no acceptance evaluation (the state a run was left in)."""
    from office import state
    con = env.con()
    run = state.get_run(con, _run_id(env))
    rev_id = _insert_revision(con, run, state.get_task(con, run["id"], tid), commit,
                              env.git("rev-parse", f"{commit}^{{tree}}").strip(), accepted=True)
    con.commit()
    return rev_id


def reaccept(env, tid: str, commit: str) -> str:
    """`tid` submits a revision at `commit`, its gates pass and acceptance is evaluated: the production path."""
    from office import contract, db, gates, state
    con = env.con()
    run = state.get_run(con, _run_id(env))
    convergence = contract.is_convergence(run)
    with db.transaction(con):
        rev_id = _insert_revision(con, run, state.get_task(con, run["id"], tid), commit,
                                  env.git("rev-parse", f"{commit}^{{tree}}").strip(), accepted=False)
        for kind in ("checks",) if convergence else ("checks", "code_review"):
            con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, input_key, "
                        "status, verdict, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ("G" + uuid.uuid4().hex[:8], run["id"], "task", tid, rev_id, run["plan_version"], kind,
                         f"fab:{rev_id}", "done", "APPROVED" if convergence and kind == "checks" else "PASS",
                         "2026-01-01T00:00:00+00:00"))
        assert gates.evaluate_acceptance(con, run, tid), "the fabricated revision is accepted"
    return rev_id


def _worker(env, tid):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"])


def _restack_events(env, tid: str) -> list[str]:
    return [r[0] for r in env.con().execute("SELECT summary FROM events WHERE kind='task.restack_needed' AND task_id=? "
                                            "ORDER BY seq", (tid,))]


def _chain(env, **script):
    """T1 <- T2 <- T3, each accepted on a commit built on the one before (no worker ran)."""
    approved_run(env, plan=PLAN_CHAIN, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS], **script)
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c2 = _commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2")
    c3 = _commit_on(env, c2, {"sub.py": GOOD_SUB}, "t3")
    for tid, c in (("T1", c1), ("T2", c2), ("T3", c3)):
        accept_fab(env, tid, c)
    return c1, c2, c3


@pytest.mark.parametrize("contract", CONTRACTS)
def test_a_reaccepted_upstream_reopens_the_accepted_dependent_and_the_rerun_restacks_it(env, contract):
    approved_run(env, plan=PLAN_STACK, integration_reviewer=[PASS], code_reviewer=[PASS],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}],
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}, "submit": True}])
    env.office("dispatch", "T1", check=0)
    env.office("dispatch", "T2", check=0)
    assert [task_row(env, t)["status"] for t in ("T1", "T2")] == ["accepted", "accepted"]
    t2_rev, t2_commit = task_row(env, "T2")["accepted_revision_id"], _accepted_commit(env, "T2")
    gates_before = env.con().execute("SELECT COUNT(*) FROM gates WHERE revision_id=?", (t2_rev,)).fetchone()[0]
    assert gates_before

    c1b = _commit_on(env, _accepted_commit(env, "T1"), {"calc.py": GOOD_ADD + "# reviewed\n"}, "t1 again")
    t1_new = reaccept(env, "T1", c1b)

    t2 = task_row(env, "T2")
    assert t2["status"] == "changes_required", t2
    assert t2["pause_reason"] == f"restack: T1 re-accepted on {t1_new}", t2
    assert t2["accepted_revision_id"] == t2_rev, "the accepted evidence stays recorded"
    assert env.con().execute("SELECT COUNT(*) FROM gates WHERE revision_id=?", (t2_rev,)).fetchone()[0] == gates_before
    assert task_row(env, "T1")["accepted_revision_id"] == t1_new
    assert len(_restack_events(env, "T2")) == 1 and f"T1 re-accepted on {t1_new}" in _restack_events(env, "T2")[0]
    code, out = env.office("status")
    assert "office rerun T2 --resume" in out.splitlines()[-1] and f"restack: T1 re-accepted on {t1_new}" in out.splitlines()[-1], out

    code, out = env.office("rerun", "T2", "--fresh", env=EXTERNAL)
    assert code == 0 and f"T2 restacked onto T1 {t1_new}" in out, out
    w2, wt2 = _worker(env, "T2")
    assert _is_ancestor(env, c1b, "HEAD", cwd=wt2), "T1's new revision is merged into the T2 worktree"
    assert _is_ancestor(env, t2_commit, "HEAD", cwd=wt2), "T2's own work stays"
    write_clean_ledger(wt2)
    code, out = env.office("submit", cwd=wt2, env=w2)
    assert code == 0, out
    t2 = task_row(env, "T2")
    assert t2["status"] == "accepted" and t2["accepted_revision_id"] != t2_rev, t2
    assert _is_ancestor(env, c1b, _accepted_commit(env, "T2"))
    from office import integration, state
    con = env.con()
    assert integration.status(con, state.get_run(con, _run_id(env)))["status"] == "accepted"


@pytest.mark.parametrize("contract", CONTRACTS)
def test_every_transitive_dependent_that_lacks_the_new_revision_is_reopened(env, contract):
    c1, c2, c3 = _chain(env)
    c1b = _commit_on(env, c1, {"calc.py": GOOD_ADD + "# reviewed\n"}, "t1 again")
    t1_new = reaccept(env, "T1", c1b)
    for tid in ("T2", "T3"):
        row = task_row(env, tid)
        assert row["status"] == "changes_required" and row["pause_reason"] == f"restack: T1 re-accepted on {t1_new}", row
        assert row["accepted_revision_id"], "its accepted revision stays recorded"
        assert len(_restack_events(env, tid)) == 1, tid
    assert task_row(env, "T1")["status"] == "accepted"


@pytest.mark.parametrize("contract", CONTRACTS)
def test_a_dependent_whose_accepted_revision_already_contains_the_new_one_stays_accepted(env, contract):
    approved_run(env, plan=PLAN_CHAIN, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c1b = _commit_on(env, c1, {"calc.py": GOOD_ADD + "# reviewed\n"}, "t1 again")
    c2 = _commit_on(env, c1b, {"mul.py": GOOD_MUL}, "t2 built on t1's second revision")
    c3 = _commit_on(env, _base(env), {"sub.py": GOOD_SUB}, "t3 off the base")
    accept_fab(env, "T1", c1)
    accept_fab(env, "T2", c2)
    accept_fab(env, "T3", c3)
    reaccept(env, "T1", c1b)
    assert task_row(env, "T2")["status"] == "accepted", "T2 already contains c1b"
    assert task_row(env, "T3")["status"] == "changes_required", "T3 does not"
    assert _restack_events(env, "T2") == []


@pytest.mark.parametrize("contract", CONTRACTS)
def test_a_first_acceptance_reopens_nothing(env, contract):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    reaccept(env, "T1", c1)
    assert task_row(env, "T1")["status"] == "accepted" and task_row(env, "T2")["status"] == "planned"
    assert _restack_events(env, "T2") == []


def _compose(env) -> dict:
    """Queue integration on the current accepted set and run it."""
    from office import db, integration, jobs, state
    con = env.con()
    run = state.get_run(con, _run_id(env))
    with db.transaction(con):
        integration.maybe_queue(con, run)
    jobs.kick(con, run["id"])
    return state.get_run(con, run["id"])["landing"]["integration"]


@pytest.mark.parametrize("contract", CONTRACTS)
def test_integration_refuses_a_stale_composition_naming_the_task_and_dependency(env, contract):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c2 = _commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2")
    t1_old = accept_fab(env, "T1", c1)
    t2_rev = accept_fab(env, "T2", c2)
    # T1 is accepted again on a revision that edits the same file T2's base holds: a merge would conflict.
    c1b = _commit_on(env, _base(env), {"calc.py": GOOD_ADD + "# other\n"}, "t1 elsewhere")
    t1_new = accept_fab(env, "T1", c1b)
    assert t1_new != t1_old
    status = _compose(env)
    assert status["status"] == "stale", status
    assert f"T2 accepted on {t2_rev} does not contain T1 accepted on {t1_new}" in status["detail"], status
    assert "conflict" not in status["detail"]
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='integration.stale'").fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM events WHERE kind='integration.conflict'").fetchone()
    code, out = env.office("status")
    assert "INTEGRATION stale: T2 accepted on" in out, out


@pytest.mark.parametrize("contract", CONTRACTS)
def test_a_run_that_is_not_stale_composes_as_before(env, contract):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c2 = _commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2")
    accept_fab(env, "T1", c1)
    accept_fab(env, "T2", c2)
    status = _compose(env)
    assert status["status"] == "accepted", status
    assert _is_ancestor(env, c2, status["commit"]) and _is_ancestor(env, c1, status["commit"])


def test_convergence_queues_no_lane_review_while_a_task_is_stale(env):
    from office import convergence, db, state
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c2 = _commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2")
    accept_fab(env, "T1", c1)
    accept_fab(env, "T2", c2)
    c1b = _commit_on(env, _base(env), {"calc.py": GOOD_ADD + "# other\n"}, "t1 elsewhere")
    accept_fab(env, "T1", c1b)  # T2 is accepted but built on c1: stale
    con = env.con()
    run = state.get_run(con, _run_id(env))
    with db.transaction(con):
        convergence.on_task_accepted(con, run, "T1")
    assert not con.execute("SELECT 1 FROM outbox WHERE kind='converge'").fetchone()
    # The restacked T2 is no longer stale: the lane is reviewed.
    c2b = _commit_on(env, c1b, {"mul.py": GOOD_MUL}, "t2 restacked")
    accept_fab(env, "T2", c2b)
    run = state.get_run(con, _run_id(env))
    with db.transaction(con):
        convergence.on_task_accepted(con, run, "T2")
    assert con.execute("SELECT 1 FROM outbox WHERE kind='converge'").fetchone()


@pytest.mark.parametrize("contract", CONTRACTS)
def test_status_surfaces_a_stale_dependency_of_an_accepted_task(env, contract):
    approved_run(env, plan=PLAN_STACK, executor=[{}], code_reviewer=[PASS], integration_reviewer=[PASS])
    c1 = _commit_on(env, _base(env), {"calc.py": GOOD_ADD}, "t1")
    c2 = _commit_on(env, c1, {"mul.py": GOOD_MUL}, "t2")
    accept_fab(env, "T1", c1)
    t2_rev = accept_fab(env, "T2", c2)
    c1b = _commit_on(env, _base(env), {"calc.py": GOOD_ADD + "# other\n"}, "t1 elsewhere")
    t1_new = accept_fab(env, "T1", c1b)
    code, data = env.ojson("status")
    assert "T2 built on T1" in data["next"] and f"T1 was accepted on {t1_new}" in data["next"], data["next"]
    assert task_row(env, "T2")["status"] == "accepted" and task_row(env, "T2")["accepted_revision_id"] == t2_rev, \
        "reading status never reopens it"
