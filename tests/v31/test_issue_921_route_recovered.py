"""#921: a route a run fell back from or avoided is recorded as available again, once per episode.

Unit tier. The same isolated Office home and scripted fake harness as the #845 suite: a real runs.db and the real
dispatch and routing modules. No model is called.
"""
import json

from test_discovery_dispatch import Cold, cold, launched  # noqa: F401 (cold is a fixture)
from test_issue_845_user_allow import events, fail, plain  # noqa: F401 (plain is a fixture)

from office import db, guide, root_wake, routechange, routing, state

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
RECOVERED = routechange.RECOVERED_KIND


def run(env):
    return state.get_run(env.con, "run-A")


def sweep(env):
    return routechange.sweep_recovered(env.con, run(env))


def recovered(env):
    return [json.loads(e["payload_json"]) for e in events(env, RECOVERED)]


def expire(env, d, when=PAST):
    key = routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])
    with db.transaction(env.con):
        env.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?", (when, key))


def failed_route(env, text="error: unknown model"):
    env.dispatch("T1")
    (d,) = env.dispatches()
    fail(env, d, text)
    return d


def review_fallback(env, before, after="codex/gpt-6-luna@low"):
    with db.transaction(env.con):
        state.record_route_change(env.con, run(env), "T1", before, after, kind="reviewer-fallback", reason="quota",
                                  actor="office", stage="code_reviewer", extra={"notice": "review", "gate": "G1"})


def probe_pass(env, route, probed_at):
    harness, rest = route.split("/", 1)
    model, effort = rest.split("@")
    with db.transaction(env.con):
        env.con.execute("INSERT INTO route_probes(key, harness, invocation_model_id, effort, result, probed_at) "
                        "VALUES(?,?,?,?,?,?)", (f"probe:{route}:{probed_at}", harness, model, effort, "pass", probed_at))


# ------------------------------------------------------------------ recovery and dedupe

def test_a_live_demotion_records_nothing_and_a_lapsed_one_records_once(plain):
    d = failed_route(plain)
    (failed,) = events(plain, "route.failed")
    assert sweep(plain) == []  # the cooldown is live: routing still rejects the route

    expire(plain, d)
    [seq] = sweep(plain)
    assert sweep(plain) == []  # one per episode, however often a tick or status sweeps

    [rec] = recovered(plain)
    assert rec == {"route": d["triple"], "harness": d["harness"], "model": d["invocation_model_id"],
                   "effort": d["effort"], "basis": "demotion lapsed", "episode": failed["seq"],
                   "evidence": "route.failed"}
    assert [e["seq"] for e in events(plain, RECOVERED)] == [seq]
    assert f"preferred route {d['triple']} is available again" in events(plain, RECOVERED)[0]["summary"]


def test_a_new_failure_after_a_recovery_is_a_new_episode(plain):
    d = failed_route(plain)
    expire(plain, d)
    assert len(sweep(plain)) == 1

    # A second failure of the same route re-arms its cooldown: the route is down again.
    with db.transaction(plain.con):
        state.emit(plain.con, run(plain), "route.failed", "again", task_id="T1",
                   payload={"route": d["triple"], "harness": d["harness"], "model": d["invocation_model_id"],
                            "effort": d["effort"], "reason": "usage_limit", "demoted_until": FUTURE})
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?",
                          (FUTURE, routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])))
    assert sweep(plain) == []

    expire(plain, d)
    assert len(sweep(plain)) == 1
    first, second = [e["seq"] for e in events(plain, "route.failed")]
    assert [r["episode"] for r in recovered(plain)] == [first, second]


def test_a_review_that_left_its_route_recovers_only_on_a_passing_reprobe_after_it(cold):
    route = "codex/gpt-6.1-sol@high"
    review_fallback(cold, route)
    (evidence,) = events(cold, "route.changed")
    assert sweep(cold) == []  # no re-probe yet

    probe_pass(cold, route, PAST)  # a pass older than the evidence is old news
    assert sweep(cold) == []

    probe_pass(cold, route, FUTURE)
    [seq] = sweep(cold)
    [rec] = recovered(cold)
    assert rec["route"] == route and rec["basis"] == "re-probe passed" and rec["episode"] == evidence["seq"]
    assert rec["evidence"] == "route.changed" and seq > evidence["seq"]


def test_a_review_route_with_a_live_demotion_is_not_recovered_by_a_pass(cold):
    route = "codex/gpt-6.1-sol@high"
    review_fallback(cold, route)
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                         (routing.demotion_key("codex", "gpt-6.1-sol", "high"), FUTURE, PAST))
    probe_pass(cold, route, FUTURE)
    assert sweep(cold) == []


def test_a_routine_review_that_stayed_on_its_route_is_not_evidence(cold):
    with db.transaction(cold.con):
        state.record_route_change(cold.con, run(cold), "T1", "codex/gpt-6.1-sol@high", "codex/gpt-6.1-sol@high",
                                  kind="reviewer-fallback", reason="quota", actor="office", extra={"gate": "G1"})
    probe_pass(cold, "codex/gpt-6.1-sol@high", FUTURE)
    assert sweep(cold) == []  # no notice=review: not a route evidence event


def test_a_terminal_run_records_no_recovery(plain):
    d = failed_route(plain)
    expire(plain, d)
    closed = {**run(plain), "phase": state.TERMINAL_PHASES[0]}
    assert routechange.sweep_recovered(plain.con, closed) == []
    assert recovered(plain) == []


# ------------------------------------------------------------------ in-flight work and re-eligibility

def test_a_recovery_touches_no_in_flight_dispatch_task_or_route(plain):
    d = failed_route(plain)
    plain.dispatch("T2")  # in flight while the route is still demoted
    before = (plain.dispatches(), [dict(r) for r in plain.con.execute("SELECT * FROM tasks ORDER BY id")])
    expire(plain, d)

    assert len(sweep(plain)) == 1
    after = (plain.dispatches(), [dict(r) for r in plain.con.execute("SELECT * FROM tasks ORDER BY id")])
    assert after == before
    assert events(plain, "route.changed") == [] and plain.con.execute("SELECT count(*) FROM amendments").fetchone()[0] == 0


def test_the_next_dispatch_considers_the_recovered_route_under_normal_ranking_without_an_amend(plain):
    d = failed_route(plain)
    assert plain.decide(role="executor", exact=d["triple"])["status"] != "selected"
    expire(plain, d)
    sweep(plain)

    again = plain.decide(role="executor", exact=d["triple"])
    assert again["status"] == "selected" and again["selected"] == d["triple"]
    assert events(plain, "route.changed") == [] and plain.con.execute("SELECT count(*) FROM amendments").fetchone()[0] == 0


# ------------------------------------------------------------------ the status line

def test_status_names_the_recovered_route_until_a_dispatch_uses_or_passes_over_it(plain, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    expire(plain, d)
    sweep(plain)
    line = f"preferred route {d['triple']} is available again (demotion lapsed); the next review, rerun or dispatch " \
           "ranks it normally"
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]
    assert line in guide.status(plain.con, run(plain)).lines

    plain.dispatch("T2")  # the next dispatch ran routing: it used or passed over the route
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines


def test_a_later_recovery_of_the_same_route_shows_again_after_a_dispatch(plain):
    d = failed_route(plain)
    expire(plain, d)
    sweep(plain)
    plain.dispatch("T2")
    assert routechange.recovery_lines(plain.con, run(plain)) == []

    state.emit(plain.con, run(plain), "route.failed", "again", task_id="T1",
               payload={"route": d["triple"], "harness": d["harness"], "model": d["invocation_model_id"],
                        "effort": d["effort"], "reason": "auth"})
    expire(plain, d)
    sweep(plain)
    assert len(routechange.recovery_lines(plain.con, run(plain))) == 1


def test_route_recovered_wakes_the_root(plain):
    assert RECOVERED in root_wake.ACTIONABLE
