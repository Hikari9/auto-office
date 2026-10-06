"""Submit enforces the executor's self-review round-cap stop."""
from __future__ import annotations

import json

import pytest

from test_preflight import _dispatched
from test_self_review_ledger import ledger_text, write_ledger


@pytest.mark.integration
@pytest.mark.approved
def test_submit_refuses_open_round_three_finding_and_signals_the_orchestrator(env):
    wenv, wt, d = _dispatched(env)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    write_ledger(wt, ledger_text(head, rnd=3, findings=[
        "FINDING medium edge-cases calc.py:9 | add skips zero | open"
    ]))

    code, out = env.office("submit", cwd=wt, env=wenv)

    assert code == 4 and "self-review ledger refused submission" in out, out
    assert "round 3 ended with a finding still open" in out, out
    assert not env.con().execute("SELECT 1 FROM revisions WHERE task_id='T1'").fetchone()
    assert (wt / "OFFICE_SELF_REVIEW.md").exists(), "a refused submission must preserve its ledger"
    event = env.con().execute(
        "SELECT summary, payload_json FROM events WHERE kind='worker.signal' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert event and "preflight stop" in event["summary"], event
    next_step = json.loads(event["payload_json"])["next"]
    assert "fixed <test> mutation=failed" in next_step, next_step
    assert "office prompt" in next_step and "office preflight" in next_step and "office submit" in next_step, next_step


@pytest.mark.integration
@pytest.mark.approved
def test_submit_accepts_and_consumes_a_clean_ledger(env):
    wenv, wt, _ = _dispatched(env)

    code, out = env.office("submit", cwd=wt, env=wenv)

    assert code == 0 and "captured" in out, out
    assert env.con().execute("SELECT 1 FROM revisions WHERE task_id='T1'").fetchone()
    assert not (wt / "OFFICE_SELF_REVIEW.md").exists(), "submit consumes the clean ledger"


@pytest.mark.integration
@pytest.mark.approved
def test_submit_accepts_changed_work_without_a_ledger(env):
    wenv, wt, _ = _dispatched(env)
    (wt / "OFFICE_SELF_REVIEW.md").unlink()

    code, out = env.office("submit", cwd=wt, env=wenv)

    assert code == 0 and "captured" in out, out
    assert env.con().execute("SELECT 1 FROM revisions WHERE task_id='T1'").fetchone()


@pytest.mark.integration
@pytest.mark.approved
def test_submit_captures_uncommitted_in_scope_work_with_a_clean_ledger(env):
    wenv, wt, _ = _dispatched(env)
    with (wt / "calc.py").open("a") as f:
        f.write("# pending submit edit\n")

    code, out = env.office("submit", cwd=wt, env=wenv)

    assert code == 0 and "captured" in out, out
    revision = env.con().execute("SELECT commit_sha FROM revisions WHERE task_id='T1'").fetchone()
    assert revision
    assert "pending submit edit" in env.git("show", f"{revision['commit_sha']}:calc.py", cwd=wt)
