"""T3: one helper (`gates.reviewer_pin`) resolves a user's reviewer pin for every role, and `run_reviewer` reads only it."""
from __future__ import annotations

import inspect
import json

from conftest import approved_run
from test_review_reroute import EXECUTOR, V31
from office import candidates, gates, state
from office.state import Refused

PIN = {"as": "codex/gpt-6-luna@xhigh", "cli": None, "external": False, "by": "user"}


def _set_task_pin(env, pin):
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'", (json.dumps(pin) if pin else None,))
    con.commit()


def _setup(env, monkeypatch, capable=True):
    approved_run(env, executor=EXECUTOR)
    con = env.con()
    run = con.execute("SELECT id FROM runs").fetchone()["id"]
    roles = []

    def fake(text, *, flag="--as", role=None, **kw):
        roles.append(role)
        if not capable:
            raise Refused("capability-missing", "route lacks the role's capability")
        return {"status": "selected", "candidate": {}}
    monkeypatch.setattr(candidates, "declared_decision", fake)
    return con, state.get_run(con, run), roles


@V31
def test_a_task_pin_applies_to_every_task_scoped_role(env, monkeypatch):
    con, run, roles = _setup(env, monkeypatch)
    _set_task_pin(env, PIN)
    gate = {"task_id": "T1"}
    for role in ("code_reviewer", "visual_reviewer", "integration_reviewer"):
        assert gates.reviewer_pin(con, run, role, gate) == PIN, role
    assert set(roles) == {"code_reviewer", "visual_reviewer", "integration_reviewer"}  # each role validated its own capability


@V31
def test_no_pin_is_none_for_every_role(env, monkeypatch):
    con, run, _ = _setup(env, monkeypatch)
    for role in ("code_reviewer", "visual_reviewer", "plan_reviewer"):
        assert gates.reviewer_pin(con, run, role, {"task_id": "T1"}) is None, role


@V31
def test_a_pin_lacking_the_roles_capability_does_not_apply_to_that_role(env, monkeypatch):
    con, run, _ = _setup(env, monkeypatch, capable=False)
    _set_task_pin(env, PIN)
    assert gates.reviewer_pin(con, run, "visual_reviewer", {"task_id": "T1"}) is None


@V31
def test_the_plan_reviewer_reads_the_run_level_pin_and_ignores_task_pins(env, monkeypatch):
    con, run, _ = _setup(env, monkeypatch)
    _set_task_pin(env, PIN)
    assert gates.reviewer_pin(con, run, "plan_reviewer") is None
    plan_pin = {**PIN, "as": "claude/claude-opus-5-5@high"}
    state.update_run(con, run["id"], plan_review={**(run.get("plan_review") or {}), "review_override": plan_pin})
    con.commit()
    assert gates.reviewer_pin(con, run, "plan_reviewer") == plan_pin  # read live, not from the caller's snapshot


@V31
def test_run_reviewer_has_no_pin_parameter_of_its_own():
    assert "review_override" not in inspect.signature(gates.run_reviewer).parameters
