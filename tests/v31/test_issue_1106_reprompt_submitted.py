"""#1106 H4: reviewer re-prompt counts a written reply or a working agent as submitted.

Evidence: in run 869e7feb, reviewer De66c10ad (pi harness) got a re-prompt at 19:39:49 local
(reply.invalid-1.txt moved aside). It wrote a new reply.txt at 19:40:09. Office still reported
"re-prompt is typed but unsubmitted" at 19:40:49, because submit_prompt returned "held"
(the prompt text was still in pi's scrollback, within the last 20 rows) and
gates._reprompt_until_valid returned before reading the file.

In gates._reprompt_until_valid, a "held" result is not final when a reply file now exists
at the output path (written after the re-prompt) or herdr reports the agent working or
busy (dispatch._herdr_agent_sample, the #583 signal).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_herdr_agent_launch import EMPTY, _fake, _live_dispatch


def _setup_reviewer_reprompt(env, monkeypatch, reads, *, harness="pi", gets=("idle",)):
    state_file = _fake(env, monkeypatch, gets=gets, reads=reads)
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("OFFICE_HERDR_LAND_TIMEOUT", "0")
    monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_WAIT", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_POLL", "0")
    from office import dispatch
    d = {**d, "launcher": "herdr", "pane_id": "w1:p7", "role": "code_reviewer", "harness": harness}
    run = {**run, "gates": {"review_reprompt_max": 1}}
    data = json.loads(state_file.read_text())
    data["pane_agents"] = {d["pane_id"]: dispatch.herdr_agent_name(d["id"])}
    data["pane_kinds"] = {d["pane_id"]: dispatch._herdr_kind(run, d)}
    state_file.write_text(json.dumps(data))
    return run, d


@pytest.mark.approved
def test_pi_reviewer_reprompt_with_reply_file_written_counts_as_submitted(env, monkeypatch):
    """A pi reviewer pane whose view keeps showing the re-prompt text, with a reply file
    written after the prompt, yields a parsed verdict and no unsubmitted reason (#1106)."""
    output = env.tmp / "reply.txt"
    held = "› Office could not read your review (empty reply). Write your complete review again"
    run, d = _setup_reviewer_reprompt(env, monkeypatch, [EMPTY, held], harness="pi")
    from office import dispatch, gates, review_parse

    # submit_prompt returns "held" because the prompt text is still in pi scrollback.
    # The reviewer wrote a new reply.txt after the re-prompt.
    orig_submit = dispatch.submit_prompt
    def fake_submit(name, prompt, **kw):
        res = orig_submit(name, prompt, **kw)
        assert res == "held", f"expected submit_prompt to return 'held', got {res!r}"
        output.write_text("VERDICT: APPROVED\nNEXT proceed\n")
        return res
    monkeypatch.setattr(dispatch, "submit_prompt", fake_submit)

    con = env.con()
    try:
        text, parsed, reason = gates._reprompt_until_valid(
            con, run, d, env.tmp, output, review_parse.parse(""),
            plan_review=False, visual=False,
        )
        events = con.execute("SELECT summary FROM events WHERE kind='review.reprompt'").fetchall()
    finally:
        con.close()

    assert parsed.valid is True
    assert parsed.verdict in ("APPROVED", "PASS")
    assert reason is None
    assert events and not any("unsubmitted" in e["summary"] for e in events)


@pytest.mark.approved
def test_reviewer_reprompt_working_agent_without_file_awaits_reply(env, monkeypatch):
    """An agent reported working with no file yet when submit_prompt returns 'held'
    awaits the file and yields a parsed verdict with no unsubmitted reason (#1106)."""
    output = env.tmp / "reply.txt"
    held = "› Office could not read your review (empty reply). Write your complete review again"
    run, d = _setup_reviewer_reprompt(env, monkeypatch, [EMPTY, held], harness="pi")
    from office import dispatch, gates, review_parse

    # herdr reports the agent is working/busy (#583 signal)
    monkeypatch.setattr(dispatch, "_herdr_agent_sample",
                        lambda name: {"status": "working", "busy": True, "content_hash": None})

    orig_submit = dispatch.submit_prompt
    def check_held(name, prompt, **kw):
        res = orig_submit(name, prompt, **kw)
        assert res == "held", f"expected submit_prompt to return 'held', got {res!r}"
        assert not output.is_file(), "no file should exist when submit_prompt returns"
        return res
    monkeypatch.setattr(dispatch, "submit_prompt", check_held)

    # When gates._await_file is reached, the file is written and read
    orig_await = gates._await_file
    def fake_await(out, wait_s, poll):
        assert not out.is_file(), "file must not exist prior to awaiting"
        out.write_text("VERDICT: APPROVED\nNEXT proceed\n")
        return orig_await(out, wait_s, poll)
    monkeypatch.setattr(gates, "_await_file", fake_await)

    con = env.con()
    try:
        text, parsed, reason = gates._reprompt_until_valid(
            con, run, d, env.tmp, output, review_parse.parse(""),
            plan_review=False, visual=False,
        )
        events = con.execute("SELECT summary FROM events WHERE kind='review.reprompt'").fetchall()
    finally:
        con.close()

    assert parsed.valid is True
    assert parsed.verdict in ("APPROVED", "PASS")
    assert reason is None
    assert events and not any("unsubmitted" in e["summary"] for e in events)


@pytest.mark.approved
def test_reviewer_reprompt_held_with_neither_signal_stays_unsubmitted(env, monkeypatch):
    """When submit_prompt returns held, no file exists, and agent is not working,
    Office stops with the unsubmitted reason and send-keys hint (#1106)."""
    output = env.tmp / "reply.txt"
    held = "› Office could not read your review (empty reply). Write your complete review again"
    run, d = _setup_reviewer_reprompt(env, monkeypatch, [EMPTY, held], harness="pi")
    from office import dispatch, gates, review_parse

    monkeypatch.setattr(dispatch, "_herdr_agent_sample",
                        lambda name: {"status": "idle", "busy": False, "content_hash": None})

    con = env.con()
    try:
        text, parsed, reason = gates._reprompt_until_valid(
            con, run, d, env.tmp, output, review_parse.parse(""),
            plan_review=False, visual=False,
        )
        events = con.execute("SELECT summary FROM events WHERE kind='review.reprompt'").fetchall()
    finally:
        con.close()

    assert not parsed.valid
    assert reason is not None
    assert "typed but unsubmitted" in reason and "send-keys" in reason
    assert events and any("typed but unsubmitted" in e["summary"] for e in events)
