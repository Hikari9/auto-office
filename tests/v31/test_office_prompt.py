"""office prompt: message a live pane agent with `agent prompt` and confirm it landed.

`herdr pane run` into a Claude pane leaves the text typed but unsubmitted:
Claude takes the Enter written in the same chunk as part of the paste
(reproduced 2026-10-01, herdr 0.9.3, Claude Code 2.1.286).
"""
from __future__ import annotations

import pytest

from test_herdr_agent_launch import BUSY, EMPTY, _calls, _fake, _live_dispatch

TEXT = "AMENDMENT A2: run office status, apply it, then office ack A2."


def _herdr_worker(env, monkeypatch, *, reads, status="running", agent="working"):
    """A herdr-hosted T1 dispatch; `agent` is what `herdr agent get` reports ("gone": no agent)."""
    state_file = _fake(env, monkeypatch, gets=[agent], reads=reads)
    for k, v in {"OFFICE_HERDR_LAND_TIMEOUT": "0", "OFFICE_HERDR_ENTER_WAIT": "0"}.items():
        monkeypatch.setenv(k, v)
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p7', status=? WHERE id=?", (status, d["id"]))
    con.commit()
    return state_file, run, d, con


def _sent(calls):
    prompts = [c for c in calls if c[:2] == ["agent", "prompt"]]
    raw = [c for c in calls if c[:2] in (["pane", "run"], ["pane", "send-text"])]
    return prompts, raw


@pytest.mark.approved
def test_prompt_goes_through_agent_prompt_and_reports_it_landed(env, monkeypatch):
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY, BUSY])
    from office import prompting
    res = prompting.prompt(con, run, "T1", TEXT)
    assert "landed" in res.lines[0]
    prompts, raw = _sent(_calls(state_file))
    assert prompts == [["agent", "prompt", "w1:p7", TEXT]] and not raw
    assert con.execute("SELECT count(*) FROM events WHERE kind='prompt' AND dispatch_id=?", (d["id"],)).fetchone()[0] == 1


@pytest.mark.approved
def test_prompt_left_in_the_composer_gets_enter_and_is_never_sent_twice(env, monkeypatch):
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[f"> {TEXT}"])
    from office import prompting
    with pytest.raises(prompting.Refused) as err:
        prompting.prompt(con, run, d["id"], TEXT)
    assert err.value.category == "prompt-held" and "send-keys w1:p7 Enter" in err.value.next_step
    calls = _calls(state_file)
    prompts, raw = _sent(calls)
    assert len(prompts) == 1 and not raw
    assert any(c[:2] == ["pane", "send-keys"] and c[-1] == "Enter" for c in calls)


@pytest.mark.approved
def test_prompt_refuses_a_dispatch_whose_agent_is_gone(env, monkeypatch):
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY], status="exited", agent="gone")
    from office import prompting
    with pytest.raises(prompting.Refused) as err:
        prompting.prompt(con, run, "T1", TEXT)
    assert "no live agent" in err.value.message and "office rerun T1" in err.value.next_step
    assert not _sent(_calls(state_file))[0]


@pytest.mark.approved
def test_prompt_cli_needs_a_message(env, monkeypatch):
    _herdr_worker(env, monkeypatch, reads=[EMPTY])
    code, out = env.office("prompt", "T1")
    assert code == 2 and "office prompt <task|dispatch>" in out, out


@pytest.mark.approved
def test_prompt_reaches_a_reviewer_recorded_exited_whose_agent_still_waits(env, monkeypatch):
    # A reviewer that settles without a reply file is recorded exited; its
    # agent stays up for the re-prompt that Office's attention reason names.
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY, BUSY], status="exited", agent="idle")
    con.execute("UPDATE dispatches SET role='code_reviewer', ended_at=started_at WHERE id=?", (d["id"],))
    con.commit()
    from office import prompting
    res = prompting.prompt(con, run, d["id"], "Write your complete review to the reply file.")
    assert "landed" in res.lines[0]
    assert len(_sent(_calls(state_file))[0]) == 1


@pytest.mark.approved
def test_auto_continue_prompts_the_agent_by_its_herdr_name_not_the_pane(env, monkeypatch):
    """Real submit_prompt through the fake herdr: `agent prompt <office-agent-name> continue`."""
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[EMPTY, BUSY])
    from office import dispatch, guide
    con.execute("UPDATE dispatches SET resets_at='2020-01-01T00:00:00+00:00', limit_label='9:30pm' WHERE id=?", (d["id"],))
    con.commit()
    run["policy"] = {"executor_usage_limit": {"auto_continue": True, "continue_grace_seconds": 0}}
    row = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (d["id"],)).fetchone())
    limit = {"resets_at": None, "label": "9:30pm", "tz": None, "local": None}
    act = {"alive": True, "busy": False, "hash": "h", "text": "Usage limit reached · limit resets 9:30pm"}
    assert guide._usage_limit_stall(con, run, row, act, limit, "T1 executor") is None
    prompts, raw = _sent(_calls(state_file))
    assert prompts == [["agent", "prompt", dispatch.herdr_agent_name(d["id"]), "continue"]] and not raw
    assert con.execute("SELECT limit_continue_outcome FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0] == "landed"
