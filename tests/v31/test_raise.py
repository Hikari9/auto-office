"""#472: `office raise` is an executor's non-terminal escalation, independent of `office submit`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, self_reviewed, task_row
from test_herdr_agent_launch import BUSY, EMPTY, _calls
from test_office_prompt import _herdr_worker

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
ASK = "Should the parser also accept tabs? Two ways: A) strict, B) lenient."


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"])


def _live(env):
    """An external (headless) T1 executor: live until it submits."""
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    return _worker(env)


def _events(env, kind):
    return [dict(r) for r in env.con().execute("SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))]


@pytest.mark.approved
def test_raise_blocks_the_task_records_the_event_and_wakes_wait_once(env):
    wenv, wt = _live(env)
    env.office("status", check=0)  # read earlier events
    code, out = env.office("raise", "--", ASK, cwd=wt, env=wenv)
    assert code == 0 and "question raised" in out and "orchestrator was woken" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("raised question: Should the parser"), row
    [ev] = _events(env, "task.raised")
    p = json.loads(ev["payload_json"])
    assert ev["task_id"] == "T1" and ev["dispatch_id"] == wenv["OFFICE_DISPATCH_ID"], ev
    assert p["kind"] == "question" and p["text"] == ASK and "dedup" in p and "block_id" in p
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 5, out
    line = next(ln for ln in out.splitlines() if ln.startswith("question:"))
    assert "T1" in line and "raised question" in line and "Should the parser also accept tabs" in line, line
    assert 'office answer T1 -- "<text>"' in line, line
    # reported once: the next wait is not an agent question again, and status keeps listing it
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code != 5, out
    code, out = env.office("status", env=EXTERNAL)
    assert "question: T1" in out and "raised question" in out, out


@pytest.mark.approved
def test_the_same_raise_again_records_nothing_new(env):
    wenv, wt = _live(env)
    env.office("raise", "--", ASK, cwd=wt, env=wenv, check=0)
    for again in (ASK, "  " + ASK.upper() + "\n"):
        code, out = env.office("raise", "--", again, cwd=wt, env=wenv)
        assert code == 0 and "already raised" in out and "still waiting" in out, out
    assert len(_events(env, "task.raised")) == 1
    # another kind, or another text, is a new raise
    env.office("raise", "--kind", "blocker", "--", ASK, cwd=wt, env=wenv, check=0)
    env.office("raise", "--", "Which tab width?", cwd=wt, env=wenv, check=0)
    assert len(_events(env, "task.raised")) == 3


@pytest.mark.approved
def test_a_headless_worker_gets_the_answer_queued_and_the_task_unblocks(env):
    wenv, wt = _live(env)
    env.office("raise", "--", ASK, cwd=wt, env=wenv, check=0)
    code, out = env.office("answer", "T1", "--", "B: lenient", env=EXTERNAL)
    assert code == 0 and "queued" in out and "next office command" in out, out
    row = task_row(env)
    assert row["status"] == "running" and row["pause_reason"] is None, row
    code, out = env.office("status", cwd=wt, env=wenv)  # the worker's next command carries the answer
    assert "ORCHESTRATOR MESSAGE" in out and "B: lenient" in out and "ANSWER to your raised question" in out, out
    code, out = env.office("status", env=EXTERNAL)
    assert "question: T1" not in out, out
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code != 5, out
    # the worker may resubmit; a repeat of the answered raise only reports the answer
    code, out = env.office("raise", "--", ASK, cwd=wt, env=wenv)
    assert "already raised" in out and "answered: B: lenient" in out, out
    assert len(_events(env, "task.raised")) == 1


@pytest.mark.approved
def test_a_pane_worker_is_prompted_with_the_answer(env, monkeypatch):
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY, BUSY])
    wenv, wt = _worker(env)
    env.office("raise", "--kind", "blocker", "--", "the staging DB is down", cwd=wt, env=wenv, check=0)
    code, out = env.office("answer", "T1", "--", "use the local sqlite file", env=EXTERNAL)
    assert code == 0 and "raise answered" in out and "landed" in out, out
    prompt = next(c for c in _calls(state_file) if c[:2] == ["agent", "prompt"])
    assert "use the local sqlite file" in prompt[3] and "raised blocker" in prompt[3], prompt
    assert task_row(env)["status"] == "running"
    [ev] = _events(env, "task.raise_answered")
    assert json.loads(ev["payload_json"])["delivery"] == "prompt"


@pytest.mark.approved
def test_a_raise_survives_resume(env):
    wenv, wt = _live(env)
    env.office("raise", "--", ASK, cwd=wt, env=wenv, check=0)
    code, out = env.office("resume", env=EXTERNAL)
    assert code == 0, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("raised question"), row
    code, out = env.office("status", env=EXTERNAL)
    assert "question: T1" in out and "Should the parser" in out, out
    code, out = env.office("answer", "T1", "--", "A", env=EXTERNAL)
    assert code == 0 and task_row(env)["status"] == "running", out


@pytest.mark.approved
def test_a_revoked_or_superseded_dispatch_cannot_raise_and_its_raise_closes(env):
    wenv, wt = _live(env)
    env.office("raise", "--", ASK, cwd=wt, env=wenv, check=0)
    env.office("revoke", "T1", check=0)
    code, out = env.office("raise", "--", "a different question", cwd=wt, env=wenv)
    assert code == 4 and "lease" in out, out
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code != 5 and "question: T1" not in out, out  # closed, not reported as a question
    [closed] = _events(env, "task.raise_closed")
    assert json.loads(closed["payload_json"])["reason"] == "revoked"
    assert len(_events(env, "task.raised")) == 1
    # a newer dispatch owns the task: the old session's raise is refused as superseded
    con = env.con()
    con.execute("UPDATE tasks SET current_dispatch_id='Dnewer' WHERE id='T1'")
    con.commit()
    code, out = env.office("raise", "--", "yet another", cwd=wt, env=wenv)
    assert code == 4 and "newer dispatch" in out, out


@pytest.mark.approved
def test_a_scope_request_after_a_refused_submit_needs_an_amendment_not_an_answer(env):
    wenv, wt = _live(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt, "calc.py")
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out, out
    code, out = env.office("raise", "--kind", "scope-request", "--", "docs need the flag", cwd=wt, env=wenv)
    assert code == 2 and "--path" in out, out  # a scope request names its paths
    code, out = env.office("raise", "--kind", "scope-request", "--path", "README.md", "--", "docs need the flag",
                           cwd=wt, env=wenv)
    assert code == 0, out
    assert task_row(env)["pause_reason"].startswith("scope requested: README.md"), task_row(env)
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 5 and "office amend T1 --contract" in out and "paths: README.md" in out, out
    [ev] = _events(env, "task.raised")
    assert "README.md" in json.loads(ev["payload_json"])["diff"]
    code, out = env.office("answer", "T1", "--", "no: revert README.md", env=EXTERNAL)
    assert code == 0 and "did not change its contract" in out, out
    # the answer changed nothing: the file is still outside the scope and the submit is still refused
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out, out


@pytest.mark.approved
def test_raise_needs_an_executor_text_and_a_known_kind(env):
    wenv, wt = _live(env)
    assert env.office("raise", cwd=wt, env=wenv)[0] == 2
    assert env.office("raise", "--kind", "nonsense", "--", "x", cwd=wt, env=wenv)[0] == 2
    code, out = env.office("raise", "--", "why", env=EXTERNAL)
    assert code != 0 and "executor" in out, out
    code, out = env.office("raise", "--help")
    assert code == 0 and "scope-request" in out and "office submit" in out, out


RAISE_ACTION = {"office": [["raise", "--kind", "question", "--", ASK]]}


def _dispatches(env):
    return [dict(r) for r in env.con().execute("SELECT * FROM dispatches WHERE task_id='T1' AND role='executor' ORDER BY started_at")]


@pytest.mark.approved
def test_a_worker_that_raised_and_exited_is_not_relaunched_or_reported_as_ended(env):
    # fake_agent's `office` action: a scripted executor runs `office raise` itself, then its process ends.
    approved_run(env, executor=[RAISE_ACTION] * 4)
    env.office("dispatch", "T1", check=0)
    assert len(_dispatches(env)) == 1  # no relaunch of the same brief
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("raised question"), row
    assert not _events(env, "task.relaunch") and not _events(env, "task.blocked")  # never "ended without submitting"
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2")
    assert code == 5 and "raised question" in out and 'office answer T1 -- "<text>"' in out, out
    assert "without submitting" not in out, out


@pytest.mark.approved
def test_answering_an_ended_worker_records_it_and_the_rerun_brief_carries_it(env):
    approved_run(env, executor=[RAISE_ACTION, {"exit": 3}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("answer", "T1", "--", "B: lenient", check=0)
    assert "nothing was delivered" in out and "office rerun T1" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and "office rerun T1" in row["pause_reason"], row
    assert env.office("status")[1].count("question: T1") == 0  # answered: no longer waiting
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    from office import paths
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "ANSWER to your raised question" in brief and "B: lenient" in brief, brief
    assert "does not change your contract" in brief
    # the raising dispatch is not a failed attempt: its crashed successors still get the full retry budget
    # (environment_retry_max = 2 relaunches after the first failure), so D1 + 3 failures
    assert len(_dispatches(env)) == 4, [d["id"] for d in _dispatches(env)]


@pytest.mark.approved
def test_the_idle_executor_stall_does_not_fire_for_a_raised_task(env, monkeypatch):
    from office import guide, rerun
    _, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY])
    wenv, wt = _worker(env)
    monkeypatch.setenv("OFFICE_EXECUTOR_IDLE_STALL_S", "0")
    monkeypatch.setattr(rerun, "agent_activity", lambda d: {"alive": True, "busy": False, "hash": "h", "text": "> ", "status": "idle"})
    guide.stalls(con, run)  # first look records the idle start
    assert any("idle" in s for s in guide.stalls(con, run))
    env.office("raise", "--", ASK, cwd=wt, env=wenv, check=0)
    assert guide.stalls(con, run) == []


@pytest.mark.approved
def test_a_rejected_submit_then_raise_in_one_session(env):
    # README.md is tracked and outside scope: the submit is refused, and the same session raises instead of retrying.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD, "README.md": "changed\n"}, "submit": True,
                                 "office_after": [["raise", "--kind", "blocker", "--", "README.md must change too"]]}] * 3)
    env.office("dispatch", "T1", check=0)
    assert _events(env, "submit.refused"), "the out-of-scope submit was refused"
    [raised] = _events(env, "task.raised")
    assert json.loads(raised["payload_json"])["kind"] == "blocker"
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("raised blocker"), row
    assert len(_dispatches(env)) == 1 and not _events(env, "task.relaunch")


@pytest.mark.approved
def test_a_headless_worker_that_ended_on_a_question_can_be_answered(env):
    # the ordinary ended-on-question case (no raise): `office answer` works without a pane
    from test_agent_questions import ENDED_ON_QUESTION
    approved_run(env, executor=[{"raw": ENDED_ON_QUESTION}] * 3)
    env.office("dispatch", "T1", check=0)
    code, out = env.office("status")
    assert "question: T1" in out and 'office answer T1 -- "<answer>"' in out, out
    code, out = env.office("answer", "T1", "--", "yes, add it")
    assert code == 0 and "answer recorded" in out and "office rerun T1" in out, out
    assert "question: T1" not in env.office("status")[1]
    env.office("rerun", "T1", "--fresh", check=0)
    from office import paths
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "yes, add it" in brief and "ANSWER to your raised question" in brief
