"""#1260: the live "Adding a new directory" prompt draws a blank line between its heading and the path.

On 3.28.7 a claude reviewer (run 1ddfbfe7, D22aa782c) held on the prompt for its own dispatch dir: the path
reader stopped at that blank line, read no directory, and Office refused to answer. The screen below is the pane
as captured, trimmed above the rule.
"""
from __future__ import annotations

from office import dispatch

DISPATCH = "/Users/rico/.local/state/auto-office/runs/1ddfbfe7-a02373309230c3e9370ff1f8/dispatches/D22aa782c"

LIVE = "\n".join([
    "claude/skills --session-id 20247e52-2e77-4b69-9471-d3fb39",
    "da4aad",
    "─" * 57,
    " Adding a new directory:",
    "",
    " /Users/rico/.local/state/auto-office/runs/1ddfbfe7-a023",
    " 73309230c3e9370ff1f8/dispatches/D22aa782c",
    "",
    " This session hasn't worked here before. Is this a",
    " directory you created or one you trust?",
    "",
    " Claude Code'll be able to read, edit, and execute files",
    " here. Its skills, commands, and agents load and can",
    " run commands, and so can the git settings of its",
    " repository. Your answer holds for this session.",
    "",
    " Security guide",
    "",
    " ❯ No, exit",
    "   Yes, add it",
    "",
    " Enter to confirm · Esc to cancel",
])


def test_the_path_after_the_blank_line_is_read_whole():
    assert dispatch._trust_dialog(LIVE)
    assert dispatch._add_dir_target(LIVE) == DISPATCH


def test_a_heading_with_no_path_reads_none():
    assert dispatch._add_dir_target(" Adding a new directory:\n\n This session hasn't worked here before.") is None


def test_the_live_prompt_for_an_owned_dir_is_answered(monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_KEY_SETTLE", "0.3")
    yes = LIVE.replace(" ❯ No, exit", "   No, exit").replace("   Yes, add it", " ❯ Yes, add it")
    frames, sent = iter([yes, yes]), []
    assert dispatch._confirm_trust("w1:p1", lambda: next(frames, yes), lambda *a: sent.append(a[-1]), LIVE,
                                   owned=lambda p: p == DISPATCH)
    assert sent == ["down", "Enter"]
