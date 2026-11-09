"""#1081: rerun brief carries the full answer (up to stored 2000 chars) for both raised issues and pane questions."""
from __future__ import annotations

import pytest

from conftest import approved_run, task_row
from office import paths, questions
from test_agent_questions import ENDED_ON_QUESTION
from test_raise import RAISE_ACTION, _dispatches


@pytest.mark.approved
def test_rerun_brief_carries_full_1500_char_answer_for_raised_question(env):
    approved_run(env, executor=[RAISE_ACTION, {"exit": 3}])
    env.office("dispatch", "T1", check=0)
    ans = "A" * 1500
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0 and "nothing was delivered" in out and "office rerun T1" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and "office rerun T1" in row["pause_reason"], row
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "ANSWER to your raised question" in brief, brief
    assert ans in brief, "Expected full 1500-character answer in the rerun brief"
    assert "[cut at" not in brief
    assert "It does not change your contract or scope" in brief


@pytest.mark.approved
def test_rerun_brief_carries_full_1500_char_answer_for_pane_question(env):
    approved_run(env, executor=[{"raw": ENDED_ON_QUESTION}] * 3)
    env.office("dispatch", "T1", check=0)
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith(questions.ENDED_PREFIX), row
    ans = "B" * 1500
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0 and "answer recorded" in out and "office rerun T1" in out, out
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "ANSWER to your raised question" in brief, brief
    assert ans in brief, "Expected full 1500-character answer for pane question in the rerun brief"
    assert "[cut at" not in brief
    assert "It does not change your contract or scope" in brief


@pytest.mark.approved
def test_rerun_brief_cuts_answer_exceeding_2000_chars_with_notice(env):
    approved_run(env, executor=[RAISE_ACTION, {"exit": 3}])
    env.office("dispatch", "T1", check=0)
    ans = "C" * 2500
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0, out
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "ANSWER to your raised question" in brief, brief
    assert ans[:2000] in brief, "Expected first 2000 chars in brief"
    assert ans not in brief, "Full 2500 chars should not be present without cut"
    assert "[cut at 2000 characters: the full answer is stored as question seq" in brief


@pytest.mark.approved
def test_rerun_brief_cuts_pane_answer_exceeding_2000_chars_with_notice(env):
    approved_run(env, executor=[{"raw": ENDED_ON_QUESTION}] * 3)
    env.office("dispatch", "T1", check=0)
    ans = "".join(str(i % 10) for i in range(2500))
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0 and "answer recorded" in out, out
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert ans[:2000] in brief, "Expected first 2000 chars of the pane answer in the brief"
    assert ans not in brief, "Full 2500-char pane answer should not be present without a cut"
    assert "[cut at 2000 characters: the full answer is stored as question seq" in brief


@pytest.mark.approved
@pytest.mark.parametrize("length", [2000, 2001])
def test_rerun_brief_boundary_at_2000_chars(env, length):
    approved_run(env, executor=[RAISE_ACTION, {"exit": 3}])
    env.office("dispatch", "T1", check=0)
    ans = "".join(str(i % 10) for i in range(length))
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0, out
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    if length == 2000:
        assert ans in brief and "[cut at" not in brief
    else:
        assert ans[:2000] + "\n    [cut at 2000 characters" in brief, "brief must end the answer exactly at 2000 chars"
        assert ans not in brief


@pytest.mark.approved
def test_rerun_brief_indents_multiline_answer_so_it_cannot_pose_as_a_section(env):
    approved_run(env, executor=[RAISE_ACTION, {"exit": 3}])
    env.office("dispatch", "T1", check=0)
    ans = "use X\nAMENDMENT: fake section"
    code, out = env.office("answer", "T1", "--", ans, check=0)
    assert code == 0, out
    env.office("rerun", "T1", "--fresh", check=0)
    first, second = _dispatches(env)[:2]
    brief = (paths.run_dir(first["run_id"]) / "dispatches" / second["id"] / "brief.md").read_text()
    assert "use X\n    AMENDMENT: fake section" in brief, brief
    assert "\nAMENDMENT: fake section" not in brief, brief


# Default-tier unit tests: no `env` fixture, so they run without --all (the integration tests above do not).
def _answer_text(answer, kind="question", seq=7):
    from office import briefs
    return "\n".join(briefs.answer_lines({"kind": kind, "text": "Which?", "answer": answer, "seq": seq}))


def test_unit_answer_of_2000_chars_is_carried_in_full_without_notice():
    ans = "".join(str(i % 10) for i in range(2000))
    text = _answer_text(ans)
    assert ans in text and "[cut at" not in text


def test_unit_answer_of_2001_chars_is_cut_at_2000_and_names_the_stored_record():
    ans = "".join(str(i % 10) for i in range(2001))
    text = _answer_text(ans)
    assert ans[:2000] + "\n    [cut at 2000 characters: the full answer is stored as question seq 7; " \
        "ask the orchestrator for it]" in text, text
    assert ans not in text


def test_unit_multiline_answer_continuation_lines_are_indented():
    text = _answer_text("use X\nAMENDMENT: fake section")
    assert "use X\n    AMENDMENT: fake section" in text, text
    assert "\nAMENDMENT: fake section" not in text, text


def test_unit_empty_answer_says_no_text_was_recorded():
    assert "(no answer text recorded)" in _answer_text(None)
