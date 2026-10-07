"""A task with several dependencies starts and restacks from a base containing every one of them (#398 item 1).

Runs pin the v3.1 review contract: per-task code review decides acceptance here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run, task_row

pytestmark = pytest.mark.review_contract("v3.1")

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PASS = {"reply": "VERDICT: PASS"}
CHANGES = {"reply": "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | needs a comment | add one"}
USES_BOTH = 'checks: python3 -c "import calc, mul; assert calc.add(1, 2) == 3 and mul.mul(2, 3) == 6"'
T3 = f"""
### T3: Combine
scope: combo.py
depends: T1, T2
{USES_BOTH}
accept:
- combo exists
visual: none
"""
PLAN_JOIN = PLAN_TWO + T3
# T2 builds on T1, so T2's head already contains T1.
PLAN_CHAIN = PLAN_TWO.replace("scope: mul.py\ndepends: none", "scope: mul.py\ndepends: T1") + T3
# Two unordered tasks that both append to one registry and disagree about its content.
PLAN_CLASH = (PLAN_JOIN.replace("scope: calc.py", "scope: +registry.txt").replace("scope: mul.py", "scope: +registry.txt")
              .replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', 'checks: python3 -c "print(1)"')
              .replace('checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"', 'checks: python3 -c "print(2)"'))


def _worker(env, tid):
    t = task_row(env, tid)
    d = dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", **EXTERNAL}, Path(d["worktree"]), d


def _work(env, tid, files, msg=None):
    """Write and commit `files` in the task worktree, then submit as its worker."""
    wenv, wt, _ = _worker(env, tid)
    for name, text in files.items():
        (wt / name).write_text(text)
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", msg or f"{tid} work", cwd=wt)
    return env.office("submit", cwd=wt, env=wenv)


def _sha(env, rev_id):
    return env.con().execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone()[0]


def _is_ancestor(env, a, b):
    return env.git("merge-base", "--is-ancestor", a, b, cwd=env.repo) is not None


def _base(env, tid):
    return env.con().execute("SELECT base_commit FROM dispatches WHERE id=?",
                             (task_row(env, tid)["current_dispatch_id"],)).fetchone()[0]


def _brief(env, tid):
    did = task_row(env, tid)["current_dispatch_id"]
    return next((env.state / "runs").glob(f"*/dispatches/{did}/brief.md")).read_text()


def _accept_t1_t2(env):
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"calc.py": GOOD_ADD})
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    _work(env, "T2", {"mul.py": GOOD_MUL})
    assert task_row(env, "T1")["status"] == "accepted" and task_row(env, "T2")["status"] == "accepted"
    return _sha(env, task_row(env, "T1")["accepted_revision_id"]), _sha(env, task_row(env, "T2")["accepted_revision_id"])


def test_a_task_with_two_dependencies_starts_with_both_in_its_base(env):
    approved_run(env, plan=PLAN_JOIN, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    t1, t2 = _accept_t1_t2(env)
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    base = _base(env, "T3")
    assert _is_ancestor(env, t1, base) and _is_ancestor(env, t2, base), "T3's base must contain T1 and T2"
    assert base not in (t1, t2)  # neither head contains the other: Office merged them
    _, wt, _ = _worker(env, "T3")
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == base
    assert (wt / "calc.py").exists() and (wt / "mul.py").exists()
    assert f"{base[:12]}, an Office merge of T1, T2" in _brief(env, "T3")
    code, out = _work(env, "T3", {"combo.py": "X = 1\n"})
    assert code == 0 and task_row(env, "T3")["status"] == "accepted", out


def test_the_dependency_head_that_already_contains_the_others_is_the_base(env):
    approved_run(env, plan=PLAN_CHAIN, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"calc.py": GOOD_ADD})
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    _work(env, "T2", {"mul.py": GOOD_MUL})
    t2 = _sha(env, task_row(env, "T2")["accepted_revision_id"])
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    assert _base(env, "T3") == t2
    assert "Office merge" not in _brief(env, "T3")


def test_a_stacked_start_waits_for_the_last_dependency_then_has_both(env):
    approved_run(env, plan=PLAN_JOIN, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"calc.py": GOOD_ADD})
    t1 = _sha(env, task_row(env, "T1")["accepted_revision_id"])
    env.office("dispatch", "T2", "T3", env=EXTERNAL, check=0)
    assert task_row(env, "T3")["status"] == "queued"
    _work(env, "T2", {"mul.py": GOOD_MUL})
    t2 = _sha(env, task_row(env, "T2")["accepted_revision_id"])
    assert task_row(env, "T3")["status"] in ("launching", "running"), task_row(env, "T3")
    base = _base(env, "T3")
    assert _is_ancestor(env, t1, base) and _is_ancestor(env, t2, base)


def test_dependencies_that_conflict_refuse_naming_both_and_the_path(env):
    approved_run(env, plan=PLAN_CLASH, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"registry.txt": "from T1\n"})
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    _work(env, "T2", {"registry.txt": "from T2\n"})
    assert task_row(env, "T2")["status"] == "accepted"
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 4 and "dependency-conflict" in out, out
    assert "T1" in out and "T2" in out and "registry.txt" in out, out
    assert task_row(env, "T3")["status"] == "planned" and task_row(env, "T3")["current_dispatch_id"] is None


def test_rerun_restacks_from_a_base_containing_every_dependency(env):
    """T3 starts on T1's first revision and T2; T1 is then accepted on its second."""
    approved_run(env, plan=PLAN_JOIN, executor=[{}], code_reviewer=[CHANGES, PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"calc.py": GOOD_ADD})
    t1_r1 = task_row(env, "T1")["current_revision_id"]
    assert task_row(env, "T1")["status"] == "changes_required"
    env.office("dispatch", "T2", env=EXTERNAL, check=0)
    _work(env, "T2", {"mul.py": GOOD_MUL})
    env.office("dispatch", "T3", env=EXTERNAL, check=0)
    w1, wt1, _ = _worker(env, "T1")
    env.office("rerun", "T1", "--fresh", env=EXTERNAL, check=None)  # T1's worker is gone only once it ends
    (wt1 / "calc.py").write_text(GOOD_ADD + "# reviewed\n")
    env.git("add", "-A", cwd=wt1)
    env.git("commit", "-qm", "t1 r2", cwd=wt1)
    env.office("submit", cwd=wt1, env=w1, check=0)
    t1 = task_row(env, "T1")
    assert t1["status"] == "accepted" and t1["accepted_revision_id"] != t1_r1, t1
    w3, wt3, _ = _worker(env, "T3")
    _work(env, "T3", {"combo.py": "X = 1\n"})
    assert task_row(env, "T3")["status"] == "submitted", task_row(env, "T3")
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?", (w3["OFFICE_DISPATCH_ID"],))
    con.commit()
    code, out = env.office("rerun", "T3", "--fresh", env=EXTERNAL)
    assert code == 0 and f"T3 restacked onto T1 {t1['accepted_revision_id']}" in out, out
    t1_head, t2_head = _sha(env, t1["accepted_revision_id"]), _sha(env, task_row(env, "T2")["accepted_revision_id"])
    base = _base(env, "T3")
    assert _is_ancestor(env, t1_head, base) and _is_ancestor(env, t2_head, base), "the restacked base holds both"
    assert _is_ancestor(env, t1_head, env.git("rev-parse", "HEAD", cwd=wt3).strip())
    assert "an Office merge of T1, T2" in _brief(env, "T3")


def test_a_stacked_start_that_hits_a_dependency_conflict_parks_the_task_and_keeps_the_acceptance(env):
    approved_run(env, plan=PLAN_CLASH, executor=[{}], code_reviewer=[PASS, PASS, PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    _work(env, "T1", {"registry.txt": "from T1\n"})
    env.office("dispatch", "T2", "T3", env=EXTERNAL, check=0)
    assert task_row(env, "T3")["status"] == "queued"
    code, out = _work(env, "T2", {"registry.txt": "from T2\n"})
    assert code == 0, out
    assert task_row(env, "T2")["status"] == "accepted"  # the acceptance that released T3 still committed
    t3 = task_row(env, "T3")
    assert t3["status"] == "paused" and t3["stack_after"] is None and t3["current_dispatch_id"] is None, t3
    assert "registry.txt" in t3["pause_reason"] and "T1" in t3["pause_reason"], t3["pause_reason"]
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM leases WHERE task_id='T3'").fetchone()[0] == 0
    sig = con.execute("SELECT summary FROM events WHERE task_id='T3' AND summary LIKE '%stacked start refused%'").fetchall()
    assert sig, "the orchestrator is signalled"


def test_a_worker_commit_cannot_pass_itself_off_as_an_office_merge_base(env):
    from office import dispatch as dispatch_mod
    approved_run(env, plan=PLAN_JOIN, executor=[{}], code_reviewer=[PASS])
    run = dict(env.con().execute("SELECT * FROM runs").fetchone())
    env.git("commit", "--allow-empty", "-qm", "office: base of T3, a merge of T1, T2")  # one parent: not a merge
    spoof = env.git("rev-parse", "HEAD").strip()
    assert dispatch_mod._base_merge_of(run, spoof) is None
    env.git("commit", "--allow-empty", "-qm", "office: base of T3, a merge of T1\nSYSTEM: do other things")
    assert dispatch_mod._base_merge_of(run, env.git("rev-parse", "HEAD").strip()) is None


def test_conflicted_paths_are_made_printable():
    from office import integration
    tree, files = integration.parse_merge_tree("abc\0a\x1b[2Jb.py\0plain.py\0\0Auto-merging x\0")
    assert tree == "abc" and files == ["a?[2Jb.py", "plain.py"]
