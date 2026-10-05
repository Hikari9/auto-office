"""A pane agent stopped on a question is surfaced by `office wait` (exit 5) and answered by `office answer`."""
from __future__ import annotations

from pathlib import Path

import pytest

from office import guide, questions
from test_herdr_agent_launch import _calls
from test_office_prompt import _herdr_worker

WIDGET = (Path(__file__).parent / "fixtures" / "claude_ask_widget.txt").read_text()
PLAIN = "⏺ I finished the parser.\n  Should I also widen the mock scope to the whole directory?\n\n> \n"
BUSY_PLAIN = "⏺ Reading files\n  Is this right?\n✻ Working (3s · esc to interrupt)\n"


def test_parse_claude_widget():
    q = questions.parse(WIDGET, status="blocked", busy=True)
    assert q["kind"] == "select" and q["question"].endswith("keep those reads?")
    assert [o["n"] for o in q["options"]] == [1, 2, 3, 4, 5]
    assert q["options"][0]["label"] == "Sign View defaults (Recommended)"


def test_parse_plain_text_question_only_when_turn_ended():
    assert questions.parse(PLAIN, status="idle", busy=False)["kind"] == "text"
    assert questions.parse(BUSY_PLAIN, status="working", busy=True) is None
    assert questions.parse("⏺ Done.\n\n> \n", status="idle", busy=False) is None


def test_parse_blocked_without_list_is_a_dialog():
    assert questions.parse("some approval ui", status="blocked", busy=True)["kind"] == "dialog"


def test_parse_old_widget_scrolled_up_is_not_a_question():
    assert questions.parse(WIDGET + "\n⏺ moving on\n> \n", status="idle", busy=False) is None


@pytest.mark.approved
def test_wait_exits_5_with_a_question_line_then_status_keeps_it(env, monkeypatch):
    _, run, d, con = _herdr_worker(env, monkeypatch, reads=[WIDGET], agent="blocked")
    res = guide.wait(con, run, timeout=5, poll=0.1)
    assert res.exit_code == questions.EXIT
    line = next(ln for ln in res.lines if ln.startswith("question:"))
    assert d["id"] in line and "w1:p7" in line and "1) Sign View defaults" in line
    assert f"office answer {d['id']} <1-5>" in line and "user decision" in line
    # a repeat scan of the same question is not news; status still lists it
    assert questions.scan(con, run)[0] == []
    assert any(ln.startswith("question:") for ln in guide.status(con, run).lines)


@pytest.mark.approved
def test_answer_number_presses_the_option_and_records_it(env, monkeypatch):
    state_file, run, d, con = _herdr_worker(env, monkeypatch, reads=[WIDGET, WIDGET, "⏺ ok\n"], agent="blocked")
    monkeypatch.setenv("OFFICE_ANSWER_TIMEOUT", "5")
    res = questions.answer(con, run, "T1", "1")
    assert "pressed 1" in res.lines[0]
    assert ["pane", "send-keys", "w1:p7", "1"] in _calls(state_file)
    assert con.execute("SELECT count(*) FROM events WHERE kind=?", (questions.ANSWERED,)).fetchone()[0] == 1


@pytest.mark.approved
def test_answer_refuses_when_no_question_shows(env, monkeypatch):
    _, run, d, con = _herdr_worker(env, monkeypatch, reads=["> composer empty"], agent="idle")
    with pytest.raises(questions.Refused) as err:
        questions.answer(con, run, "T1", "1")
    assert err.value.category == "no-question"


@pytest.mark.approved
def test_answer_rejects_an_option_that_does_not_exist(env, monkeypatch):
    _, run, d, con = _herdr_worker(env, monkeypatch, reads=[WIDGET], agent="blocked")
    with pytest.raises(questions.Refused) as err:
        questions.answer(con, run, "T1", "9")
    assert err.value.category == "no-option"
