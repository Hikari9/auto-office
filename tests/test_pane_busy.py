"""A pane waiting on its own background shells/monitors is busy, not stalled."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from office import dispatch  # noqa: E402

SPINNER = """\
● Running the suite.

✽ Harmonizing… (1m 2s · ↓ 1.2k tokens)

╭──────────────────────────────╮
│ >                            │
╰──────────────────────────────╯
  ⏵⏵ accept edits on (shift+tab to cycle)
"""

BACKGROUND = """\
● Started the watcher; waiting on it.

╭──────────────────────────────╮
│ >                            │
╰──────────────────────────────╯
  ⏵⏵ bypass permissions on · 1 shell, 3 monitors still running
"""

IDLE = """\
● Done. Nothing else to do.

╭──────────────────────────────╮
│ >                            │
╰──────────────────────────────╯
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

SUBMITTED = """\
● Submitted.

TASK=T7 COMMIT=abc1234 PUSHED=yes CHECKS=pass 4/4 SUBMIT=accepted R1 NEXT=none

╭──────────────────────────────╮
│ >                            │
╰──────────────────────────────╯
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def _footer(segment: str) -> str:
    return IDLE.replace("bypass permissions on (shift+tab to cycle)", f"bypass permissions on · {segment}")


def test_spinner_is_busy():
    assert dispatch._pane_busy(SPINNER)


def test_background_footer_is_busy():
    assert dispatch._pane_busy(BACKGROUND)


@pytest.mark.parametrize("segment", [
    "1 shell", "2 shells", "3 monitors", "1 monitor", "1 shell, 3 monitors",
    "1 shell, 3 monitors still running", "2 shells still running",
])
def test_background_counts_are_busy(segment):
    assert dispatch._pane_busy(_footer(segment))
    assert dispatch._pane_busy(f"· {segment}")
    assert dispatch._pane_busy(segment)


def test_plain_idle_prompt_is_not_busy():
    assert not dispatch._pane_busy(IDLE)


def test_final_submit_line_without_background_is_not_busy():
    assert not dispatch._pane_busy(SUBMITTED)


@pytest.mark.parametrize("text", [
    "● Ran 1 shell command and it passed.\n❯ ",
    "● I closed 2 monitors on the desk.\n❯ ",
    "0 shells would be fine\n❯ ",
])
def test_prose_mentioning_shells_is_not_busy(text):
    assert not dispatch._pane_busy(text)


def test_stale_count_scrolled_out_of_footer_is_not_busy():
    scrolled = "· 1 shell, 3 monitors still running\n" + "\n".join(f"line {i}" for i in range(20)) + "\n" + IDLE
    assert not dispatch._pane_busy(scrolled)
