"""#1029 (Rico, 2026-11-02): the root-wake prompt is one line naming what changed and the authorized next
action; the standing rules moved to the orchestrator skill."""
from pathlib import Path

from office import root_wake


def test_the_wake_line_names_the_change_and_the_next_action():
    line = root_wake.wake_line("67dcc801-0000", "T4 submitted", "next: office review L-egress", through=812)
    assert line == ("Run 67dcc801 (through event 812): T4 submitted; next: office review L-egress. "
                    "Run `office --run 67dcc801-0000 context`.")


def test_a_long_change_or_action_stays_one_bounded_line():
    line = root_wake.wake_line("r1234567", "x\n" * 300, "y " * 300)
    assert "\n" not in line and len(line) < root_wake.WAKE_CHANGE_MAX + root_wake.WAKE_NEXT_MAX + 80


def test_the_standing_rules_live_in_the_orchestrator_skill():
    skill = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text()
    for rule in ("(through event N)", "follow only its authorized next action", "A submit is not success", "A wake may be a duplicate",
                 "HUMAN DECISION REQUIRED"):
        assert rule in skill, rule
