"""A brief pointer held in the composer is submitted with Enter, never typed again (M6).

Fixtures follow run ff2d70bb dispatch D0b5b1624: claude in a 42-column herdr
pane held the pointer twice, the second copy missing its leading "Read and ",
with blank rows below the footer.
"""
from __future__ import annotations

import textwrap

import pytest

from office import dispatch

from test_herdr_agent_launch import _calls, _launch_events, launch_in_herdr

POINTER = ("Read and carry out the brief at /Users/x/.local/state/auto-office/runs/ff2d70bb-54b7/dispatches/"
           "D0b5b1624/brief.md exactly. When the work and its checks are complete, run: office submit")


def _pane(*composer: str, width: int = 42, blank_rows: int = 12) -> str:
    """A narrow claude pane: transcript, the wrapped composer, its footer, then blank rows."""
    rows = ["Claude Code v3", "", "> earlier message", ""]
    for text in composer:
        rows += ["│ " + line for line in textwrap.wrap(text, width - 2)]
    rows += ["  ? for shortcuts            ctx 12k"] + [""] * blank_rows
    return "\n".join(rows)


HELD_TWICE = _pane(POINTER, POINTER[len("Read and "):])


def test_held_pointer_is_found_below_trailing_blank_rows():
    view = _pane(POINTER, blank_rows=20)
    assert len(view.splitlines()) > 20
    assert dispatch._composer_holds(view, POINTER)


def test_held_pointer_with_a_mangled_start_is_found():
    assert dispatch._composer_holds(_pane(POINTER[len("Read and "):]), POINTER)
    assert dispatch._composer_holds(HELD_TWICE, POINTER)


def test_empty_or_unrelated_composer_holds_nothing():
    assert not dispatch._composer_holds(_pane("fix the lint warning"), POINTER)
    assert not dispatch._composer_holds(None, POINTER)
    assert not dispatch._composer_holds("", POINTER)


def _stub(monkeypatch, views, landed="") -> None:
    seq = list(views)
    monkeypatch.setattr(dispatch, "_pane_view", lambda name: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(dispatch, "_prompt_landed", lambda *a, **kw: landed)
    monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")


def test_unreadable_pane_is_never_an_absent_composer(monkeypatch):
    _stub(monkeypatch, [None])
    keys: list = []
    got = dispatch._submit_held("a", "w1:p1", POINTER, 0, herdr=lambda *a: keys.append(a))
    assert got != "absent"
    assert keys and all(k[-1] == "Enter" for k in keys)


def test_held_pointer_is_submitted_with_enter_not_reported_absent(monkeypatch):
    _stub(monkeypatch, [HELD_TWICE])
    keys: list = []
    assert dispatch._submit_held("a", "w1:p1", POINTER, 0, herdr=lambda *a: keys.append(a)) == "held"
    assert keys and all(k[-1] == "Enter" for k in keys)


@pytest.mark.parametrize("recheck", [HELD_TWICE, None])
def test_retype_path_never_types_a_held_or_unreadable_pointer(monkeypatch, recheck):
    # _submit_held saw an empty composer, but the pane holds the pointer (or
    # cannot be read) right before the retype.
    calls: list = []
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: calls.append(a))
    monkeypatch.setattr(dispatch, "_await_agent_ui", lambda *a, **kw: "ready")
    monkeypatch.setattr(dispatch, "_agent_up", lambda *a: True)
    monkeypatch.setattr(dispatch, "_submit_held", lambda *a, **kw: "absent")
    monkeypatch.setattr(dispatch, "_land_timeout", lambda: 0)
    _stub(monkeypatch, ["> composer empty", recheck])
    assert dispatch._deliver_prompt("a", "w1:p1", POINTER) is False
    assert not any(c[:2] == ("pane", "send-text") for c in calls)


@pytest.mark.approved
def test_pointer_held_at_the_end_is_reported_as_typed_not_relaunched(env, monkeypatch):
    def reads(brief):
        pointer = f"Read and carry out the brief at {brief} exactly."
        return ["> composer empty", "> composer empty", _pane(pointer, pointer[len("Read and "):])]

    state_file, run, d, _, res = launch_in_herdr(env, monkeypatch, reads=reads)
    assert res["prompt_landed"] is False
    assert not any(c[:2] == ["pane", "send-text"] for c in _calls(state_file))
    events = _launch_events(env, run)
    assert len(events) == 1 and "typed but unsubmitted" in events[0] and "did not land" not in events[0]
