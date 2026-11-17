"""#1115: a review pin records who chose it. `by: user` only when the command carried the user's `--quote`; an
orchestrator's pin says so. Storing a task or lane pin records a `route.changed` (kind repin) event by that actor, as
a plan re-pin does, and a pin that does not apply to every task or role of a lane says which.
"""
from __future__ import annotations

import json

import pytest

from office import candidates, gates

from test_convergence_contract import _q, _start
from test_review_reroute import EXECUTOR, GARBAGE, _pin, _reviewer

MANUAL = {"OFFICE_JOBS": "manual"}  # queue the review, do not run it


@pytest.mark.parametrize("quote, by", [(None, "orchestrator"), ("", "orchestrator"), ("   ", "orchestrator"),
                                       ("use luna for this", "user")])
def test_review_pin_takes_the_actor_from_the_quote(quote, by):
    assert gates.review_pin("codex/gpt-6-luna@xhigh", quote=quote)["by"] == by
    assert gates.review_pin(None, quote=quote) is None


def _repins(env):
    return [json.loads(r["payload_json"]) for r in _q(env, "SELECT payload_json FROM events WHERE kind='route.changed'")
            if json.loads(r["payload_json"]).get("kind") == "repin"]


@pytest.mark.parametrize("flags, by", [([], "orchestrator"), (["--quote", "the user said use it"], "user")])
def test_a_lane_review_pin_records_its_actor_and_a_repin_event(env, flags, by):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    reviewer = _reviewer(env)
    code, out = env.office("review", "L-T1", "--as", reviewer, *flags, env=MANUAL)
    assert code == 0, out
    assert _pin(env)["as"] == reviewer and _pin(env)["by"] == by
    (event,) = _repins(env)
    assert event["actor"] == by and event["after"] == reviewer and event["task_id"] == "L-T1"


def test_a_pin_covering_only_some_tasks_of_a_lane_names_the_uncovered_ones(monkeypatch):
    tasks = {"T1": {"review_override": {"as": "codex/gpt-6-luna@xhigh"}}, "T2": {}, "T3": {"review_override": {"as": "x/y"}}}
    monkeypatch.setattr(gates.state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(gates.state, "pinned_config", lambda run: {})
    monkeypatch.setattr(candidates, "declared_decision", lambda *a, **k: {})
    pin = {"as": "codex/gpt-6-luna@xhigh"}
    (line,) = gates.pin_notices(None, {"id": "R1"}, ["T1", "T2", "T3"], pin, [])
    assert "T2, T3" in line and "T1" not in line.split("no pin on")[1]
    assert gates.pin_notices(None, {"id": "R1"}, ["T1"], pin, []) == [], "a one-task lane is fully covered"


def test_a_pin_dropped_by_the_role_gate_prints_a_notice(monkeypatch):
    from office.state import Refused

    def refuse(*a, **k):
        raise Refused("route-cannot-serve-role", "cannot judge screenshots")

    monkeypatch.setattr(gates.state, "pinned_config", lambda run: {})
    monkeypatch.setattr(candidates, "declared_decision", refuse)
    (line,) = gates.pin_notices(None, {"id": "R1"}, ["T1"], {"as": "codex/gpt-6-luna@xhigh"}, ["visual_reviewer"])
    assert "dropped for the visual_reviewer" in line and "codex/gpt-6-luna@xhigh" in line
