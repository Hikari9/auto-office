"""Task PR stacking and acceptance: a task stacked at dispatch targets the branch it was cut from,
an accepted sync that beats the revision sync still opens the PR and readies it, and a failing
`gh pr ready` is recorded rather than dropped."""
from __future__ import annotations

import json
import sys

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, start_inline
from office import db, jobs, prs, state
from test_task_prs import FAKE_GH, gh, github

APPROVED = {"reply": "VERDICT: APPROVED\nNEXT proceed"}


def _run_one_task(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[APPROVED])
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    assert env.ojson("status")[1]["data"]["tasks"] == {"T1": "accepted"}


def test_task_stacked_at_dispatch_without_depends_targets_the_branch_it_was_cut_from(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                         {"write": {"mul.py": GOOD_MUL}, "submit": True}],
               convergence_reviewer=[APPROVED, APPROVED])
    start_inline(env, plan=PLAN_TWO)  # T2 has `depends: none`
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", "T2")
    assert code == 0 and "T2 stacked after T1" in out, out
    assert env.ojson("status")[1]["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}
    prs = {p["title"].split(":")[0]: p for p in gh(env)["prs"]}
    assert prs["T1"]["base"] == "main"
    assert prs["T2"]["base"] == prs["T1"]["head"], prs
    assert "stacked on T1 (#1)" in prs["T2"]["body"], prs["T2"]["body"]


def test_accepted_sync_before_the_revision_sync_opens_the_pr_and_marks_it_ready(env, monkeypatch):
    _run_one_task(env, monkeypatch)
    # Rewind to what the accepted job sees when it runs first: no PR on GitHub, none recorded.
    (env.tmp / "gh.json").write_text(json.dumps({**gh(env), "prs": []}))
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs").fetchone()["id"]
        with db.transaction(con):
            con.execute("UPDATE tasks SET pr_json=NULL WHERE id='T1'")
            con.execute("DELETE FROM outbox WHERE kind='pr_sync'")
            rev = con.execute("SELECT accepted_revision_id FROM tasks WHERE id='T1'").fetchone()[0]
            prs.queue(con, state.get_run(con, run_id), "T1", "accepted", rev)
        assert jobs.run_pending(con, run_id) == 1
    finally:
        con.close()
    (pr,) = gh(env)["prs"]
    assert not pr["draft"], pr
    assert any("T1 accepted on" in c and "ready for review" in c for c in pr["comments"]), pr["comments"]


def test_failing_gh_pr_ready_is_recorded_not_ignored(env, monkeypatch):
    github(env, monkeypatch)
    (env.bin / "gh").write_text(
        f"#!{sys.executable}\nimport runpy, sys\n"
        "if sys.argv[1:3] == ['pr', 'ready']:\n    print('GraphQL: boom', file=sys.stderr)\n    raise SystemExit(1)\n"
        f"runpy.run_path({str(FAKE_GH)!r}, run_name='__main__')\n")
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[APPROVED])
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    assert env.ojson("status")[1]["data"]["tasks"]["T1"] == "accepted"
    (pr,) = gh(env)["prs"]
    assert pr["draft"] and not any("ready for review" in c for c in pr["comments"]), pr
    code, out = env.office("inspect", "events")
    assert "T1 PR accepted: gh pr ready failed: GraphQL: boom" in out, out
