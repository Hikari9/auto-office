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


ENDED_ON_QUESTION = (
    "`pnpm typecheck` fails, and the fix needs one line outside my SCOPE.\n\n"
    "**Question for the orchestrator (Q1):** may I add `personAliasGuid?: string;` to src/http/oauth.ts?\n\n"
    "TASK=T1 COMMIT=31d0a33 PUSHED=yes CHECKS=fail (typecheck) SUBMIT=not attempted "
    "NEXT=Answer Q1: approve the one-line addition, or say how the seam should move.\n")


def test_final_question_reads_the_closing_next_step():
    q = questions.final_question(ENDED_ON_QUESTION)
    assert q["kind"] == "text" and q["ended"] and q["question"].startswith("Question for the orchestrator (Q1)")
    assert questions.final_question("Done.\nTASK=T1 CHECKS=pass SUBMIT=submitted NEXT=none\n") is None
    assert questions.final_question("Checks are green; I stopped before submit.\n") is None
    assert questions.final_question("Should T1 also own tests/auth?\n")["question"] == "Should T1 also own tests/auth?"


@pytest.mark.approved
def test_a_headless_worker_that_ended_on_a_question_is_a_question_not_a_relaunch(env):
    # Run 330605a8: three headless sessions in a row asked the same scope question and were
    # each reported as "ended (success) without submitting; relaunching".
    from conftest import approved_run, task_row
    approved_run(env, executor=[{"raw": ENDED_ON_QUESTION}] * 3)
    env.office("dispatch", "T1", check=0)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0] == 1
    t = task_row(env)
    assert t["status"] == "blocked" and t["pause_reason"].startswith(questions.ENDED_PREFIX), t
    code, out = env.office("wait", "--timeout", "1")
    assert code == questions.EXIT, out
    assert "question: T1" in out and "Q1" in out and "office amend T1" in out and "office rerun T1" in out, out
    code, out = env.office("status")
    assert "question: T1" in out, out


def test_final_question_reports_the_latest_question_and_ignores_bare_ids():
    # Review F5: an earlier, answered question in the log is not the one asked now.
    log = ("Question (Q1): may I touch a.ts?\nAnswered: yes.\n...work...\n"
           "Question (Q2): may I also touch b.ts?\nTASK=T1 SUBMIT=not attempted NEXT=Answer Q2\n")
    assert questions.final_question(log)["question"].startswith("Question (Q2)")
    # Review F8: a NEXT that only mentions a question id does not ask anything.
    assert questions.final_question("Stopped.\nTASK=T1 SUBMIT=not attempted NEXT=rerun checks; Q1 was resolved\n") is None
    # Review F10: one fingerprint rule for live and ended questions.
    q = questions.final_question(log)
    assert q["fingerprint"] == questions.fingerprint(q)


def test_only_a_cleanly_ended_executor_can_end_on_a_question():
    # Review F3 (planners have no amend/rerun answer path) and F6 (a crash is not a question).
    from office import dispatch
    assert dispatch._may_end_on_question({"role": "executor", "terminal_classification": "success"})
    assert not dispatch._may_end_on_question({"role": "planner", "terminal_classification": "success"})
    assert not dispatch._may_end_on_question({"role": "executor", "terminal_classification": "nonzero"})


@pytest.mark.approved
def test_a_crashed_worker_whose_last_line_is_a_question_is_still_relaunched(env):
    from conftest import approved_run, task_row
    approved_run(env, executor=[{"raw": "Does the fixture need a DB?", "exit": 3}] * 4)
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert not (t["pause_reason"] or "").startswith(questions.ENDED_PREFIX), t


def test_a_next_naming_a_question_with_an_answer_or_approval_still_asks():
    # Review #446 F7.
    assert questions.final_question("Stopped.\nTASK=T1 SUBMIT=not attempted NEXT=await orchestrator answer to Q1\n")
    assert questions.final_question("Stopped.\nTASK=T1 SUBMIT=not attempted NEXT=need approval on Q1\n")
    assert questions.final_question("Stopped.\nTASK=T1 SUBMIT=not attempted NEXT=rerun checks; Q1 was resolved\n") is None


@pytest.mark.approved
def test_a_tail_cut_mid_line_never_supplies_the_question(env):
    # Review #446 F9: the first line of a partial tail is a fragment.
    from conftest import approved_run
    from office import dispatch, paths, state
    approved_run(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    ddir = paths.run_dir(run["id"]) / "dispatches" / "Dcut"
    ddir.mkdir(parents=True, exist_ok=True)
    tail = "TASK=T1 SUBMIT=not attempted NEXT=Answer Q1 about b.ts\n"
    line = "Question (Q9): an old question that is cut?\n"
    body = "x" * 20000 + "\n" + line + "y\n" * ((16384 - len(tail) - 10) // 2) + tail
    (ddir / "output.log").write_text(body)
    q = dispatch._ended_on_question(run, {"id": "Dcut", "log_path": str(ddir / "output.log")})
    assert q and q["question"].startswith("Answer Q1"), q  # never the cut "...is cut?" fragment


def test_the_brief_reports_exactly_the_deliveries_it_renders():
    # Review #446 F8: brief-deliveries.json comes from the brief's own query.
    import inspect
    from office import briefs
    assert "carried" in inspect.signature(briefs.worker_brief).parameters
