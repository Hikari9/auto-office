"""#958: a review pinned to a route in a quota or usage-limit cooldown is not launched into the wall; the review moves to
the next eligible route with a notice and the pin stays. A user-pinned reviewer with no cooldown on its route still runs.
A revoked review job is not a stall for `guide.stalls`. Reviewers are scripted fake harnesses."""
from __future__ import annotations

import json

import pytest

from conftest import approved_run, task_row
from test_convergence_contract import APPROVED, _q, _start, _status
from test_lane_gate_revoke import _rewind_to_running, _unavailable_lane
from test_review_reroute import EXECUTOR, PASS, V31, _code_gates, _pin, _reviewer, _triples
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
def test_a_harness_wide_cooldown_also_moves_a_pinned_review(env):
    def cool_harness(route):
        con = env.con()
        con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                    (routing.harness_demotion_key(_split(route)[0]), FAR, NOW))
        con.commit()
    pinned, launched = _review_with_pin(env, cool=cool_harness)
    assert launched and pinned not in launched, launched
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


@V31
def test_a_cooled_pin_with_no_eligible_alternative_names_its_expiry_and_the_exact_recovery(env, monkeypatch):
    from office import candidates
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", check=0)
    pinned = _reviewer(env)
    _pin_route(env, pinned)
    _cool(env, pinned, expires=FAR)
    real, launched_before = candidates.route_role, len(_triples(env))
    def no_other_reviewer(con, config, run, role, **kw):
        if role == "code_reviewer":  # no route but the cooled pin qualifies
            return {"status": "no_qualifying_route", "skipped": []}
        return real(con, config, run, role, **kw)
    monkeypatch.setattr(candidates, "route_role", no_other_reviewer)
    env.office("review", "T1")
    recovery = f"office review T1 --as {pinned}"
    gate = _code_gates(env)[-1]
    assert gate["verdict"] == "UNAVAILABLE" and len(_triples(env)) == launched_before, (dict(gate), _triples(env))
    assert recovery in gate["summary"] and "2999-01-01T00:00Z" in gate["summary"], gate["summary"]
    assert _pin(env)["as"] == pinned  # the pin stays on the task
    nxt = _status(env)["next"]
    assert recovery in nxt and "2999-01-01T00:00Z" in nxt, nxt
    # The same pin, no longer declarable (a route denied since): the status line still renders, with the generic recovery.
    from office.state import Refused
    real_declared = candidates.declared_decision
    def denied(route, **kw):
        if kw.get("flag") == "--review-as":
            raise Refused("route-denied", f"{route} is denied")
        return real_declared(route, **kw)
    monkeypatch.setattr(candidates, "declared_decision", denied)
    nxt = _status(env)["next"]
    assert "office review T1 [--as" in nxt and "in a cooldown" not in nxt, nxt
def test_a_lane_review_with_a_cooled_pin_hands_the_review_to_another_route(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    pinned = _reviewer(env)
    _pin_route(env, pinned)
    _cool(env, pinned, expires=FAR)
    before = len(_triples(env))
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1")
    launched = _triples(env)[before:]
    assert launched and pinned not in launched, launched
    assert _pin(env)["as"] == pinned  # the pin stays
    assert any("the pinned route" in r and "is in a cooldown until" in r for r in _fallback_reasons(env)), _fallback_reasons(env)


def test_a_revoked_lane_review_job_is_not_reported_as_a_stall(env):
    _unavailable_lane(env)
    _rewind_to_running(env)
    code, out = env.office("revoke", "L-T1:convergence")
    assert code == 0 and "Jrev" in out, out
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    job = _q(env, "SELECT status, error FROM outbox WHERE id='Jrev'")[0]
    assert job["status"] == "failed" and job["error"].startswith("revoked by orchestrator:"), dict(job)
    assert guide.stalls(con, run) == [], guide.stalls(con, run)

    # Negative controls: a job that failed for any other reason is still a stall, including a harness error
    # that merely starts with "revoked:" (the marker is the orchestrator's, not that word).
    for error in ("harness crashed", "revoked: refresh token", "Revoked by orchestrator? no: auth"):
        con.execute("UPDATE outbox SET error=? WHERE id='Jrev'", (error,))
        con.commit()
        assert any(f"job convergence_review failed: {error}" in s for s in guide.stalls(con, run)), (error, guide.stalls(con, run))
