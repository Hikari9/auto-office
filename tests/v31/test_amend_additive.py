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
    "send it back to the orchestrator for review", "send READY_FOR_REVIEW when done",
    "send READY_FOR_REVIEW to the orchestrator",
])
def test_authority_terms_ignore_a_send_to_the_orchestrator(text):
    # A no-review amendment telling a worker to report back was refused as an authority change.
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is None, text


@pytest.mark.parametrize("text", ["send SMS to members", "send the invitations", "send a newsletter",
                                  # review #446 F1: an exempt object sent to anyone but the orchestrator counts
                                  "send it to all members", "send the summary to every parent",
                                  "send NEWSLETTER_2026 to members", "send it back",
                                  # R3-2: the orchestrator must be the whole recipient, and a protocol
                                  # word must name no recipient or channel
                                  "send the report to the office team",
                                  "send the summary to Office staff and every parent",
                                  "send it to the orchestrator and to all members", "send it to office@example.org",
                                  "send NEWSLETTER_2026 out tonight", "send PROMO-CODE via sms"])
def test_authority_terms_still_match_external_sends(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text


@pytest.mark.parametrize("text", [
    "send the status to the orchestrator.", "send it to the orchestrator and stop", "send READY-FOR-LIVE and stop",
    "send the report to the orchestrator, then run tests and merge", "send the summary to the orchestrator: done",
])
def test_a_report_to_the_orchestrator_followed_by_a_next_step_is_exempt(text):
    # R4-1: sentence ends and new actions after the orchestrator are not other recipients.
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is None, text


@pytest.mark.parametrize("text", ["send the report to the office for parents", "send it to the office, members too"])
def test_an_audience_after_the_office_still_counts(text):
    # R4-2.
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text


@pytest.mark.parametrize("text", [
    # PR #492 review item 1: an action word must not let a later recipient through
    "send it to the office and then to all members", "send it to the office and post it to members",
    # item 2: default-deny after the recipient (unlisted audiences, purpose then audience, clause after comma)
    "send the report to the office and donors", "send the report to the office and congregants",
    "send the report to the office for review by parents",
    "send the report to the office for approval, then to all members",
    "send READY-FOR-LIVE and donors", "send it to office.example.org",
])
def test_anything_after_the_orchestrator_but_an_end_or_next_action_is_external(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text


def test_a_done_note_after_the_orchestrator_is_exempt():
    # Item 6: "(done)" ends the report; "and to nobody else" stays refused (stricter, by design).
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search("send the status to the orchestrator (done)") is None
    assert AUTHORITY_TERMS.search("send the status to the orchestrator and to nobody else") is not None


_TO_ORCH = "send the report to the orchestrator"


@pytest.mark.parametrize("text", [
    # a071c74 re-verify item 1: an audience after punctuation
    "send the report to the office: parents and staff", "send it to the office (done). parents too",
    "send it to the office: parents too", "send it to the office; and to parents", "send it to the office. Members too",
    "send READY-FOR-LIVE: all members",
    # item 2: a recipient after a next-action verb
    _TO_ORCH + " and update members", _TO_ORCH + " and then push it to all members",
    _TO_ORCH + " and continue to all members", _TO_ORCH + " and check with parents",
    _TO_ORCH + " and test it on members", _TO_ORCH + " then run it past parents",
])
def test_every_clause_after_the_orchestrator_is_checked(text):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(text) is not None, text


@pytest.mark.parametrize("tail", [
    # item 4: ordinary report-back lines
    " and request review", " and keep going", " and start T2", " and move on to T2", " and open the PR",
    " and report back", " and await instructions", " and mark T1 done", " and do nothing else",
    " after tests pass", " once tests pass", " if tests fail", ", stop",
])
def test_ordinary_next_steps_after_a_report_are_exempt(tail):
    from office.amend import AUTHORITY_TERMS
    assert AUTHORITY_TERMS.search(_TO_ORCH + tail) is None, tail
