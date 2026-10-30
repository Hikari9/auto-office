"""#921: a route a run fell back from or avoided is recorded as available again, once per episode.

Unit tier. The same isolated Office home and scripted fake harness as the #845 suite: a real runs.db and the real
dispatch, routing and gates modules. No model is called.
"""
import json
import sqlite3

from test_discovery_dispatch import Cold, cold, launched  # noqa: F401 (cold is a fixture)
from test_issue_845_user_allow import events, fail, plain  # noqa: F401 (plain is a fixture)

from office import context_snapshot, db, gates, guide, paths, root_wake, route_probe, routechange, routing, state
from office.util import now_iso, parse_iso

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
RECOVERED = routechange.RECOVERED_KIND
CODEX = "codex@1/gpt-6.1-sol@high"  # a candidate id: the form a review route change records


def run(env):
    return state.get_run(env.con, "run-A")


def sweep(env):
    return routechange.sweep_recovered(env.con, run(env))


def recovered(env):
    return [json.loads(e["payload_json"]) for e in events(env, RECOVERED)]


def expire(env, d, when=None):
    """Lapse the route's demotion. Default: now, after the failed dispatch started, as a real 30-minute cooldown does."""
    key = routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])
    with db.transaction(env.con):
        env.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?", (when or now_iso(), key))


def failed_route(env, text="error: unknown model"):
    env.dispatch("T1")
    (d,) = env.dispatches()
    fail(env, d, text)
    return d


def review_fallback(env, before, after="codex@1/gpt-6-luna@low"):
    with db.transaction(env.con):
        gates.review_route_notice(env.con, run(env), {"id": "G1", "task_id": "T1"}, "code_reviewer", before, after,
                                  "quota")


def probe_pass(env, route, probed_at, invocation=None):
    m = routechange._CANDIDATE.fullmatch(route)
    with db.transaction(env.con):
        env.con.execute("INSERT INTO route_probes(key, harness, invocation_model_id, effort, result, probed_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (f"probe:{route}:{probed_at}", m["harness"], invocation or m["model"], m["effort"], "pass",
                         probed_at))


def review_dispatch(env, route, invocation):
    """The failed reviewer dispatch a review route change names; it records the invocation model the route ran as."""
    with db.transaction(env.con):
        env.con.execute("INSERT INTO dispatches(id, run_id, role, triple, invocation_model_id, started_at) "
                        "VALUES(?,?,?,?,?,?)", (f"rd-{route}", "run-A", "code_reviewer", route, invocation, PAST))


def cooldown(env, route, when):
    with db.transaction(env.con):
        env.con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                        (route, when, now_iso()))


def line_for(route, basis):
    return f"preferred route {route} is available again ({basis}); the next review, rerun or dispatch ranks it normally"


# ------------------------------------------------------------------ recovery and dedupe

def test_a_live_demotion_records_nothing_and_a_lapsed_one_records_once(plain):
    d = failed_route(plain)
    (failed,) = events(plain, "route.failed")
    assert sweep(plain) == []  # the cooldown is live: routing still rejects the route

    expire(plain, d)
    [seq] = sweep(plain)
    assert sweep(plain) == []  # one per episode, however often a tick or status sweeps

    [rec] = recovered(plain)
    stored = plain.con.execute("SELECT expires_at FROM route_cooldowns WHERE route=?",
                               (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),)).fetchone()[0]
    assert parse_iso(rec.pop("available_at")) == parse_iso(stored)  # the instant the demotion lapsed, not the sweep time
    assert rec == {"route": d["triple"], "harness": d["harness"], "model": d["invocation_model_id"],
                   "effort": d["effort"], "role": "executor", "basis": "demotion lapsed",
                   "episode": failed["seq"], "evidence": "route.failed"}
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
    review_fallback(cold, CODEX)
    (evidence,) = events(cold, "route.changed")
    assert sweep(cold) == []  # no re-probe yet

    probe_pass(cold, CODEX, PAST)  # a pass older than the evidence is old news
    assert sweep(cold) == []

    probe_pass(cold, CODEX, FUTURE)
    [seq] = sweep(cold)
    [rec] = recovered(cold)
    assert rec["route"] == CODEX and rec["basis"] == "re-probe passed" and rec["episode"] == evidence["seq"]
    assert rec["evidence"] == "route.changed" and rec["role"] == "code_reviewer" and seq > evidence["seq"]


ALIAS = "claude@1/sonnet@high"  # model_id sonnet runs as invocation claude-sonnet-5


def test_an_aliased_review_route_recovers_under_its_invocation_model(cold):
    review_dispatch(cold, ALIAS, "claude-sonnet-5")
    review_fallback(cold, ALIAS)
    probe_pass(cold, ALIAS, FUTURE, invocation="claude-sonnet-5")
    [seq] = sweep(cold)
    assert recovered(cold)[0]["basis"] == "re-probe passed" and recovered(cold)[0]["model"] == "claude-sonnet-5"


def canonical_invocation(alias_model):
    """The catalog's canonical invocation id for a claude alias: the id routing and the probes key on."""
    return route_probe.catalog_row({"harness": "claude", "model_id": alias_model, "effort": "high"})["invocation_model_id"]


def test_an_aliased_skipped_primary_with_no_dispatch_row_resolves_through_the_catalog(cold):
    canonical = canonical_invocation("sonnet")
    assert canonical != "sonnet"  # the alias is a real redirection, not the id itself
    review_fallback(cold, ALIAS)  # a skipped primary: the review notice exists, no dispatch of the route
    assert cold.con.execute("SELECT count(*) FROM dispatches WHERE triple=?", (ALIAS,)).fetchone()[0] == 0
    probe_pass(cold, ALIAS, FUTURE, invocation=canonical)
    [seq] = sweep(cold)
    assert recovered(cold)[0]["model"] == canonical and recovered(cold)[0]["basis"] == "re-probe passed"


def test_an_aliased_skipped_primary_with_a_live_canonical_cooldown_is_not_recovered(cold):
    canonical = canonical_invocation("sonnet")
    review_fallback(cold, ALIAS)
    cooldown(cold, routing.cooldown_key("claude", canonical), FUTURE)
    probe_pass(cold, ALIAS, FUTURE, invocation=canonical)
    assert sweep(cold) == []


def test_an_aliased_review_route_with_a_live_invocation_cooldown_is_not_recovered(cold):
    review_dispatch(cold, ALIAS, "claude-sonnet-5")
    review_fallback(cold, ALIAS)
    cooldown(cold, routing.cooldown_key("claude", "claude-sonnet-5"), FUTURE)
    probe_pass(cold, ALIAS, FUTURE, invocation="claude-sonnet-5")
    assert sweep(cold) == []


def test_a_review_route_with_a_live_demotion_is_not_recovered_by_a_pass(cold):
    review_fallback(cold, CODEX)
    cooldown(cold, routing.demotion_key("codex", "gpt-6.1-sol", "high"), FUTURE)
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == []


def test_a_review_route_with_a_live_no_effort_cooldown_is_not_recovered_by_a_pass(cold):
    # Routing rejects every effort of an invocation while its no-effort cooldown is live.
    review_fallback(cold, CODEX)
    cooldown(cold, routing.cooldown_key("codex", "gpt-6.1-sol"), FUTURE)
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == []


def test_a_passing_probe_with_no_probed_at_neither_crashes_nor_recovers(cold):
    review_fallback(cold, CODEX)
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO route_probes(key, harness, invocation_model_id, effort, result, probed_at) "
                         "VALUES('probe:null','codex','gpt-6.1-sol','high','pass',NULL)")
    assert sweep(cold) == []


def test_a_review_the_user_re_pinned_is_never_recovered(cold):
    with db.transaction(cold.con):
        state.record_route_change(cold.con, run(cold), "T1", CODEX, "codex@1/gpt-6-luna@low", kind="repin",
                                  reason="the user re-pinned the plan reviewer", actor="user", stage="code_reviewer",
                                  extra={"notice": "review", "gate": "G1"})
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == []  # the user's own decision is not a fallback the run should undo


def test_a_stale_line_is_not_shown_after_a_successful_dispatch_deleted_the_cooldown(plain):
    d = failed_route(plain)
    # The demotion lapsed, a dispatch of the route then succeeded and deleted its cooldown rows, and no sweep ran before.
    with db.transaction(plain.con):
        plain.con.execute("UPDATE events SET payload_json=json_set(payload_json,'$.demoted_until',?) "
                          "WHERE run_id='run-A' AND kind='route.failed'", (PAST,))
        plain.con.execute("DELETE FROM route_cooldowns WHERE route=?",
                          (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),))
        plain.con.execute("INSERT INTO dispatches(id, run_id, role, triple, invocation_model_id, started_at) "
                          "VALUES('ok','run-A','executor',?,?,?)", (d["triple"], d["invocation_model_id"], now_iso()))
    assert len(sweep(plain)) == 1
    assert routechange.recovery_lines(plain.con, run(plain)) == []


def test_a_success_that_deletes_a_live_demotion_does_not_recover_it_before_its_deadline(plain):
    d = failed_route(plain)  # demoted until 30 minutes from now
    with db.transaction(plain.con):  # a dispatch of the route succeeded and deleted its cooldown rows
        plain.con.execute("DELETE FROM route_cooldowns WHERE route=?",
                          (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),))
    assert sweep(plain) == []
    with db.transaction(plain.con):  # the deadline passes: the demotion has lapsed
        plain.con.execute("UPDATE events SET payload_json=json_set(payload_json,'$.demoted_until',?) "
                          "WHERE run_id='run-A' AND kind='route.failed'", (PAST,))
    assert len(sweep(plain)) == 1
    assert [r["basis"] for r in recovered(plain)] == ["demotion lapsed"]


def test_a_routine_review_that_stayed_on_its_route_is_not_evidence(cold):
    with db.transaction(cold.con):
        state.record_route_change(cold.con, run(cold), "T1", CODEX, CODEX, kind="reviewer-fallback", reason="quota",
                                  actor="office", stage="code_reviewer", extra={"gate": "G1"})
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == []  # no notice=review: not route evidence


def test_a_declared_route_failure_is_never_recovered(plain):
    d = {"route": "codex@1/gpt-6.1-sol@medium", "harness": "codex", "model": "gpt-6.1-sol", "effort": "medium"}
    with db.transaction(plain.con):
        state.emit(plain.con, run(plain), "route.failed", "declared route failed", task_id="T1",
                   payload={**d, "reason": "unsupported-model-effort", "declared": True, "demoted_until": PAST})
    assert sweep(plain) == []  # no cooldown is live, but the user declared this route: it is not swapped or recovered



def test_a_terminal_run_records_no_recovery(plain):
    d = failed_route(plain)
    expire(plain, d)
    closed = {**run(plain), "phase": state.TERMINAL_PHASES[0]}
    assert routechange.sweep_recovered(plain.con, closed) == []
    assert recovered(plain) == []


def test_a_live_no_effort_cooldown_blocks_a_lapsed_demotion_until_it_lapses(plain):
    d = failed_route(plain)
    cooldown(plain, routing.cooldown_key(d["harness"], d["invocation_model_id"]), FUTURE)
    expire(plain, d)
    assert sweep(plain) == []
    with db.transaction(plain.con):
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?",
                          (PAST, routing.cooldown_key(d["harness"], d["invocation_model_id"])))
    assert len(sweep(plain)) == 1


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
    assert len(recovered(plain)) == 1  # the recovery is recorded before routing reads it, so the ranking below is the real one

    again = plain.decide(role="executor", exact=d["triple"])
    assert again["status"] == "selected" and again["selected"] == d["triple"]
    assert events(plain, "route.changed") == [] and plain.con.execute("SELECT count(*) FROM amendments").fetchone()[0] == 0


# ------------------------------------------------------------------ the status line and the wake

def test_status_names_the_recovered_route_until_a_dispatch_uses_or_passes_over_it(plain, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    expire(plain, d)
    line = line_for(d["triple"], "demotion lapsed")

    assert line in guide.status(plain.con, run(plain)).lines  # status records the recovery itself
    guide.status(plain.con, run(plain))
    assert len(recovered(plain)) == 1  # a second status does not re-record
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]

    plain.dispatch("T2")  # the next dispatch ran routing: it used or passed over the route
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines


def test_a_later_recovery_of_the_same_route_shows_again_after_a_dispatch(plain):
    d = failed_route(plain)
    expire(plain, d)
    sweep(plain)
    plain.dispatch("T2")
    assert routechange.recovery_lines(plain.con, run(plain)) == []

    with db.transaction(plain.con):
        state.emit(plain.con, run(plain), "route.failed", "again", task_id="T1",
                   payload={"route": d["triple"], "harness": d["harness"], "model": d["invocation_model_id"],
                            "effort": d["effort"], "reason": "auth"})
    expire(plain, d, when=now_iso())  # available after the dispatch above
    sweep(plain)
    assert len(routechange.recovery_lines(plain.con, run(plain))) == 1


def test_a_dispatch_between_the_lapse_and_the_first_sweep_clears_the_line(plain, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    expire(plain, d)  # available now
    plain.dispatch("T2")  # routing used the route before any sweep recorded the recovery
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line_for(d["triple"], "demotion lapsed") not in guide.status(plain.con, run(plain)).lines


def test_an_unrelated_dispatch_does_not_clear_the_line(plain):
    d = failed_route(plain)
    expire(plain, d)
    sweep(plain)
    with db.transaction(plain.con):  # a reviewer dispatch on another route
        plain.con.execute("INSERT INTO dispatches(id, run_id, role, triple, started_at) VALUES('dx','run-A','code_reviewer',"
                          "'codex@1/gpt-6-luna@low',?)", (now_iso(),))
        # an executor dispatch pinned to a declared route skips the ranking, so it passes nothing over
        plain.con.execute("INSERT INTO dispatches(id, run_id, role, triple, started_at, override_json) VALUES('dy','run-A',"
                          "'executor','codex@1/gpt-6-luna@low',?,'{\"x\":1}')", (now_iso(),))
    assert routechange.recovery_lines(plain.con, run(plain)) == [line_for(d["triple"], "demotion lapsed")]


def test_a_gate_on_the_recovered_route_clears_the_line(plain):
    d = failed_route(plain)
    expire(plain, d)
    sweep(plain)
    with db.transaction(plain.con):
        plain.con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, route, created_at) "
                          "VALUES('GR','run-A','T1','code_review','k','queued',?,?)", (d["triple"], now_iso()))
    assert routechange.recovery_lines(plain.con, run(plain)) == []


def test_status_and_context_name_the_recovered_route_the_same_way(plain, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    expire(plain, d)
    line = line_for(d["triple"], "demotion lapsed")
    assert line in context_snapshot.context(plain.con, run(plain)).lines  # context records the recovery itself
    assert line in guide.status(plain.con, run(plain)).lines

    plain.dispatch("T2")
    assert line not in context_snapshot.context(plain.con, run(plain)).lines


def test_route_recovered_wakes_the_root(plain):
    assert RECOVERED in root_wake.ACTIONABLE


def test_reconcile_wakes_the_root_for_a_recovery_before_any_status_call(plain):
    d = failed_route(plain)
    expire(plain, d)
    with db.transaction(plain.con):
        plain.con.execute("INSERT INTO root_wake_bindings(run_id, harness, session_id, pane_id, agent_name, enabled, "
                          "generation, registered_at, last_event_seq) VALUES('run-A','claude','s','p','root-claude',1,1,?,0)",
                          (now_iso(),))
    root_wake.reconcile(plain.con, run(plain))
    rows = plain.con.execute("SELECT kind, state FROM root_wake_intents WHERE run_id='run-A' AND kind=?",
                             (RECOVERED,)).fetchall()
    assert [tuple(r) for r in rows] == [(RECOVERED, "pending")]


def test_a_sweep_with_nothing_to_record_takes_no_writer_lock(plain, monkeypatch):
    # Status, context and wake reads sweep on every call. With another writer holding the lock, a sweep that records
    # nothing must still return, not wait out the transaction retries.
    failed_route(plain)  # a live demotion: nothing is recoverable
    plain.con.execute("PRAGMA busy_timeout=0")
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    holder = sqlite3.connect(str(paths.runs_db()), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert sweep(plain) == []
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_review_route_with_no_effort_recovers_on_a_pass_whose_probe_has_no_effort(cold):
    none_route = "codex@1/gpt-6.1-sol@None"  # candidate_id writes a missing effort as the text None
    review_fallback(cold, none_route)
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO route_probes(key, harness, invocation_model_id, effort, result, probed_at) "
                         "VALUES('probe:no-effort','codex','gpt-6.1-sol',NULL,'pass',?)", (FUTURE,))
    assert len(sweep(cold)) == 1
    [rec] = recovered(cold)
    assert rec["route"] == none_route and rec["effort"] is None and rec["basis"] == "re-probe passed"


def test_a_later_review_that_leaves_a_recovered_route_clears_its_line(cold):
    review_fallback(cold, CODEX)
    probe_pass(cold, CODEX, now_iso())
    sweep(cold)
    assert routechange.recovery_lines(cold.con, run(cold)) == [line_for(CODEX, "re-probe passed")]

    review_fallback(cold, CODEX, after="codex@1/gpt-6-luna@low")  # routing passed the recovered route over again
    assert routechange.recovery_lines(cold.con, run(cold)) == []
