"""#921: a route a run fell back from or avoided is recorded as available again, once per episode.

Unit tier. The same isolated Office home and scripted fake harness as the #845 suite: a real runs.db and the real
dispatch, routing and gates modules. No model is called.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from test_discovery_dispatch import Cold, cold, launched  # noqa: F401 (cold is a fixture)
from test_adaptive_dispatch import PLANNED, _approve, _quota
from test_adaptive_routing import cand, req
from test_issue_845_user_allow import events, fail, plain  # noqa: F401 (plain is a fixture)

from office import adapters, candidates, context_snapshot, db, gates, guide, paths, route_policy, root_wake, route_probe, routechange, routing, state
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
    """Lapse the route's demotion (and an auth failure's harness demotion). Default: now, after the failed dispatch
    started, as a real 30-minute cooldown does."""
    keys = (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),
            routing.harness_demotion_key(d["harness"]))
    with db.transaction(env.con):
        env.con.execute(f"UPDATE route_cooldowns SET expires_at=? WHERE route IN ({','.join('?' * len(keys))})",
                        (when or now_iso(), *keys))


def failed_route(env, text="not logged in"):  # an auth failure: a quota or auth route failure, recoverable
    return failed_with(env, text, "nonzero")


def failed_with(env, text, classification):
    env.dispatch("T1")
    (d,) = [x for x in env.dispatches() if x["task_id"] == "T1"]
    fail(env, d, text, classification)
    return d


def review_fallback(env, before, after="codex@1/gpt-6-luna@low", skip_class="quota-auth"):
    with db.transaction(env.con):
        gates.review_route_notice(env.con, run(env), {"id": "G1", "task_id": "T1"}, "code_reviewer", before, after,
                                  "quota", skip_class=skip_class)


def put_probe(env, route, result, probed_at, reason=None, *, invocation=None, harness_version=None, profile=None):
    """A probe row keyed as routing keys it: the route's current harness version and adapter and the default launch
    profile, unless a test names another. Like a reservation, the write replaces the row for its key."""
    m = routing.CANDIDATE_ID_RE.fullmatch(route)
    adapter = adapters.load_all()[m["harness"]]
    effort = None if m["effort"] == "None" else m["effort"]
    cand = {"harness": m["harness"], "invocation_model_id": invocation or m["model"], "effort": effort,
            "harness_version": harness_version or adapters.route_version(m["harness"], adapter)}
    fp = route_probe.fingerprint(cand, adapter, profile or route_probe.PROFILE)
    with db.transaction(env.con):
        env.con.execute("INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                        "invocation_model_id, effort, result, reason_class, probed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (route_probe.key(cand, adapter, fp["profile"]), fp["harness"], fp["harness_version"],
                         fp["adapter_hash"], fp["profile"], fp["invocation_model_id"], effort, result, reason, probed_at))


def catalog_fields(monkeypatch, d, **fields):
    """The catalog row routing reads for a failed route, with `fields` set. Other routes keep the real catalog."""
    real = route_probe.catalog_row

    def row(cand):
        if (cand.get("harness"), cand.get("model_id")) == (d["harness"], d["invocation_model_id"]):
            return {**(real(cand) or {}), **fields}
        return real(cand)
    monkeypatch.setattr(route_probe, "catalog_row", row)


def probe_pass(env, route, probed_at, invocation=None):
    put_probe(env, route, "pass", probed_at, invocation=invocation)


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


def test_an_unsupported_model_demotion_never_records_a_recovery(plain):
    """Only quota or auth failures are recovered. A lapsed demotion for an unsupported model stays unrecorded: its
    exact probe still fails, so routing keeps rejecting the route."""
    d = failed_route(plain, "error: unknown model")
    expire(plain, d)
    assert sweep(plain) == []
    assert recovered(plain) == []


def test_a_closed_run_records_no_recovery_even_when_the_caller_read_it_open(plain):
    d = failed_route(plain)
    expire(plain, d)
    stale = run(plain)  # read before the run closed
    with db.transaction(plain.con):
        plain.con.execute("UPDATE runs SET phase='closed' WHERE id='run-A'")
    assert routechange.sweep_recovered(plain.con, stale) == []
    assert recovered(plain) == []


def test_a_review_skip_whose_rate_limit_lapsed_recovers_without_a_reprobe(cold):
    cooldown(cold, routing.cooldown_key("codex", "gpt-6.1-sol"), FUTURE)  # the rate limit that skipped the route
    review_fallback(cold, CODEX)
    (evidence,) = events(cold, "route.changed")
    assert sweep(cold) == []  # still live
    with db.transaction(cold.con):
        cold.con.execute("UPDATE route_cooldowns SET expires_at=?", (now_iso(),))
    [seq] = sweep(cold)
    [rec] = recovered(cold)
    assert rec["route"] == CODEX and rec["basis"] == "cooldown lapsed" and rec["episode"] == evidence["seq"]


def test_a_review_skip_whose_rate_limit_lapsed_is_not_recovered_over_a_fresh_failed_probe(cold):
    cooldown(cold, routing.cooldown_key("codex", "gpt-6.1-sol"), FUTURE)
    review_fallback(cold, CODEX)
    put_probe(cold, CODEX, "fail", now_iso(), "auth-quota-blocked")  # routing rejects the route on this fresh probe
    with db.transaction(cold.con):
        cold.con.execute("UPDATE route_cooldowns SET expires_at=?", (now_iso(),))
    assert sweep(cold) == []
    assert recovered(cold) == []


def test_a_transport_failure_never_records_a_recovery(plain):
    d = failed_with(plain, "socket hang up", "nonzero")
    expire(plain, d)
    assert sweep(plain) == [] and recovered(plain) == []


def test_a_timeout_failure_never_records_a_recovery(plain):
    d = failed_with(plain, "", "timeout")
    expire(plain, d)
    assert sweep(plain) == [] and recovered(plain) == []


def test_a_review_skip_whose_rate_limit_lapsed_before_the_notice_does_not_recover(cold):
    cooldown(cold, routing.cooldown_key("codex", "gpt-6.1-sol"), PAST)  # lapsed before the skip: not its reason
    review_fallback(cold, CODEX)
    assert sweep(cold) == []


def test_a_review_skip_is_not_recovered_by_a_lapsed_demotion_of_its_route(cold):
    """A demotion row is not the rate limit that skipped a review route, so its lapse alone recovers nothing."""
    review_fallback(cold, CODEX)
    cooldown(cold, routing.demotion_key("codex", "gpt-6.1-sol", "high"), FUTURE)
    with db.transaction(cold.con):
        cold.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?",
                         (now_iso(), routing.demotion_key("codex", "gpt-6.1-sol", "high")))
    assert sweep(cold) == []  # no re-probe passed


def test_a_rate_limit_refreshed_after_the_skip_does_not_recover_it(cold):
    key = routing.cooldown_key("codex", "gpt-6.1-sol")
    cooldown(cold, key, FUTURE)
    review_fallback(cold, CODEX)
    with db.transaction(cold.con):  # a later limit hit refreshes the row: whether it was live at the skip is unknown
        cold.con.execute("UPDATE route_cooldowns SET expires_at=?, updated_at=? WHERE route=?", (now_iso(), now_iso(), key))
    assert sweep(cold) == []


def test_a_lapsed_demotion_does_not_hide_a_rate_limit_that_lapsed_before_the_notice(cold):
    """The rate limit's own row decides: a lapsed demotion beside it recovers nothing, and a rate limit that lapsed
    before the skip does not recover it either."""
    cooldown(cold, routing.cooldown_key("codex", "gpt-6.1-sol"), PAST)
    cooldown(cold, routing.demotion_key("codex", "gpt-6.1-sol", "high"), PAST)
    review_fallback(cold, CODEX)
    assert sweep(cold) == []


def test_a_run_gone_since_the_caller_read_it_records_nothing(plain, monkeypatch):
    d = failed_route(plain)
    expire(plain, d)
    stale = run(plain)
    monkeypatch.setattr(state, "get_run", lambda con, run_id: None)  # the run row is gone at the writer lock
    assert routechange.sweep_recovered(plain.con, stale) == []
    monkeypatch.undo()
    assert recovered(plain) == []


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


def claude_reads_a_version(monkeypatch):
    """The fixture's claude reports no harness version, so routing cannot key a probe on it. Give it one for the alias tests."""
    real = adapters.harness_version
    monkeypatch.setattr(adapters, "harness_version", lambda adapter: "2.1.0" if adapter.get("id") == "claude" else real(adapter))


def test_an_aliased_review_route_recovers_under_its_invocation_model(cold, monkeypatch):
    claude_reads_a_version(monkeypatch)
    review_dispatch(cold, ALIAS, "claude-sonnet-5")
    review_fallback(cold, ALIAS)
    probe_pass(cold, ALIAS, FUTURE, invocation="claude-sonnet-5")
    [seq] = sweep(cold)
    assert recovered(cold)[0]["basis"] == "re-probe passed" and recovered(cold)[0]["model"] == "claude-sonnet-5"


def canonical_invocation(alias_model):
    """The catalog's canonical invocation id for a claude alias: the id routing and the probes key on."""
    return route_probe.catalog_row({"harness": "claude", "model_id": alias_model, "effort": "high"})["invocation_model_id"]


def test_an_aliased_skipped_primary_with_no_dispatch_row_resolves_through_the_catalog(cold, monkeypatch):
    claude_reads_a_version(monkeypatch)
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
    put_probe(cold, CODEX, "pass", None)
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
        plain.con.execute("DELETE FROM route_cooldowns WHERE route IN (?,?)",
                          (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),
                           routing.harness_demotion_key(d["harness"])))
        plain.con.execute("INSERT INTO dispatches(id, run_id, role, triple, invocation_model_id, started_at) "
                          "VALUES('ok','run-A','executor',?,?,?)", (d["triple"], d["invocation_model_id"], now_iso()))
    assert len(sweep(plain)) == 1
    assert routechange.recovery_lines(plain.con, run(plain)) == []


def test_a_success_that_deletes_a_live_demotion_does_not_recover_it_before_its_deadline(plain):
    d = failed_route(plain)  # demoted until 30 minutes from now
    with db.transaction(plain.con):  # a dispatch of the route succeeded and deleted its cooldown rows
        plain.con.execute("DELETE FROM route_cooldowns WHERE route IN (?,?)",
                          (routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"]),
                           routing.harness_demotion_key(d["harness"])))
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
    for reason in ("auth", "unsupported-model-effort"):  # the second is no evidence at all: the SQL filter drops it
        with db.transaction(plain.con):
            state.emit(plain.con, run(plain), "route.failed", "declared route failed", task_id="T1",
                       payload={**d, "reason": reason, "declared": True, "demoted_until": PAST})
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
    plain.dispatch("T2")  # in flight while the route is still demoted
    d = failed_route(plain)
    expire(plain, d)
    before = (plain.dispatches(), [dict(r) for r in plain.con.execute("SELECT * FROM tasks ORDER BY id")])

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


def test_an_in_flight_dispatch_that_fails_after_the_recovery_supersedes_its_line(plain, monkeypatch):
    """A dispatch started before the recovery was never considered by routing, so it clears nothing. When it then fails
    on the route, its failure installs a live cooldown: the route is down again and the line must go."""
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    plain.dispatch("T2")  # in flight on the route while it is healthy
    (inflight,) = [x for x in plain.dispatches() if x["task_id"] == "T2"]
    d = failed_route(plain)
    assert inflight["triple"] == d["triple"]
    expire(plain, d)
    assert len(sweep(plain)) == 1
    line = line_for(d["triple"], "demotion lapsed")
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]

    fail(plain, inflight, "not logged in")  # the in-flight dispatch fails on the route after the recovery
    assert sweep(plain) == []  # its new cooldown is live
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines

    expire(plain, inflight)  # the new failure lapses: a new episode, recovered once more
    assert len(sweep(plain)) == 1
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]


def test_an_in_flight_timeout_after_the_recovery_hides_its_line_while_its_cooldown_is_live(plain, monkeypatch):
    """A timeout is not quota or auth evidence, but it still demotes the route: the line goes while that cooldown is live."""
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    plain.dispatch("T2")
    (inflight,) = [x for x in plain.dispatches() if x["task_id"] == "T2"]
    d = failed_route(plain)
    expire(plain, d)
    assert len(sweep(plain)) == 1
    line = line_for(d["triple"], "demotion lapsed")
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]

    fail(plain, inflight, "", "timeout")  # the in-flight dispatch times out on the route after the recovery
    assert json.loads(events(plain, "route.failed")[-1]["payload_json"])["reason"] == "timeout"
    assert sweep(plain) == []
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines

    expire(plain, inflight)  # the timeout's demotion lapses: the route is available again
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]


def test_a_cooldown_with_no_route_event_supersedes_the_line(plain, monkeypatch):
    """A quota wall sets its cooldown at the dispatch's end with no route event. Routing rejects the route on it all the same."""
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    expire(plain, d)
    assert len(sweep(plain)) == 1
    line = line_for(d["triple"], "demotion lapsed")
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]

    with db.transaction(plain.con):
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?",
                          (FUTURE, routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])))
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines


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
    put_probe(cold, none_route, "pass", FUTURE)
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


def probe_at(env, d, result, reason=None, at=None, **fingerprint):
    """A probe of the failed route's exact fingerprint, recorded at `at` (default now)."""
    put_probe(env, d["triple"], result, at or now_iso(), reason, invocation=d["invocation_model_id"], **fingerprint)


def test_a_fresh_failed_probe_keeps_a_lapsed_route_down_until_a_later_pass(plain, monkeypatch):
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    probe_at(plain, d, "fail", "auth-quota-blocked")  # routing still rejects the route on this probe
    expire(plain, d)
    assert sweep(plain) == []
    probe_at(plain, d, "pass", at=FUTURE)
    assert len(sweep(plain)) == 1


def test_a_stale_failed_probe_does_not_keep_a_lapsed_route_down(plain):
    d = failed_route(plain)
    probe_at(plain, d, "fail", "auth-quota-blocked", at=PAST)  # past the temporary TTL: routing no longer reads it
    expire(plain, d)
    assert len(sweep(plain)) == 1


def test_a_newer_failed_probe_supersedes_an_older_pass_for_a_review_route(cold):
    review_fallback(cold, CODEX)
    probe_pass(cold, CODEX, FUTURE)
    put_probe(cold, CODEX, "fail", "2999-06-01T00:00:00+00:00", "auth-quota-blocked")  # the same route fails its probe after the pass
    assert sweep(cold) == []


def test_a_live_harness_wide_cooldown_keeps_a_lapsed_route_down(plain):
    d = failed_route(plain)  # an auth failure also demotes every route on its harness
    with db.transaction(plain.con):  # only the route's own demotion lapses
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?",
                          (now_iso(), routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])))
    assert sweep(plain) == []
    expire(plain, d)  # the harness-wide demotion lapses too
    assert len(sweep(plain)) == 1


def test_a_live_shared_quota_pool_keeps_a_lapsed_route_down(plain):
    """OpenCode's free models share one quota wall: a live pool cooldown keeps a lapsed demotion of one of them down."""
    d = {"route": "opencode@1/some-free@high", "harness": "opencode", "model": "some-free", "effort": "high"}
    with db.transaction(plain.con):
        state.emit(plain.con, run(plain), "route.failed", "free tier exhausted", task_id="T1",
                   payload={**d, "reason": "usage_limit", "demoted_until": PAST})
        plain.con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                          (routing.demotion_key("opencode", "some-free", "high"), PAST, now_iso()))
        plain.con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                          ("pool:opencode:free", FUTURE, now_iso()))
    assert sweep(plain) == []
    with db.transaction(plain.con):
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?", (PAST, "pool:opencode:free"))
    assert len(sweep(plain)) == 1


def test_a_review_change_the_user_recorded_is_never_recovered_even_with_quota_auth_evidence(cold):
    with db.transaction(cold.con):
        state.record_route_change(cold.con, run(cold), "T1", CODEX, "codex@1/gpt-6-luna@low", kind="repin",
                                  reason="the user kept the plan reviewer", actor="user", stage="code_reviewer",
                                  extra={"notice": "review", "gate": "G1", "skip_class": "quota-auth"})
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == []  # the evidence qualifies, but the user made the change: the user's decision stands


def test_a_review_route_recovers_only_on_a_pass_of_its_exact_fingerprint(cold):
    """Routing reads one exact fingerprint. A pass of another launch profile or harness version proves nothing about it."""
    review_fallback(cold, CODEX)
    put_probe(cold, CODEX, "pass", FUTURE, profile="other")
    assert sweep(cold) == []
    put_probe(cold, CODEX, "pass", FUTURE, harness_version="0.0.0-other")
    assert sweep(cold) == []
    put_probe(cold, CODEX, "pass", FUTURE)
    assert len(sweep(cold)) == 1


def test_a_same_key_reservation_overwrites_the_pass_and_leaves_no_verdict_to_recover_on(cold):
    """The reservation overwrites its exact key's row, so the pass is gone. While it is fresh routing does not use the route;
    once orphaned, the pending row is no verdict and the route is not recovered."""
    review_fallback(cold, CODEX)
    put_probe(cold, CODEX, "pass", FUTURE)
    put_probe(cold, CODEX, "pending", "2999-06-01T00:00:00+00:00")
    assert routechange.sweep_recovered(cold.con, run(cold), now=parse_iso("2999-06-01T00:00:01+00:00")) == []
    assert routechange.sweep_recovered(cold.con, run(cold), now=parse_iso("3000-01-01T00:00:00+00:00")) == []


def test_an_orphaned_pending_probe_of_another_profile_does_not_hide_a_fresh_failed_probe(plain, monkeypatch):
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    expire(plain, d)
    timeout = route_policy.DISCOVERY_DEFAULTS["probe_timeout_s"]
    failed_at = datetime.now(timezone.utc) - timedelta(seconds=timeout + 600)  # fresh: under the temporary TTL
    probe_at(plain, d, "fail", "auth-quota-blocked", at=failed_at.isoformat())
    probe_at(plain, d, "pending", None, at=(failed_at + timedelta(seconds=1)).isoformat(), profile="other")  # newer, orphaned
    assert sweep(plain) == []  # the exact fresh failed probe still speaks: routing rejects the route on it


def test_a_pass_of_another_launch_profile_or_version_does_not_hide_the_exact_fresh_failure(plain, monkeypatch):
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    probe_at(plain, d, "fail", "auth-quota-blocked")  # routing rejects the route on this exact probe
    expire(plain, d)
    probe_at(plain, d, "pass", at=FUTURE, profile="other")
    assert sweep(plain) == []
    probe_at(plain, d, "pass", at=FUTURE, harness_version="0.0.0-other")
    assert sweep(plain) == []


def test_a_fresh_failure_of_another_harness_version_does_not_keep_the_route_down(plain):
    d = failed_route(plain)
    probe_at(plain, d, "fail", "auth-quota-blocked", harness_version="0.0.0-other")  # routing never reads this version
    expire(plain, d)
    assert len(sweep(plain)) == 1


def test_a_fresh_failed_probe_after_the_recovery_hides_its_line(plain, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    expire(plain, d)
    assert len(sweep(plain)) == 1
    line = line_for(d["triple"], "demotion lapsed")
    assert routechange.recovery_lines(plain.con, run(plain)) == [line]

    probe_at(plain, d, "fail", "auth-quota-blocked")  # from now on routing rejects the route on this exact probe
    assert routechange.recovery_lines(plain.con, run(plain)) == []
    assert line not in guide.status(plain.con, run(plain)).lines


def test_a_role_allow_admits_a_discovery_route_past_its_failed_probe(plain, monkeypatch):
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    probe_at(plain, d, "fail", "auth-quota-blocked")  # a role allow admits the route: routing attaches no probe to it
    allow = route_policy.preferred_seed_policy(d["role"], [{"harness": d["harness"], "model_id": d["invocation_model_id"],
                                                             "effort": d["effort"]}])
    monkeypatch.setattr(candidates, "current_user_policies", lambda repo_root=None: [allow])
    expire(plain, d)
    assert len(sweep(plain)) == 1


def test_a_fresh_failed_probe_does_not_hold_a_dispatchable_route(plain, monkeypatch):
    """Routing reads a probe only for a discovery-eligible route. A dispatchable one is available whatever its probe."""
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=True)
    probe_at(plain, d, "fail", "auth-quota-blocked")
    expire(plain, d)
    assert len(sweep(plain)) == 1


def test_a_configured_probe_timeout_judges_an_orphaned_reservation_by_the_run_policy(plain, monkeypatch):
    d = failed_route(plain)
    catalog_fields(monkeypatch, d, dispatchable=False, discovery="eligible")
    expire(plain, d)
    with db.transaction(plain.con):  # this run's policy raises the probe timeout past the default
        plain.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'",
                          (json.dumps({"routing": {"discovery": {"probe_timeout_s": 600}}}),))
    started = datetime.now(timezone.utc) - timedelta(seconds=200)  # past the default 120 s, inside the configured 600 s
    put_probe(plain, d["triple"], "pending", started.isoformat(), invocation=d["invocation_model_id"])
    assert sweep(plain) == []  # still in flight under this run's policy: routing holds the route


def skip_decision(category=None):
    """A review decision whose preferred primary (CODEX) was rejected and whose selection chose a luna route."""
    primary = {"harness": "codex", "model_id": "gpt-6.1-sol", "effort": "high", "harness_version": "1.0.0"}
    chosen = {"harness": "codex", "model_id": "gpt-6-luna", "effort": "low", "harness_version": "1.0.0"}
    rejection = {"candidate": CODEX, "stage": 1, "reason": "trust floor"}
    if category:
        rejection.update(stage=6, category=category)
    seed = [{"model_id": "gpt-6.1-sol", "effort": "high", "harness": "codex"}]
    return {"request": {"preferred_seed": seed, "candidates": [primary, chosen]}, "rejected": [rejection]}, chosen


def test_a_trust_or_capability_skip_never_yields_route_recovered(cold):
    decision, chosen = skip_decision()
    route, why, skip_class = gates._skipped_primary(decision, chosen)
    assert (route, skip_class) == (CODEX, "other")
    with db.transaction(cold.con):
        gates.review_route_notice(cold.con, run(cold), {"id": "G1", "task_id": "T1"}, "code_reviewer", route,
                                  "codex@1/gpt-6-luna@low", why, skip_class=skip_class)
    probe_pass(cold, CODEX, FUTURE)  # the route is available again: a pass after the skip
    assert sweep(cold) == [] and recovered(cold) == []


def test_a_quota_skip_of_the_primary_recovers_on_a_passing_reprobe(cold):
    decision, chosen = skip_decision(category="rate-limited")
    route, why, skip_class = gates._skipped_primary(decision, chosen)
    assert (route, skip_class) == (CODEX, "quota-auth")
    with db.transaction(cold.con):
        gates.review_route_notice(cold.con, run(cold), {"id": "G1", "task_id": "T1"}, "code_reviewer", route,
                                  "codex@1/gpt-6-luna@low", why, skip_class=skip_class)
    probe_pass(cold, CODEX, FUTURE)
    assert len(sweep(cold)) == 1 and recovered(cold)[0]["route"] == CODEX


def skip_notice(env, route, why, skip_class, after="codex@1/gpt-6-luna@low"):
    with db.transaction(env.con):
        gates.review_route_notice(env.con, run(env), {"id": "G1", "task_id": "T1"}, "code_reviewer", route, after, why,
                                  skip_class=skip_class)


def test_a_quota_crossing_primary_is_recovered_once_a_reprobe_passes(cold):
    # Routing's own decision: the preferred primary crosses the quota reserve while a safe luna route is available.
    primary = cand("codex", "gpt-6.1-sol", "high", quota=10)
    primary["harness_version"] = "1.0.0"
    chosen = cand("codex", "gpt-6-luna", "low", quota=60, out=0.5, idx=40)
    chosen["harness_version"] = "1.0.0"
    request = req([primary, chosen], role="executor",
                  preferred_seed=[{"model_id": "gpt-6.1-sol", "effort": "high", "harness": "codex"}])
    request["policy"] = {"cost_policy": "balanced", "quota_reserve_percent": 20}
    decision = {**routing.route(request), "request": request}  # candidates.decide attaches the request the same way
    chosen = decision["candidate"]
    route, why, skip_class = gates._skipped_primary(decision, chosen)
    assert (route, skip_class) == (CODEX, "quota-auth")
    skip_notice(cold, route, why, skip_class)
    probe_pass(cold, CODEX, FUTURE)
    assert len(sweep(cold)) == 1 and recovered(cold)[0]["route"] == CODEX


def test_a_probe_failed_on_an_auth_quota_block_is_recovered_once_a_reprobe_passes(cold):
    primary = {"harness": "codex", "harness_version": "1.0.0", "model_id": "gpt-6.1-sol", "effort": "high",
               "probe": {"result": "fail", "reason_class": "auth-quota-blocked", "detail": "429"}}
    chosen = {"harness": "codex", "harness_version": "1.0.0", "model_id": "gpt-6-luna", "effort": "low"}
    rejected = []
    routing._reject_pool([primary], rejected)
    decision = {"request": {"preferred_seed": [{"model_id": "gpt-6.1-sol", "effort": "high", "harness": "codex"}],
                            "candidates": [primary, chosen]}, "rejected": rejected}
    route, why, skip_class = gates._skipped_primary(decision, chosen)
    assert (route, skip_class) == (CODEX, "quota-auth")
    skip_notice(cold, route, why, skip_class)
    probe_pass(cold, CODEX, FUTURE)
    assert len(sweep(cold)) == 1


def test_a_review_notice_without_a_skip_class_is_not_evidence(cold):
    # Notices written before the skip class existed, and the last-failed path, say nothing about quota or auth.
    with db.transaction(cold.con):
        gates.review_route_notice(cold.con, run(cold), {"id": "G1", "task_id": "T1"}, "code_reviewer", CODEX,
                                  "codex@1/gpt-6-luna@low", f"{CODEX} was rejected: trust floor")
    probe_pass(cold, CODEX, FUTURE)
    assert sweep(cold) == [] and recovered(cold) == []



def test_a_usage_limit_rerun_fallback_alone_recovers_and_shows_in_status(env, monkeypatch):
    """#921 F6: a rerun after a usage_limit records `route.fallback` (source usage_limit) naming the blocked route. With no
    `route.failed` for that route, the fallback is the only evidence, so its recovery and status line must come from it."""
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office status")  # the fixture has no plan row
    _approve(env, plan=PLANNED)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    con = env.con()
    rid = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    parent = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())
    limit_key = routing.cooldown_key(parent["harness"], parent["invocation_model_id"])
    with db.transaction(con):  # the stalled dispatch and its quota wall, as dispatch records them
        con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, stall_kind='usage_limit' WHERE id=?",
                    (parent["id"],))
        con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)", (limit_key, FUTURE, now_iso()))

    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env, claude=1))
    assert code == 0, out
    latest = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())
    assert latest["triple"] != parent["triple"], out
    fallback = con.execute("SELECT seq, payload_json FROM events WHERE kind='route.fallback' AND dispatch_id=?",
                           (latest["id"],)).fetchone()
    assert json.loads(fallback["payload_json"])["before"] == parent["triple"]
    assert con.execute("SELECT 1 FROM events WHERE run_id=? AND kind='route.failed'", (rid,)).fetchone() is None

    run_row = state.get_run(con, rid)
    assert routechange.sweep_recovered(con, run_row) == []  # the quota wall is live
    with db.transaction(con):
        con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?", (now_iso(), limit_key))
    assert len(routechange.sweep_recovered(con, run_row)) == 1
    [rec] = recovered_in(con, rid)
    assert rec["route"] == parent["triple"] and rec["episode"] == fallback["seq"] and rec["evidence"] == "route.fallback"
    assert rec["role"] == "executor" and rec["basis"] == "demotion lapsed"
    assert rec["harness"] == parent["harness"] and rec["model"] == parent["invocation_model_id"]

    line = line_for(parent["triple"], "demotion lapsed")
    assert any(l.endswith(line) for l in guide.status(con, run_row).lines)
    assert len(recovered_in(con, rid)) == 1  # the status read does not record it again


def recovered_in(con, run_id):
    return [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? "
                                                  "ORDER BY seq", (run_id, RECOVERED)).fetchall()]


def test_an_auth_fallback_recovers_once_its_route_demotion_lapses_and_a_sourceless_one_is_not_evidence(plain):
    with db.transaction(plain.con):  # a rerun that left an auth-blocked route, and a runtime fallback with no source
        state.emit(plain.con, run(plain), "route.fallback", "T1 rerouted after auth", task_id="T1",
                   payload={"before": CODEX, "after": "codex@1/gpt-6-luna@low", "source": "auth", "actor": "orchestrator"})
        state.emit(plain.con, run(plain), "route.fallback", "T2 fell back", task_id="T2",
                   payload={"before": "codex@1/gpt-6-luna@low", "after": CODEX, "reason": "auth"})
    demoted = routing.demotion_key("codex", "gpt-6.1-sol", "high")
    cooldown(plain, demoted, FUTURE)
    assert sweep(plain) == []
    with db.transaction(plain.con):
        plain.con.execute("UPDATE route_cooldowns SET expires_at=? WHERE route=?", (now_iso(), demoted))
    assert len(sweep(plain)) == 1
    [rec] = recovered(plain)
    assert rec["route"] == CODEX and rec["evidence"] == "route.fallback" and rec["basis"] == "demotion lapsed"
