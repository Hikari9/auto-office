"""A Herdr pane reviewer that hit its harness's quota wall (opencode's "Free usage limit
reached") is a launch failure read before any re-prompt: the chain moves to another harness
at once. Run 12922516 (2026-11-02) spent eight minutes re-prompting a walled deepseek reviewer."""
from __future__ import annotations

import json

import pytest

from test_convergence_contract import APPROVED, _start
from test_review_reroute import EXECUTOR, _q

from office import gates

WALL = ("thinking about the change\nLet me read the rest of the test file.\n\n"
        "Free usage limit reached. Free models work in OpenCode (https://opencode.ai);\n"
        "subscribe to OpenCode Go to keep going: https://opencode.ai/go\n\n┃\n\n"
        "office-reviewer · DeepSeek V4.1 Flash Free [max] · 94.0K (47%) · OpenCode\n")


def _pane(tmp_path, text, **d):
    (tmp_path / "pane-tail.txt").write_text(text)
    return {"id": "D1", "launcher": "herdr", "pane_id": "w1:p1", "adapter_id": "opencode", "harness": "opencode", **d}


def test_the_adapters_wall_in_the_panes_last_lines_is_found(tmp_path):
    assert gates._pane_quota_wall(_pane(tmp_path, WALL), tmp_path) == "free-usage-limit-reached"


@pytest.mark.parametrize("text, extra", [
    ("Free usage limit reached\n" + "the review goes on\n" * 20, {}),  # far above the last lines: review text
    (WALL, {"launcher": "headless"}),  # not a pane agent
    ("all good, writing the reply\n", {}),
])
def test_review_text_or_a_non_pane_agent_is_not_a_wall(tmp_path, text, extra):
    assert gates._pane_quota_wall(_pane(tmp_path, text, **extra), tmp_path) is None


def test_a_walled_reviewer_moves_on_without_a_reprompt(env, monkeypatch):
    calls = []

    def walled(d, ddir):
        calls.append(d["id"])
        return "free-usage-limit-reached" if len(calls) == 1 else None

    monkeypatch.setattr(gates, "_pane_quota_wall", walled)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": ""}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert not _q(env, "SELECT * FROM events WHERE kind='review.reprompt'")
    changes = [json.loads(e["payload_json"]) for e in _q(env, "SELECT * FROM events WHERE kind='route.changed'")]
    assert any("[quota: free-usage-limit-reached]" in c.get("reason", "") for c in changes), changes
