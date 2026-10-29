"""#790: a codex reviewer's brief pointer typed into its Herdr pane must be submitted, not left in the composer.

Codex draws its composer with a `›` glyph and no border, and wraps the pointer over several rows. The fake
pane below keeps state: it holds the typed text until an Enter submits it, and it drops the first Enter the
way a TUI can when the paste is not finished. Office must press Enter once more before it declares the prompt
unlanded, and a reviewer route it leaves must log `route.fallback` with its reason.
"""
from __future__ import annotations

import json

import pytest

from test_convergence_contract import APPROVED, _start
from test_issue_790_held_pointer import _Pane
from test_review_reroute import EXECUTOR, _q
from office import dispatch

BUSY = "Working (1s • esc to interrupt)"
FOOTER = "  100% context left · ? for shortcuts"
POINTER = "Read and carry out the review brief at /runs/r/dispatches/D1/brief.md exactly. Write your complete review."
EMPTY_REPLY = {"reply": ""}


def _codex_composer(text: str) -> str:
    """Codex's composer: a `›` prompt glyph, the text wrapped at the pane width, then the footer."""
    rows = [text[i:i + 58] for i in range(0, len(text), 58)]
    body = "\n".join(("› " if i == 0 else "  ") + r for i, r in enumerate(rows))
    return f"{body}\n{FOOTER}"


class CodexPane:
    """Scripted codex pane. `agent prompt` types the text; the first Enter after it is dropped, the second submits it."""

    def __init__(self, monkeypatch, *, drop_enters: int = 1):
        self.composer, self.submitted, self.enters, self.prompts = None, False, 0, []
        self.drop_enters = drop_enters
        monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
        monkeypatch.setenv("OFFICE_HERDR_LAND_TIMEOUT", "0")
        monkeypatch.setattr(dispatch, "_pane_view", self.view)
        monkeypatch.setattr(dispatch, "_herdr_quiet", self.herdr)

    def herdr(self, *args):
        if args[:2] == ("agent", "prompt"):
            self.prompts.append(args[3])
            self.composer = args[3]
        elif args[:2] == ("pane", "send-keys") and args[-1] == "Enter":
            self.enters += 1
            if self.composer and self.enters > self.drop_enters:
                self.composer, self.submitted = None, True

    def view(self, name):
        if self.submitted:
            return f"{BUSY}\n{FOOTER}"
        return _codex_composer(self.composer) if self.composer else f"> \n{FOOTER}"


def test_codex_pointer_whose_first_enter_is_dropped_is_submitted_by_one_more_enter(monkeypatch):
    pane = CodexPane(monkeypatch, drop_enters=1)
    assert dispatch.submit_prompt("office-D1", POINTER, pane="w1:p1") == "landed"
    assert pane.prompts == [POINTER], "the pointer must never be typed twice"
    assert pane.enters == 2


def test_codex_pointer_that_never_submits_is_reported_held_after_bounded_enters(monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_ENTER_TRIES", "2")
    pane = CodexPane(monkeypatch, drop_enters=99)
    assert dispatch.submit_prompt("office-D1", POINTER, pane="w1:p1") == "held"
    assert pane.prompts == [POINTER] and pane.enters == 2


@pytest.mark.xfail(strict=True, reason="#790 repro: dispatch._settle_unlanded_pointer sends no Enter while herdr reports "
                                       "working; fix is pending the dispatch.py scope amendment")
def test_codex_pointer_held_while_herdr_reports_working_is_submitted(monkeypatch):
    pane = _Pane(monkeypatch, [_codex_composer(POINTER)], status="working")
    (landed, mismatch), spec = pane.settle()
    assert pane.enters, f"no Enter reached a composer that holds the brief; spec={spec}"


def test_reviewer_route_left_for_another_logs_route_fallback_with_its_reason(env, monkeypatch):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY_REPLY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    fallbacks = _q(env, "SELECT summary, payload_json FROM events WHERE kind='route.fallback' ORDER BY seq")
    assert len(fallbacks) == 1, [r["summary"] for r in fallbacks]
    payload = json.loads(fallbacks[0]["payload_json"])
    assert payload["before"] and payload["after"] and payload["before"] != payload["after"]
    assert payload["stage"] == "code_reviewer" and "no reply after re-prompts" in payload["reason"]
    assert payload["reason"] in fallbacks[0]["summary"]
