"""#1087: a refused task-PR push is durable and points to manual recovery."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import Env, GOOD_ADD, PLAN_TWO, _activate, start_inline
from test_task_prs import github, remote_head


@pytest.mark.parametrize(("with_ready_task", "with_existing_pr"), [(False, False), (True, False), (False, True)])
def test_unrecorded_remote_tip_refusal_survives_through_status_and_land(
        tmp_path, monkeypatch, with_ready_task, with_existing_pr):
    env = _activate(Env(tmp_path, monkeypatch), monkeypatch)
    bare = github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    if with_ready_task:
        start_inline(env, plan=PLAN_TWO)
    else:
        start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    _, status = env.ojson("status")
    run_id = status["data"]["run_id"]
    branch = f"office/{run_id[:8]}/T1"

    foreign = env.tmp / "foreign"
    subprocess.run(["git", "clone", "-q", str(bare), str(foreign)], check=True)
    subprocess.run(["git", "-C", str(foreign), "config", "user.email", "foreign@example.test"], check=True)
    subprocess.run(["git", "-C", str(foreign), "config", "user.name", "Foreign"], check=True)
    (foreign / "README.md").write_text("unrecorded remote work\n")
    subprocess.run(["git", "-C", str(foreign), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(foreign), "commit", "-qm", "unrecorded remote work"], check=True)
    subprocess.run(["git", "-C", str(foreign), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], check=True)
    refused_tip = remote_head(bare, branch)

    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    assert remote_head(bare, branch) == refused_tip, "Office must not overwrite an unrecorded remote tip"

    from office import state
    con = env.con()
    try:
        task = state.get_task(con, run_id, "T1")
    finally:
        con.close()
    refusal = task["pr"]["push_refused"]
    assert refusal["revision"] == task["accepted_revision_id"]
    assert refusal["branch"] == branch
    assert "no earlier task revision proves ownership of remote head" in refusal["reason"]

    if with_existing_pr:
        pr = {"number": 1, "url": "https://github.com/o/r/pull/1", "base": "main", "branch": branch,
              "push_refused": refusal}
        from office import db
        con = env.con()
        try:
            with db.transaction(con):
                state.update_task(con, run_id, "T1", pr=pr)
        finally:
            con.close()
        gh_state_path = env.tmp / "gh.json"
        gh_state = json.loads(gh_state_path.read_text())
        gh_state["prs"].append({"number": 1, "url": pr["url"], "head": branch, "base": "main",
                                "title": "T1", "body": "", "draft": False, "state": "open", "comments": []})
        gh_state_path.write_text(json.dumps(gh_state))

    _, status = env.ojson("status")
    next_action = status["data"]["next"]
    assert ("inspect origin/" + branch).lower() in next_action.lower(), next_action
    assert "without force-pushing it" in next_action and "office close --handoff <pr-url>" in next_action
    assert "office land" not in next_action
    if with_ready_task:
        assert "office dispatch T2" in next_action, next_action
        return

    code, landed = env.office("land", "--merge", "--quote", "approved merge")
    assert code != 0, landed
    assert "next:" in landed and ("inspect origin/" + branch).lower() in landed.lower(), landed
    assert "office close --handoff <pr-url>" in landed, landed
    assert "push-refused" in landed, landed
    if with_existing_pr:
        gh_state = json.loads((env.tmp / "gh.json").read_text())
        assert not [call for call in gh_state["calls"] if call[:2] == ["pr", "merge"]], gh_state["calls"]
        assert gh_state["prs"][0]["state"] == "open", gh_state["prs"]
