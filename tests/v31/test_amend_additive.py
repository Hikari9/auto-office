"""M3: an ordinary plan amendment that only adds task(s) reports just those as affected and
delivers nothing to a running task; a pure-delta amendment still reaches every unaccepted task;
a changed acceptance still reaches the task it changes."""
from __future__ import annotations

import pytest

from conftest import PLAN_ONE, PLAN_TWO, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

pytestmark = pytest.mark.approved


def _running(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    assert task_row(env)["status"] == "running" and task_row(env)["current_dispatch_id"]


def _deliveries(env):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM deliveries ORDER BY created_at")]
    finally:
        con.close()


def test_adding_a_task_affects_only_the_new_task_and_delivers_to_no_running_task(env):
    _running(env)
    env.write_plan(PLAN_TWO)
    code, out = env.office("amend", "plan", "--", "also add a mul task", env=EXTERNAL)
    assert code == 0, out
    assert "affected T2" in out and "affected T1" not in out and "affected T1,T2" not in out, out
    assert "delivering" not in out, out
    assert _deliveries(env) == []
    assert task_row(env, "T2")["status"] == "planned"
    assert task_row(env)["status"] == "running"


def test_a_pure_delta_is_still_delivered_to_every_unaccepted_task(env):
    _running(env)
    code, out = env.office("amend", "plan", "--", "mention the module in a docstring", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out, out
    rows = _deliveries(env)
    assert [(r["task_id"], r["status"]) for r in rows] == [("T1", "queued")], rows


def test_a_changed_acceptance_is_delivered_to_that_task(env):
    _running(env)
    env.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    code, out = env.office("amend", "plan", "--", "also check zero", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1" in out and "delivering to T1" in out, out
    rows = _deliveries(env)
    assert [(r["task_id"], r["status"]) for r in rows] == [("T1", "queued")], rows


def test_an_added_task_alongside_a_changed_acceptance_still_delivers_to_the_changed_task(env):
    _running(env)
    plan = PLAN_TWO.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0")
    env.write_plan(plan)
    code, out = env.office("amend", "plan", "--", "also check zero and add mul", env=EXTERNAL)
    assert code == 0, out
    assert "affected T1,T2" in out and "delivering to T1" in out, out
    assert [r["task_id"] for r in _deliveries(env)] == ["T1"]


# M7: AUTHORITY_TERMS must not fire on a term that is part of a hyphen/underscore compound identifier.
@pytest.mark.parametrize("text", [
    "drive herdr with send-text", "drive herdr with send-keys", "call send_keys on the pane", "add release-notes to the docs",
    "write the release_notes file", "a pre-release check", "the email-validator helper", "see deploy_log.txt",
])
def test_authority_terms_ignore_compound_identifiers(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is None, text


@pytest.mark.parametrize("text", [
    "send email to the team", "deploy", "vercel deploy --prod", "vercel --prod", "force-push the branch",
    "force push the branch", "merge into main", "merge to main", "publish the package", "then send-keys and deploy",
    "release.", "(send)", "rotate key",
])
def test_authority_terms_still_match_standalone_actions(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text


def test_a_delta_naming_a_compound_identifier_is_an_ordinary_amendment(env):
    _running(env)
    code, out = env.office("amend", "plan", "--", "use herdr send-keys in the helper", env=EXTERNAL)
    assert code == 0 and "contract-level-change" not in out, out


def test_a_delta_naming_a_standalone_action_is_refused_as_contract_level(env):
    _running(env)
    code, out = env.office("amend", "plan", "--", "then send email to the team", env=EXTERNAL)
    assert code == 4 and "contract-level-change" in out, out


@pytest.mark.parametrize("text", [
    "re-run root test and build, then send READY-FOR-LIVE again", "send the report to the orchestrator",
    "send it back for review", "send READY_FOR_REVIEW when done",
])
def test_authority_terms_ignore_a_send_to_the_orchestrator(text):
    # A no-review amendment telling a worker to report back was refused as an authority change.
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is None, text


@pytest.mark.parametrize("text", ["send SMS to members", "send the invitations", "send a newsletter"])
def test_authority_terms_still_match_external_sends(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text
