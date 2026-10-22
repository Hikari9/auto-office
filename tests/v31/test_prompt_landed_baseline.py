"""A busy footer already on the pre-prompt screen is not proof a prompt landed (#738)."""
from __future__ import annotations

import pytest

from office import dispatch

PROMPT = "apply amendment A38 then office ack A38"
BUSY = "● Editing calc.py\n\n✻ Harmonizing… ({t}s · esc to interrupt)\n"
IDLE = "● done\n\n> \n  ? for shortcuts   ctx: 12k\n"


def busy(t=10, extra=""):
    return BUSY.format(t=t) + extra


def test_busy_footer_already_present_is_not_landed_without_a_change():
    before = busy(10)
    assert dispatch._landed_in(before, None)  # no baseline: the footer alone still counts
    assert not dispatch._landed_in(before, None, before)


def test_ticking_spinner_and_timer_are_not_a_change():
    assert not dispatch._landed_in(busy(42).replace("✻", "✶"), None, busy(10))


def test_changed_screen_with_busy_footer_is_landed():
    assert dispatch._landed_in(busy(12, "● Applying the amendment\n"), None, busy(10))


def test_prompt_sitting_in_the_composer_of_a_busy_pane_is_not_landed():
    typed = busy(12, f"│ {PROMPT}\n")
    assert not dispatch._landed_in(typed, None, busy(10), PROMPT)
    assert dispatch._landed_in(typed, None, busy(10))  # without the prompt known it is a change


def test_idle_baseline_with_busy_footer_is_landed():
    assert dispatch._landed_in(busy(1), None, IDLE)


def test_ctx_rise_lands_even_when_busy_baseline_is_unchanged():
    before = busy(10, "  ctx: 12k\n")
    assert dispatch._landed_in(busy(10, "  ctx: 15k\n"), 12.0, before)
    assert not dispatch._landed_in(before, 12.0, before)


def test_startup_screen_is_never_landed():
    assert not dispatch._landed_in("Hooks need review\n" + busy(1), None, IDLE)


@pytest.fixture
def pane(monkeypatch):
    views = []
    monkeypatch.setattr(dispatch, "_pane_view", lambda name: views.pop(0) if len(views) > 1 else views[0])
    monkeypatch.setattr(dispatch.time, "sleep", lambda s: None)
    return views


def test_prompt_landed_ignores_an_unchanged_busy_pane(pane):
    pane.append(busy(10))
    assert dispatch._prompt_landed("a", 0, baseline_screen=busy(10)) == ""


def test_prompt_landed_sees_a_post_prompt_change(pane):
    pane.extend([busy(10), busy(11, "● Applying the amendment\n")])
    assert dispatch._prompt_landed("a", 5, baseline_screen=busy(10)) == "landed"


def test_prompt_landed_takes_the_transcript_signal(pane):
    pane.append(busy(10))
    assert dispatch._prompt_landed("a", 0, baseline_screen=busy(10), seen=lambda: True) == "landed"


def _sent(monkeypatch):
    sent = []
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: sent.append(a))
    monkeypatch.setattr(dispatch, "_land_timeout", lambda: 0)
    monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
    return sent


def test_submit_prompt_to_a_busy_pane_needs_a_post_prompt_change(pane, monkeypatch):
    _sent(monkeypatch)
    pane.append(busy(10))  # the pane never changes: the prompt did not land
    assert dispatch.submit_prompt("a", PROMPT) == ""


def test_submit_prompt_to_a_busy_pane_lands_on_a_change(pane, monkeypatch):
    _sent(monkeypatch)
    pane.extend([busy(10), busy(11, "● Applying the amendment\n")])
    assert dispatch.submit_prompt("a", PROMPT) == "landed"


def test_submit_prompt_reports_a_prompt_left_in_a_busy_composer_as_held(pane, monkeypatch):
    sent = _sent(monkeypatch)
    pane.extend([busy(10), busy(11, f"│ {PROMPT}\n")])
    assert dispatch.submit_prompt("a", PROMPT) == "held"
    assert any(a[-1] == "Enter" for a in sent)
