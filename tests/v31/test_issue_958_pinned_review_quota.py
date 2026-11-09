"""#958: a review pinned to a route in a quota or usage-limit cooldown is not launched into the wall; the review moves to
the next eligible route with a notice and the pin stays. A user-pinned reviewer with no cooldown on its route still runs.
A revoked review job is not a stall for `guide.stalls`. Reviewers are scripted fake harnesses."""
from __future__ import annotations

import json

import pytest

from conftest import approved_run, task_row
from test_convergence_contract import _q
from test_lane_gate_revoke import _rewind_to_running, _unavailable_lane
from test_review_reroute import EXECUTOR, PASS, V31, _pin, _reviewer, _triples
from office import guide, routing, state

FAR = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"
NOW = "2026-11-09T00:00:00+00:00"


def _split(route: str) -> tuple[str, str, str]:
    """`codex/gpt-6-luna@xhigh` -> (harness, model, effort)."""
    harness, rest = route.split("/", 1)
    model, effort = rest.rsplit("@", 1)
    return harness, model, effort


def _pin_route(env, route: str) -> None:
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": route, "cli": None, "external": False, "by": "user"}),))
    con.commit()


def _cool(env, route: str, *, key: str = "demotion", expires: str = FAR) -> None:
    """A cooldown row as Office writes it: `quota` is the quota-wall key (any effort), `demotion` the per-effort one."""
    harness, model, effort = _split(route)
    name = routing.cooldown_key(harness, model) if key == "quota" else routing.demotion_key(harness, model, effort)
    con = env.con()
    con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)", (name, expires, NOW))
    con.commit()


def _fallback_reasons(env) -> list[str]:
    return [json.loads(r["payload_json"])["reason"] for r in _q(env, "SELECT payload_json FROM events WHERE kind='route.fallback'")]


def _review_with_pin(env, *, cool=None) -> tuple[str, list[str]]:
    """Blocked on an UNAVAILABLE review, pinned to a reviewer, optionally cooled; returns the pin and the routes
    launched by the review that follows."""
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", check=0)
    pinned = _reviewer(env)
    _pin_route(env, pinned)
    if cool is not None:
        cool(pinned)
    env.script(code_reviewer=[{"reply": PASS}])
    before = len(_triples(env))
    code, out = env.office("review", "T1")
    assert code == 0, out
    assert task_row(env)["status"] == "accepted", task_row(env)
    return pinned, _triples(env)[before:]


@V31
@pytest.mark.parametrize("key", ["demotion", "quota"])
def test_a_pinned_reviewer_in_a_cooldown_is_not_launched_and_the_review_moves_to_another_route(env, key):
    pinned, launched = _review_with_pin(env, cool=lambda route: _cool(env, route, key=key))
    assert launched and pinned not in launched, launched  # the cooled route is never launched
    assert _pin(env)["as"] == pinned and _pin(env)["by"] == "user"  # the pin is not rewritten
    assert any(f"the pinned route" in r and "is in a cooldown until" in r for r in _fallback_reasons(env)), _fallback_reasons(env)


@V31
def test_an_expired_cooldown_does_not_move_the_pinned_review(env):
    pinned, launched = _review_with_pin(env, cool=lambda route: _cool(env, route, expires=PAST))
    assert launched == [pinned], launched
    assert not any("in a cooldown" in r for r in _fallback_reasons(env)), _fallback_reasons(env)


@V31
def test_a_pinned_reviewer_with_no_cooldown_on_its_route_still_runs_on_it(env):
    def cool_other(pinned):
        other = "claude/claude-opus-5-5@high" if pinned.startswith("codex") else "codex/gpt-6-luna@xhigh"
        _cool(env, other)  # a cooldown on another route does not touch the pin
    pinned, launched = _review_with_pin(env, cool=cool_other)
    assert launched == [pinned], launched
    assert not any("in a cooldown" in r for r in _fallback_reasons(env)), _fallback_reasons(env)


def test_a_revoked_lane_review_job_is_not_reported_as_a_stall(env):
    _unavailable_lane(env)
    _rewind_to_running(env)
    code, out = env.office("revoke", "L-T1:convergence")
    assert code == 0 and "Jrev" in out, out
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    job = _q(env, "SELECT status, error FROM outbox WHERE id='Jrev'")[0]
    assert job["status"] == "failed" and job["error"].startswith("revoked:"), dict(job)
    assert guide.stalls(con, run) == [], guide.stalls(con, run)

    # Negative control: a job that failed for any other reason is still a stall.
    con.execute("UPDATE outbox SET error='harness crashed' WHERE id='Jrev'")
    con.commit()
    assert any("job convergence_review failed: harness crashed" in s for s in guide.stalls(con, run)), guide.stalls(con, run)
