"""#790: a codex reviewer's brief pointer typed into its Herdr pane must be submitted, not left in the composer.

Codex draws its composer with a `›` glyph and no border, and wraps the pointer over several rows. The fake
pane below keeps state: it holds the typed text until an Enter submits it, and it drops the first Enter the
way a TUI can when the paste is not finished. Office must press Enter once more before it declares the prompt
unlanded, and a reviewer route it leaves must log `route.fallback` with its reason.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run
from test_convergence_contract import APPROVED, _start
from test_issue_790_held_pointer import _Pane
from test_review_reroute import EXECUTOR, _q
from office import candidates, contract, dispatch, gates, review_parse, routing, state
from office.util import now_iso

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


def _working_sample(monkeypatch, *, text_busy):
    """herdr's agent status reads `working`; `text_busy` says whether the pane text shows a busy footer."""
    monkeypatch.setattr(dispatch, "_herdr_agent_sample",
                        lambda name: {"status": "working", "busy": True, "text_busy": text_busy})


def test_codex_pointer_held_while_herdr_reports_working_is_submitted(monkeypatch):
    pane = _Pane(monkeypatch, [_codex_composer(POINTER)], status="working")
    _working_sample(monkeypatch, text_busy=False)
    (landed, mismatch), spec = pane.settle()
    assert landed is True and mismatch is None and pane.enters == [("pane", "send-keys", "w1:p1", "Enter")], \
        f"expected one Enter on the Codex input row; spec={spec}"
    assert "landing_unconfirmed_working" not in spec


def test_working_pane_with_a_busy_footer_and_the_pointer_in_scrollback_gets_no_enter(monkeypatch):
    pane = _Pane(monkeypatch, [f"{BUSY}\n{_codex_composer(POINTER)}"], status="working")
    _working_sample(monkeypatch, text_busy=True)
    (landed, mismatch), spec = pane.settle()
    assert (landed, mismatch) == (False, None) and pane.enters == []
    assert spec == {"landing_unconfirmed_working": True}


def test_working_codex_pane_with_pointer_above_empty_input_row_gets_no_enter(monkeypatch):
    view = f"{_codex_composer(POINTER)}\n› \n{FOOTER}"
    pane = _Pane(monkeypatch, [view], status="working")
    _working_sample(monkeypatch, text_busy=False)
    (landed, mismatch), spec = pane.settle()
    assert (landed, mismatch) == (False, None) and pane.enters == []
    assert spec == {"landing_unconfirmed_working": True}


def test_reviewer_route_left_for_another_logs_route_fallback_with_its_reason(env, monkeypatch):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY_REPLY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    fallbacks = _q(env, "SELECT summary, payload_json FROM events WHERE kind='route.fallback' ORDER BY seq")
    assert len(fallbacks) == 1, [r["summary"] for r in fallbacks]
    payload = json.loads(fallbacks[0]["payload_json"])
    assert payload["before"] and payload["after"] and payload["before"] != payload["after"]
    assert payload["stage"] == "code_reviewer" and "no reply after re-prompts" in payload["reason"]
    assert payload["reason"] in fallbacks[0]["summary"]


# D1 (#827 wins): a live herdr reviewer that ended its turn normally and left an empty or verdict-less reply is
# re-prompted before any fallback. A reviewer that crashed, exited or reached a non-success status is not.

def _herdr_reviewer_run(env, monkeypatch, *, classification: str, reply: str | None, reprompt_calls: list):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, created_at) "
                "VALUES('G790', ?, 'T1', 'T1', 'code_review', 'd1-790', 'running', ?)", (run["id"], now_iso()))
    gate = dict(con.execute("SELECT * FROM gates WHERE id='G790'").fetchone())
    calls = []
    routes = [
        {"harness": "opencode", "adapter_id": "opencode", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "2.0.29"},
        {"harness": "claude", "adapter_id": "claude", "model_id": "fake-model",
         "invocation_model_id": "fake-model", "effort": "high", "harness_version": "1.0.0"},
    ]

    def route_role(_con, _config, _run, _role, *, exclude=None, **_kwargs):
        for candidate in routes:
            if routing.candidate_id(candidate) not in (exclude or []):
                calls.append(candidate["harness"])
                return {"status": "selected", "candidate": candidate,
                        "selected": routing.candidate_id(candidate), "selection_disclosure": {}}
        return {"status": "no_qualifying_route", "skipped": []}

    def launch(_run, reviewer, _kind, ddir, *, output, **_kwargs):
        # A live herdr pane (herdr reports it idle), the reviewer's own exit classification, and its reply file.
        if reply is not None:
            output.write_text(reply)
        con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p790', status='exited', "
                    "terminal_classification=?, ended_at=? WHERE id=?",
                    (classification, "2026-10-29T00:00:00+00:00", reviewer["id"]))

    def reprompt(_con, _run, _d, _ddir, output, parsed, **_kw):
        reprompt_calls.append(_d["id"])
        text = APPROVED
        return text, review_parse.parse(text, plan_review=False, visual=False, contract=contract.of(_run)), None

    monkeypatch.setattr(candidates, "route_role", route_role)
    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(gates, "_reprompt_until_valid", reprompt)
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    monkeypatch.setattr(gates, "_reviewer_pane_terminal_without_reply", lambda d, out: None)
    try:
        result = gates.run_reviewer(con, run, gate, "code_reviewer", "review", cwd=Path(env.repo))
    finally:
        con.close()
    return result, calls


@pytest.mark.parametrize("reply", [None, "Looks fine to me, no verdict line."])
def test_live_reviewer_with_empty_or_verdictless_reply_is_reprompted_before_fallback(env, monkeypatch, reply):
    reprompts: list = []
    result, calls = _herdr_reviewer_run(env, monkeypatch, classification="success", reply=reply, reprompt_calls=reprompts)
    assert len(reprompts) >= 1, "the same reviewer must be re-prompted before any fallback"
    assert calls == ["opencode"], calls
    assert result.get("verdict") == "APPROVED", result


@pytest.mark.parametrize("classification", ["nonzero", "timeout"])
def test_crashed_or_timed_out_live_reviewer_falls_back_without_reprompt(env, monkeypatch, classification):
    reprompts: list = []
    result, calls = _herdr_reviewer_run(env, monkeypatch, classification=classification, reply=None,
                                        reprompt_calls=reprompts)
    assert reprompts == [], "a launch failure is never re-prompted (#827)"
    assert calls == ["opencode", "claude"], calls
