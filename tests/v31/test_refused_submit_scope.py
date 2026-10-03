"""#259: a refused submit is a durable blocker even while the worker lives; an executor can
request scope; an amendment tells the live worker to resubmit; hints stay consistent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
WIDER = PLAN_ONE.replace("scope: calc.py", "scope: calc.py, README.md")


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"])


def _live_refused(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    return wenv, wt


@pytest.mark.approved
def test_refused_submit_from_a_live_worker_blocks_and_wakes_wait(env):
    wenv, wt = _live_refused(env)
    env.office("status", check=0)  # consume earlier events
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out and "--request-scope" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("submit refused"), row
    con = env.con()
    d = con.execute("SELECT status, override_json FROM dispatches WHERE id=?", (wenv["OFFICE_DISPATCH_ID"],)).fetchone()
    assert d["status"] in ("running", "launching") and "README.md" in json.loads(d["override_json"])["submit_refused"]["files"]
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "blocker: T1 submit refused" in out and "README.md" in out, out


@pytest.mark.approved
def test_request_scope_wakes_the_orchestrator_with_files_reason_and_diff(env):
    wenv, wt = _live_refused(env)
    env.office("status", check=0)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "docs need the new flag", cwd=wt, env=wenv)
    assert code == 0 and "scope request recorded" in out and "office amend T1 --contract" in out, out
    assert task_row(env)["status"] == "blocked"
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "requests scope README.md: docs need the new flag" in out, out
    assert "office amend T1 --contract" in out, out
    row = env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()
    payload = json.loads(row[0])
    assert payload["files"] == ["README.md"] and payload["reason"] == "docs need the new flag" and "README.md" in payload["diff"]


@pytest.mark.approved
def test_request_scope_needs_a_reason_and_an_executor(env):
    wenv, wt = _live_refused(env)
    code, out = env.office("submit", "--request-scope", "README.md", cwd=wt, env=wenv)
    assert code == 2 and "why the scope must grow" in out, out
    code, out = env.office("submit", "--request-scope", "README.md", "--", "why", env=EXTERNAL)
    assert code != 0 and "executor" in out, out


@pytest.mark.approved
def test_amendment_tells_the_live_blocked_worker_to_resubmit(env):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "docs need the new flag", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    code, out = env.office("amend", "T1", "--contract", "--", "add README.md", check=0)
    assert task_row(env)["status"] == "running" and task_row(env)["pause_reason"] is None
    texts = [json.loads(r[0]).get("text", "") for r in env.con().execute(
        "SELECT payload_json FROM outbox WHERE kind='notify_worker'")]
    assert any("office submit again" in t and "office ack" in t for t in texts), texts
    code, out = env.office("ack", "A1", cwd=wt, env=wenv)
    assert code == 0, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out


@pytest.mark.approved
def test_blocked_worker_can_revert_and_resubmit(env):
    wenv, wt = _live_refused(env)
    env.office("submit", cwd=wt, env=wenv)
    assert task_row(env)["status"] == "blocked"
    env.git("checkout", "--", "README.md", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    assert task_row(env)["status"] != "blocked" and task_row(env)["pause_reason"] is None


@pytest.mark.approved
def test_rerun_on_a_live_worker_does_not_point_at_itself(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    code, out = env.office("rerun", "T1", "--resume")
    assert code == 4 and "worker-live" in out or "live worker" in out, out
    assert "office prompt T1" in out and "office revoke T1" in out, out


def test_plan_review_brief_asks_for_registries_in_scope():
    from office import briefs
    assert "Scope registries" in briefs.PLAN_REVIEW_FORMAT and "gate" in briefs.PLAN_REVIEW_FORMAT
