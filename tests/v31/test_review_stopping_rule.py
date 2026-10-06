"""Stopping rule: after the round budget, only a high finding blocks a task.

This suite covers the v3.1 review contract, which every run started before #337 (and any run
started with review.contract: v3.1) keeps for its whole life; it pins its runs to that contract.
The convergence contract is covered by test_convergence_contract.py.
"""
from __future__ import annotations

import pytest

from conftest import GOOD_ADD, approved_run, task_row

from office import review_parse

pytestmark = pytest.mark.review_contract("v3.1")


def test_levels_map_to_blocking_severity_and_old_words_still_parse():
    p = review_parse.parse("VERDICT: CHANGES_REQUIRED\n"
                           "FINDING F1 | high | a.py:1 | x | y\nFINDING F2 | medium | a.py:2 | x | y\n"
                           "FINDING F3 | low | a.py:3 | x | y\nFINDING F4 | material | a.py:4 | x | y\n"
                           "FINDING F5 | minor | a.py:5 | x | y")
    assert p.valid, p.errors
    got = {f["code"]: (f["severity"], f["level"]) for f in p.findings}
    assert got == {"F1": ("material", "high"), "F2": ("material", "medium"), "F3": ("minor", "low"),
                   "F4": ("material", "high"), "F5": ("minor", "low")}


def test_medium_findings_get_a_final_fix_round_then_a_verify_only_pass(env):
    medium = "VERDICT: CHANGES_REQUIRED\nFINDING F{n} | medium | calc.py:2 | a rare race | fix every caller"
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD + f"# try {i}\n"}, "submit": True} for i in range(6)],
                 code_reviewer=[{"reply": medium.format(n=i)} for i in range(1, 7)])
    env.office("dispatch", "T1", check=0)
    for _ in range(6):
        if task_row(env)["status"] != "changes_required":
            break
        env.office("rerun", "T1", "--fresh", check=0)
    t = task_row(env)
    assert t["status"] == "accepted", t
    con = env.con()
    kinds = [r[0] for r in con.execute("SELECT kind FROM events ORDER BY seq")]
    assert "gate.final_fix_round" in kinds and "gate.followups" in kinds
    assert con.execute("SELECT COUNT(*) FROM gates WHERE escalated=1").fetchone()[0] == 0
    last = con.execute("SELECT verdict, summary FROM gates WHERE kind='code_review' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert last["verdict"] == "PASS" and "verify-only" in last["summary"], dict(last)
    assert con.execute("SELECT COUNT(*) FROM findings WHERE state='deferred'").fetchone()[0] >= 1
    assert con.execute("SELECT COUNT(*) FROM findings WHERE state='open'").fetchone()[0] == 0


def test_a_high_finding_in_the_verify_only_round_still_blocks(env):
    medium = "VERDICT: CHANGES_REQUIRED\nFINDING F{n} | medium | calc.py:2 | a rare race | fix every caller"
    high = "VERDICT: CHANGES_REQUIRED\nFINDING F9 | high | calc.py:2 | loses data | fix it"
    # two mediums use the one final fix round; the high arrives in the verify-only review
    replies = [{"reply": medium.format(n=i)} for i in range(1, 3)] + [{"reply": high}] * 4
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD + f"# try {i}\n"}, "submit": True} for i in range(8)],
                 code_reviewer=replies)
    env.office("dispatch", "T1", check=0)
    for _ in range(8):
        if task_row(env)["status"] != "changes_required":
            break
        env.office("rerun", "T1", "--fresh", check=0)
    t = task_row(env)
    assert t["status"] != "accepted", t
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM findings WHERE code='F9' AND state='open'").fetchone()[0] == 1


def test_a_repeat_triggered_final_fix_makes_the_next_review_verify_only(env):
    same = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | medium | calc.py:2 | a rare race | fix every caller"
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD + f"# try {i}\n"}, "submit": True} for i in range(6)],
                 code_reviewer=[{"reply": same}] * 6)
    env.office("dispatch", "T1", check=0)
    for _ in range(6):
        if task_row(env)["status"] != "changes_required":
            break
        env.office("rerun", "T1", "--fresh", check=0)
    assert task_row(env)["status"] == "accepted", task_row(env)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='gate.final_fix_round'").fetchone()[0] == 1
    # round 1, round 2 (repeat -> the one final fix), then one verify-only review; no second final fix
    assert con.execute("SELECT COUNT(*) FROM gates WHERE kind='code_review'").fetchone()[0] == 3


def test_carried_findings_show_their_level():
    from office import briefs
    carried = [{"code": "F1", "severity": "material", "level": "medium", "location": "a.py:1", "summary": "race"},
               {"code": "F2", "severity": "material", "level": None, "location": "a.py:2", "summary": "old"}]
    rev = {"id": "R1", "commit_sha": "0" * 40}
    text = briefs.code_review_brief({}, {"id": "T1", "title": "t", "accept": [], "scope": []}, rev, "", "none",
                                    carried, "/tmp/x", verify_only=True)
    assert "- F1 [medium] a.py:1 race" in text and "- F2 [high] a.py:2 old" in text


def test_visual_review_is_not_governed_by_the_stopping_rule():
    from office import gates
    run = {"gates": {"visual_review_max_rounds": 1}}
    assert gates._verify_only(None, run, {"kind": "visual", "round": 9, "task_id": "T1"}) is False
