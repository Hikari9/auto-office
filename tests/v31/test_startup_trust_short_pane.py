"""#726: a startup dialog taller than its pane is still classified, snapshotted and answered by keys.

The screens are recorded text of Claude's folder-trust dialog (no numbered options). In a short pane
the viewport carries only the last rows, so the question that names the dialog sits above it.
"""
from __future__ import annotations

import json

import pytest

import test_herdr_agent_launch as hal
from office import db, dispatch, startup

QUESTION = " Quick safety check: Is this a project you created or one you trust?"
DIALOG = ("Accessing workspace:\n /Users/rico/project\n" + QUESTION + "\n"
          "  Yes, I trust this folder\n❯ No, exit\n Enter to confirm · Esc to cancel")
VIEWPORT = "❯ No, exit\n Enter to confirm · Esc to cancel"
NUMBERED = ("Accessing workspace:\n" + QUESTION + "\n❯ 1. Yes, I trust this folder\n  2. No, exit\n"
            " Enter to confirm · Esc to cancel")


@pytest.fixture
def panes(monkeypatch):
    """Point the startup reads at recorded screens: visible and recent text."""
    screens = {"visible": VIEWPORT, "recent": DIALOG}
    monkeypatch.setattr(dispatch, "_pane_visible", lambda pane: screens["visible"])
    monkeypatch.setattr(dispatch, "_pane_read", lambda pane: screens["recent"])
    return screens


def test_a_viewport_without_a_marker_borrows_the_question_from_the_recent_read(panes):
    assert startup.label(VIEWPORT) == startup.UNRECOGNIZED  # what the old visible-only read saw
    text = startup.screen_text("w1:p2")
    assert QUESTION.strip() in text and "❯ No, exit" in text
    assert startup.label(text) == dispatch.TRUST_SCREEN


def test_a_visible_marker_needs_no_second_read(panes, monkeypatch):
    panes["visible"] = DIALOG
    monkeypatch.setattr(dispatch, "_pane_read", lambda pane: pytest.fail("recent read taken"))
    assert startup.screen_text("w1:p2") == DIALOG


def test_the_recent_read_is_bounded(panes):
    panes["recent"] = QUESTION + "\n" + "\n".join(f"line {i}" for i in range(startup.RECENT_LINES)) + "\n" + VIEWPORT
    assert startup.screen_text("w1:p2") == VIEWPORT  # the question is further back than the window


def test_a_marker_in_older_output_is_not_the_screen_up_now(panes):
    # The dialog was answered; the pane now shows something else, and the scrollback still has the dialog.
    panes["visible"] = "● ready\n  ? for shortcuts"
    panes["recent"] = DIALOG + "\n● ready\n  ? for shortcuts"
    text = startup.screen_text("w1:p2")
    assert text == panes["visible"] and startup.label(text) == startup.UNRECOGNIZED


def test_a_recent_read_that_does_not_end_with_the_viewport_is_ignored(panes):
    panes["recent"] = DIALOG.replace("❯ No, exit", "❯ Yes, I trust this folder")
    assert startup.screen_text("w1:p2") == VIEWPORT


def test_an_unreadable_pane_stays_empty(panes):
    panes["visible"] = ""
    assert startup.screen_text("w1:p2") == ""


def test_the_borrowed_screen_keeps_its_fingerprint_when_the_cursor_moves(panes):
    before = startup.fingerprint(startup.screen_text("w1:p2"))
    panes["visible"] = "  No, exit\n Enter to confirm · Esc to cancel"
    panes["recent"] = DIALOG.replace("  Yes, I trust this folder\n❯ No, exit", "❯ Yes, I trust this folder\n  No, exit")
    assert startup.fingerprint(startup.screen_text("w1:p2")) == before


def test_hint_offers_keys_without_digits_when_there_are_no_numbered_options():
    assert startup.options(DIALOG) == []
    hint = startup.answer_command("D1", startup.options(DIALOG), "abc123")
    assert "--keys <esc|enter|up|down>" in hint and "--choice" not in hint
    assert "1-9" not in hint and "--expect abc123" in hint


def test_hint_keeps_choice_for_numbered_options():
    opts = startup.options(NUMBERED)
    assert [o["n"] for o in opts] == [1, 2]
    hint = startup.answer_command("D1", opts, "abc123")
    assert "--choice <1-2>" in hint and "--keys <" not in hint


# --------------------------------------------------------------- through the launcher

BRANCH = r'''elif os.environ.get("FAKE_HERDR_SHORT") and args[:2] == ["agent", "start"]:
    code = 1
    result = {{"error": {{"code": "agent_not_ready", "message": "agent is blocked during startup"}}}}
    data.setdefault("pane_agents", {{}})[args[args.index("--pane") + 1]] = args[2]
    data.setdefault("pane_kinds", {{}})[args[args.index("--pane") + 1]] = args[args.index("--kind") + 1]
    data["short"], data["asked"] = True, True
elif args[:2] == ["pane", "read"] and data.get("short"):
    full = os.environ["FAKE_HERDR_DIALOG"]
    print("\n".join(full.splitlines()[-2:]) if args[args.index("--source") + 1] == "visible" else full)
    json.dump(data, open(state, "w"))
    sys.exit(0)
elif args[:2] == ["pane", "read"] and data.get("asked") and args[args.index("--source") + 1] != "visible":
    print(os.environ["FAKE_HERDR_DIALOG"])  # answered, but the dialog is still in the scrollback
    sys.exit(0)
elif args[:2] == ["pane", "send-keys"] and data.get("short"):
    data.setdefault("short_keys", []).append(args[3])
    if args[3] == "esc":
        data["short"] = False
elif args[:2] == ["agent", "get"] and data.get("short"):
    result = {{"agent": {{"name": args[2], "agent_status": "blocked", "launch_pending": True}}}}
    for pane, name in data.get("pane_agents", {{}}).items():
        if name == args[2]:
            result["agent"].update({{"pane_id": pane, "agent": data.get("pane_kinds", {{}}).get(pane)}})
'''


@pytest.fixture
def short_herdr(monkeypatch):
    anchor = 'elif args[:2] == ["agent", "start"] and os.environ.get("FAKE_HERDR_TRUST_DIALOG"):'
    monkeypatch.setattr(hal, "FAKE_HERDR", hal.FAKE_HERDR.replace(anchor, BRANCH + anchor, 1))
    for k, v in {"FAKE_HERDR_SHORT": "1", "FAKE_HERDR_DIALOG": DIALOG, "OFFICE_STARTUP_PROMPT_WAIT": "30",
                 "OFFICE_STARTUP_PROMPT_POLL": "0.01", "OFFICE_STARTUP_PROMPT_SETTLE": "0"}.items():
        monkeypatch.setenv(k, v)


def _rows():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM startup_prompts ORDER BY created_at")]
    finally:
        con.close()


@pytest.mark.approved
def test_a_short_pane_trust_dialog_is_labelled_snapshotted_and_answered_by_keys(env, monkeypatch, short_herdr):
    seen = {}

    def answer(_seconds):
        rows = [r for r in _rows() if r["state"] == "waiting"]
        if rows and "row" not in seen:
            seen["row"] = rows[0]
            seen["line"] = startup.line(rows[0])
            seen["snapshot"] = open(rows[0]["snapshot_path"]).read()
            code, out = env.office("answer", rows[0]["dispatch_id"], "--keys", "esc", "--expect", rows[0]["fingerprint"])
            assert code == 0, out
    monkeypatch.setattr(startup, "_sleep", answer)
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY],
                                                        adapter="claude", model="claude-opus-4-7", effort="high")
    row = seen["row"]
    assert row["screen"] == dispatch.TRUST_SCREEN
    assert QUESTION.strip() in seen["snapshot"] and "❯ No, exit" in seen["snapshot"]
    assert "--keys <esc|enter|up|down>" in seen["line"] and "--choice" not in seen["line"]
    # Only the answer the orchestrator gave was pressed; Office chose nothing.
    assert json.loads(state_file.read_text())["short_keys"] == ["esc"]
    # The dialog lingers in the scrollback after the answer; the visible screen alone ends the hold.
    assert [r["state"] for r in _rows()] == ["resolved"] and res["launcher"] == "herdr"
