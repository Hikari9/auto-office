"""#1239: Claude Code's "Adding a new directory" prompt for an untrusted `--add-dir`.

A claude reviewer launched with `--add-dir` for its dispatch dir and for all of Office's state home stalled on one
prompt per directory, recorded as an unrecognised startup screen. Office now answers the prompt ("Yes, add it",
session only) when the directory it names is Office-owned for the run, never for the state home or anything else.
"""
from __future__ import annotations

import sys

import pytest

from office import dispatch

DISPATCH = "/Users/u/.local/state/auto-office/runs/5fbb00d3-69b9f560e050a5fd30a1f90e/dispatches/D8da8e71b"
STATE = "/Users/u/.local/state/auto-office"


def _screen(path_lines, selected_yes=False):
    """The prompt as a narrow pane drew it on 2026-11-17: the path wraps mid-token."""
    return "\n".join([
        "dd-dir /Users/u/.claude/skills --session-id e9c89368",
        "─" * 40,
        " Adding a new directory:",
        *(f" {ln}" for ln in path_lines),
        " This session hasn't worked here before. Is this a",
        " directory you created or one you trust?",
        " Starting Claude Code in this directory and trusting it",
        " there lasts, and covers its whole repository or",
        " everything below it.",
        " Security guide",
        "   No, exit" if selected_yes else " ❯ No, exit",
        " ❯ Yes, add it" if selected_yes else "   Yes, add it",
        " Enter to confirm · Esc to cancel",
    ])


WRAPPED = ["/Users/u/.local/state/auto-office/runs/5fbb00d3-69b9", "f560e050a5fd30a1f90e/dispatches/D8da8e71b"]


def test_the_prompt_is_a_trust_dialog_and_its_wrapped_directory_is_read_whole():
    no = _screen(WRAPPED)
    assert dispatch._trust_dialog(no)
    assert dispatch._add_dir_target(no) == DISPATCH
    assert dispatch._add_dir_target(_screen([STATE])) == STATE
    assert dispatch._add_dir_target("Quick safety check: Is this a project you created or one you trust?") is None


def test_yes_add_it_counts_only_on_an_added_directory_prompt():
    assert dispatch._trust_selected("Yes, add it", add_dir=True)
    assert not dispatch._trust_selected("Yes, add it")
    assert not dispatch._trust_selected("No, exit", add_dir=True)


def test_an_owned_directory_is_added_after_moving_off_no_exit(monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_KEY_SETTLE", "0.3")
    no, yes = _screen(WRAPPED), _screen(WRAPPED, selected_yes=True)
    frames, sent, asked = iter([yes, yes]), [], []
    owned = lambda p: asked.append(p) or p == DISPATCH  # noqa: E731
    assert dispatch._confirm_trust("w1:p1", lambda: next(frames, yes), lambda *a: sent.append(a[-1]), no, owned=owned)
    assert sent == ["down", "Enter"] and asked == [DISPATCH]


def test_the_state_home_or_an_unknown_owner_is_never_added(monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_KEY_SETTLE", "0.3")
    for screen, owned in ((_screen([STATE]), lambda p: p == DISPATCH), (_screen(WRAPPED), None)):
        sent = []
        assert not dispatch._confirm_trust("w1:p1", lambda: screen, lambda *a: sent.append(a[-1]), screen, owned=owned)
        assert sent == [], "no key is pressed on a prompt Office may not answer"


def test_startup_answers_the_dispatch_dir_then_stops_at_the_state_home(monkeypatch):
    """The 2026-11-17 sequence: the dispatch dir prompt, then the state home one. Office answers the first and
    leaves the second to the user (with #1239's read roots it never appears)."""
    monkeypatch.setenv("OFFICE_HERDR_KEY_SETTLE", "0.3")
    yes1, yes2 = _screen(WRAPPED, selected_yes=True), _screen([STATE], selected_yes=True)
    screens = iter([yes1, yes1, yes2, yes2])
    sent = []
    monkeypatch.setattr(dispatch, "_pane_visible", lambda pane: next(screens, yes2))
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: sent.append(a[-1]))
    monkeypatch.setattr(dispatch, "_herdr_json", lambda args: {"status": "blocked"})
    assert not dispatch._answer_startup_trust("w1:p1", "x", timeout=1, owned=lambda p: p == DISPATCH)
    assert sent == ["Enter"]


def test_startup_answers_consecutive_owned_prompts_then_the_agent_is_up(monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_KEY_SETTLE", "0.3")
    run_dir = "/Users/u/.local/state/auto-office/runs/5fbb00d3-69b9f560e050a5fd30a1f90e"
    yes1, yes2 = _screen(WRAPPED, selected_yes=True), _screen([run_dir], selected_yes=True)
    screens = iter([yes1, yes1, yes2, yes2])
    sent = []
    monkeypatch.setattr(dispatch, "_pane_visible", lambda pane: next(screens, "> composer"))
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: sent.append(a[-1]))
    monkeypatch.setattr(dispatch, "_herdr_json", lambda args: {"status": "idle"})
    assert dispatch._answer_startup_trust("w1:p1", "x", timeout=2, owned=lambda p: p.startswith(run_dir))
    assert sent == ["Enter", "Enter"]


def test_office_owned_covers_the_run_and_its_dispatch_dirs_not_the_state_home(env):
    from office import paths
    run = {"id": "5fbb00d3-69b9f560e050a5fd30a1f90e"}
    rd = paths.run_dir(run["id"])
    (rd / "dispatches" / "D1").mkdir(parents=True)
    assert dispatch._office_owned(run, rd) and dispatch._office_owned(run, rd / "dispatches" / "D1")
    assert not dispatch._office_owned(run, paths.state_home()) and not dispatch._office_owned(run, paths.data_home())
    assert not dispatch._office_owned(run, paths.run_dir("other-run"))


@pytest.mark.skipif(sys.platform != "win32", reason="a drive path is absolute only on Windows")
def test_the_prompt_names_a_windows_directory():
    """Only a path starting with `/` counted, so on Windows no "Adding a new directory" prompt was ever answered."""
    screen = ("Adding a new directory:\n\n C:\\Users\\me\\state\\runs\\abc\n -def\\dispatches\\D1\n"
              " This session hasn't worked here before. Is this a directory you created or one you trust?\n")
    assert dispatch._add_dir_target(screen) == "C:\\Users\\me\\state\\runs\\abc-def\\dispatches\\D1"
    assert dispatch._add_dir_target(screen.replace("C:\\Users", "Users")) is None  # a relative path is still refused
