"""#494 T5: learning attribution for discovery trials.

A trial is an ordinary executor dispatch plus the append-only `route_discovery_events`
under its `attempt_id`. These tests seed a real runs.db with that shape (the rows T4's
dispatch writes) and drive `route_learning` directly. No harness, no model, no network.
"""
import json
import sqlite3

import pytest

from office import db, route_learning, route_policy, scoring

ROUTE = "codex/gpt-6.1-sol@high"          # the learner's evidence key
TRIPLE = "codex@2/gpt-6.1-sol@high"       # the dispatched route identity
FALLBACK = "claude@2/claude-sonnet-5-5@high"
ALLOC = {"probes": {"used": 1, "max": 2}, "trials": {"used": 1, "max": 1},
         "rolling": {"used": 1, "max": 1, "window": 1}}
DIGEST = "sha256:policy"


def fingerprint(effort="high"):
    return {"harness": "codex", "harness_version": "0.162.0", "adapter_hash": "sha256:adapter", "profile": "worker",
            "invocation_model_id": "gpt-6.1-sol", "effort": effort}


def probe_key(effort="high"):
    fp = fingerprint(effort)
    return "|".join(fp[k] for k in ("harness", "harness_version", "invocation_model_id", "effort", "adapter_hash", "profile"))


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    scoring.ensure_trust_schema(con)
    yield con
    con.close()


def at(day, hour=0):
    return f"2026-09-{day:02d}T{hour:02d}:00:00+00:00"


class Seed:
    """Rows the way dispatch, submit and gates leave them."""

    def __init__(self, con):
        self.con, self.n = con, 0

    def run(self, rid, phase="closed"):
        self.con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES(?,?,?,?)",
                         (rid, "Change", phase, json.dumps({"size_class": "S"})))

    def task(self, rid, tid, status="accepted", accepted=None):
        self.con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, "
                         "status, introduced_plan_version, contract_version, acceptance_version, accepted_revision_id, "
                         "created_at, updated_at) VALUES(?,?,?,'executor','[]','[]','[]','[]',?,1,1,1,?,'t','t')",
                         (rid, tid, tid, status, accepted))

    def dispatch(self, rid, did, tid, *, day=1, effort="high", model="gpt-6.1-sol", harness="codex", term="success",
                 exit_code=0):
        self.con.execute(
            "INSERT INTO dispatches(id, run_id, role, task_id, triple, harness, model, effort, started_at, ended_at, "
            "terminal_classification, exit_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, rid, "executor", tid, f"{harness}@2/{model}@{effort}", harness, model, effort, at(day), at(day, 1),
             term, exit_code))

    def revision(self, rid, rev, did, tid):
        self.con.execute(
            "INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, requirements_version, "
            "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
            "VALUES(?,?,?,1,?,'c','t',1,1,1,'e',?,'submitted','t')", (rev, rid, tid, did, rev))

    def finding(self, rid, did, tid, rev, category):
        self.n += 1
        self.con.execute("INSERT INTO findings(id, dispatch_id, run_id, task_id, revision_id, category, severity, state, "
                         "summary, created_at) VALUES(?,?,?,?,?,?,'material','open','x','t')",
                         (f"F{self.n}", did, rid, tid, rev, category))

    def event(self, kind, attempt, rid, tid, did, *, effort="high", outcome=None, reason_class=None, origin="dispatch",
              freshness="fresh-run"):
        with db.transaction(self.con):
            route_policy.record_event(
                self.con, kind=kind, attempt_id=attempt, origin=origin, policy_digest=DIGEST, probe_key=probe_key(effort),
                reason="discovery: untried candidate", run_id=rid, plan_version=3, task_id=tid, dispatch_id=did,
                role="executor", fingerprint_json=fingerprint(effort),
                candidate_route=f"codex@2/gpt-6.1-sol@{effort}", primary_route=FALLBACK, fallback_route=FALLBACK,
                probe_freshness=freshness, allocation_json=ALLOC, outcome=outcome, reason_class=reason_class)

    def trial(self, attempt, rid, tid, did, *, ending="launched", reason_class=None, effort="high"):
        """The events and `route_trials` row of one trial, as far as `ending` says it got."""
        self.event("probe-reserved", attempt, rid, tid, None, effort=effort, outcome="reserved", origin="preflight",
                   freshness="none")
        self.event("probe-result", attempt, rid, tid, None, effort=effort, outcome="pass", origin="preflight")
        self.event("dispatch-linked", attempt, rid, tid, did, effort=effort)
        self.event("trial-reserved", attempt, rid, tid, did, effort=effort, outcome="reserved")
        status = "reserved"
        if ending != "reserved":
            self.event("trial-launched", attempt, rid, tid, did, effort=effort, outcome="launched")
            status = "launched"
        if ending in ("launch-failed", "fell-back"):
            self.event("trial-launch-failed", attempt, rid, tid, did, effort=effort, outcome="launch-failed",
                       reason_class=reason_class, origin="recovery")
            status = "launch-failed"
        if ending == "fell-back":
            self.event("trial-fell-back", attempt, rid, tid, did, effort=effort, outcome="fell-back", origin="recovery")
            status = "fell-back"
        self.con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                         "policy_digest, reason, status, created_at, updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (attempt, rid, tid, did, "executor", f"codex@2/gpt-6.1-sol@{effort}", probe_key(effort), FALLBACK,
                          DIGEST, "discovery: untried candidate", status, at(1), at(1)))

    def landed_trial(self, i):
        """Run Ri: one trial dispatch whose revision was accepted."""
        rid, did, rev = f"R{i}", f"D{i}", f"V{i}"
        self.run(rid)
        self.task(rid, "T1", accepted=rev)
        self.dispatch(rid, did, "T1", day=i)
        self.revision(rid, rev, did, "T1")
        self.trial(f"A{i}", rid, "T1", did)


@pytest.fixture
def seed(con):
    return Seed(con)


def outcome(con, did):
    return next(o for o in route_learning.derive_outcomes(con) if o["dispatch_id"] == did)


def evidence(con, outcomes):
    cand = {"harness": "codex", "harness_version": "2.0.0", "model_id": "gpt-6.1-sol",
            "invocation_model_id": "gpt-6.1-sol", "effort": "high"}
    return route_learning.evidence_for(outcomes, [cand], {}, as_of=at(5))["routes"][ROUTE]


def terminal_events(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM route_discovery_events WHERE kind IN ('trial-accepted','trial-rejected') ORDER BY seq")]


def all_events(con):
    return [tuple(r) for r in con.execute("SELECT * FROM route_discovery_events ORDER BY seq")]


# ------------------------------------------------------------------ an accepted trial teaches the route

def test_an_accepted_trial_is_route_attributed_evidence(con, seed):
    seed.landed_trial(1)
    o = outcome(con, "D1")
    assert o["success"] and o["attribution"] == "route" and o["learn_weight"] == 1.0 and o["route"] == ROUTE
    assert o["work"] is True
    assert o["trial"] == {"attempt_id": "A1", "state": "launched", "reason_class": None, "fallback_route": FALLBACK,
                          "probe_key": probe_key()}
    (episode,) = route_learning.episodes([o])
    assert episode["trial"]["attempt_id"] == "A1" and episode["success"] and episode["learn_weight"] == 1.0
    stats = evidence(con, [o])
    assert stats["successes"] > 0.9 and stats["failures"] == 0


def test_a_dispatch_with_no_trial_events_carries_no_trial(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V1")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    assert outcome(con, "D1")["trial"] is None


def test_accepted_trials_clear_the_maturity_bar_through_the_existing_replay_rules(con, seed):
    for i in range(1, 13):
        seed.landed_trial(i)
    eps = [e for e in route_learning.episodes(route_learning.derive_outcomes(con)) if e["role"] == "executor"]
    assert len(eps) == 12 and all(e["trial"] for e in eps)
    (move,) = route_learning.eligibility_transitions(eps, {ROUTE: {"prior_p": 0.5}}, {})
    assert move["state"] == "learned-eligible" and move["replay"]["validated"] is True
    assert move["evidence"]["samples"] == 12 and move["evidence"]["runs"] == 12
    # the same bar still refuses a thin record: two accepted trials are not mature
    assert route_learning.eligibility_transitions(eps[:2], {ROUTE: {"prior_p": 0.5}}, {}) == []


def test_learning_from_accepted_trials_never_touches_trust_or_quarantine(con, seed):
    for i in range(1, 13):
        seed.landed_trial(i)
    cid = TRIPLE
    trust_before = scoring.evaluate_trust_state(con, cid)
    acts = lambda: con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]  # noqa: E731
    rows_before = acts()
    with db.transaction(con):
        written = route_learning.refresh(con, {"executor": {ROUTE: {"prior_p": 0.5}}})
    assert [w["state"] for w in written] == ["learned-eligible"]  # learning happened
    assert acts() == rows_before == 0
    assert scoring.evaluate_trust_state(con, cid) == trust_before
    assert trust_before[1] == "valid-unverified"  # accepted trials are not trust


# ------------------------------------------------------------------ what is not the model's

@pytest.mark.parametrize("reason", ["transient", "auth-quota-blocked", "isolation-missing", "conformance-failed",
                                    "unsupported-model-effort", None])
@pytest.mark.parametrize("ending", ["launch-failed", "fell-back"])
def test_a_trial_that_ended_before_work_is_not_charged_to_the_model(con, seed, reason, ending):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)  # the trial: the harness exited nonzero
    seed.trial("A1", "R1", "T1", "D1", ending=ending, reason_class=reason)
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")  # the known-working fallback
    seed.revision("R1", "V2", "D2", "T1")
    o = outcome(con, "D1")
    assert o["attribution"] == "environment" and o["learn_weight"] == 0 and not o["success"]
    assert o["trial"]["reason_class"] == reason and o["trial"]["state"] == ending
    assert (f"({reason})" in o["attribution_provenance"]) == (reason is not None)
    # the fallback is a different route and is read as any dispatch is
    d2 = outcome(con, "D2")
    assert d2["success"] and d2["route"] == "claude/claude-sonnet-5-5@high" and d2["trial"] is None
    stats = evidence(con, route_learning.derive_outcomes(con))
    assert stats["failures"] == 0 and stats["n_effective"] == 0


def test_the_same_nonzero_exit_without_a_trial_still_counts_against_the_route(con, seed):
    """The control: only the recorded trial events move that dispatch out of the route's record."""
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    o = outcome(con, "D1")
    assert o["attribution"] == "mixed" and o["learn_weight"] > 0


def test_work_that_started_is_never_classified_as_a_launch_failure(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", status="cancelled")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)
    seed.revision("R1", "V1", "D1", "T1")
    seed.finding("R1", "D1", "T1", "V1", "code_review")
    seed.trial("A1", "R1", "T1", "D1", ending="launch-failed", reason_class="transient")  # recorded, yet work exists
    o = outcome(con, "D1")
    assert o["attribution"] != "environment" and o["work"] is True


def test_a_brief_or_plan_defect_on_a_trial_is_not_the_models(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", status="cancelled")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    seed.finding("R1", "D1", "T1", "V1", "brief")
    seed.trial("A1", "R1", "T1", "D1")
    o = outcome(con, "D1")
    assert o["attribution"] == "plan" and o["learn_weight"] == 0 and o["trial"]["attempt_id"] == "A1"


def test_a_code_review_rejection_of_a_trial_counts_against_the_route(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    seed.finding("R1", "D1", "T1", "V1", "code_review")
    seed.trial("A1", "R1", "T1", "D1")
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    o = outcome(con, "D1")
    assert o["attribution"] == "route" and o["learn_weight"] > 0.8 and not o["success"]


def test_a_confirmed_unsupported_effort_marks_only_that_exact_effort(con, seed):
    for run, effort, ending, reason in (("R1", "high", "launch-failed", "unsupported-model-effort"),
                                        ("R2", "medium", "launched", None),
                                        ("R3", "xhigh", "launch-failed", "transient")):
        seed.run(run)
        seed.task(run, "T1", accepted="V")
        seed.dispatch(run, f"D{run}", "T1", effort=effort)
        seed.trial(f"A{run}", run, "T1", f"D{run}", ending=ending, reason_class=reason, effort=effort)
    # a bare probe that confirmed `low` unsupported: no trial ever followed it
    seed.event("probe-result", "Alow", None, None, None, effort="low", outcome="fail",
               reason_class="unsupported-model-effort", origin="manual")
    # a probe that failed for another reason confirms nothing
    seed.event("probe-result", "Amax", None, None, None, effort="max", outcome="fail", reason_class="transient",
               origin="manual")
    assert set(route_learning.unsupported_routes(con)) == {"codex/gpt-6.1-sol@high", "codex/gpt-6.1-sol@low"}
    assert route_learning.unsupported_routes(con)["codex/gpt-6.1-sol@high"]["attempt_id"] == "AR1"


def test_attribution_reads_the_events_not_the_mutable_probe_cache(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)
    seed.trial("A1", "R1", "T1", "D1", ending="fell-back", reason_class="auth-quota-blocked")
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    before = outcome(con, "D1")
    # a later probe of the same key lands in the cache: first as a pass, then as a failure
    for result, reason in (("pass", None), ("fail", "conformance-failed")):
        con.execute("INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                    "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (probe_key(), "codex", "0.162.0", "sha256:adapter", "worker", "gpt-6.1-sol", "high", result, reason,
                     "later", at(9), "R9", None, "Alater"))
        after = outcome(con, "D1")
        assert after["attribution"] == before["attribution"] == "environment"
        assert after["attribution_provenance"] == before["attribution_provenance"]
        assert after["trial"] == before["trial"] and after["trial"]["reason_class"] == "auth-quota-blocked"


# ------------------------------------------------------------------ the terminal outcome, recorded once

def observe(con):
    with db.transaction(con):
        return route_learning.record_trial_outcomes(con)


def trial_row(con, attempt):
    return dict(con.execute("SELECT * FROM route_trials WHERE id=?", (attempt,)).fetchone())


def test_an_accepted_trial_gets_one_terminal_event_and_its_row_updated(con, seed):
    seed.landed_trial(1)
    prior = all_events(con)
    (written,) = observe(con)
    assert written["kind"] == "trial-accepted" and written["attempt_id"] == "A1" and written["dispatch_id"] == "D1"
    (event,) = terminal_events(con)
    assert (event["kind"], event["outcome"], event["origin"]) == ("trial-accepted", "accepted", "job")
    assert (event["run_id"], event["task_id"], event["dispatch_id"], event["role"]) == ("R1", "T1", "D1", "executor")
    assert (event["policy_digest"], event["probe_key"], event["fallback_route"]) == (DIGEST, probe_key(), FALLBACK)
    assert json.loads(event["fingerprint_json"]) == fingerprint() and event["candidate_route"] == TRIPLE
    row = trial_row(con, "A1")
    assert row["status"] == "accepted" and row["outcome"] == "accepted"
    assert all_events(con)[:len(prior)] == prior  # nothing earlier was touched


def test_a_second_observation_is_a_no_op(con, seed):
    seed.landed_trial(1)
    observe(con)
    snapshot, row = all_events(con), trial_row(con, "A1")
    assert observe(con) == []
    with db.transaction(con):
        route_learning.refresh(con)
        route_learning.refresh(con)
    assert all_events(con) == snapshot and trial_row(con, "A1") == row and len(terminal_events(con)) == 1


def test_a_rejected_trial_is_recorded_as_rejected(con, seed):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    seed.trial("A1", "R1", "T1", "D1")
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    (written,) = observe(con)
    assert written["kind"] == "trial-rejected"
    assert trial_row(con, "A1")["status"] == "rejected" and trial_row(con, "A1")["outcome"] == "rejected"


def test_a_trial_still_in_flight_has_no_gate_result_to_observe(con, seed):
    seed.run("R1", phase="executing")
    seed.task("R1", "T1", status="running")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    seed.trial("A1", "R1", "T1", "D1")
    assert observe(con) == [] and terminal_events(con) == [] and trial_row(con, "A1")["status"] == "launched"


@pytest.mark.parametrize("ending", ["launch-failed", "fell-back", "reserved"])
def test_a_trial_that_never_did_work_gets_no_terminal_event(con, seed, ending):
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)
    seed.trial("A1", "R1", "T1", "D1", ending=ending, reason_class="transient")
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    before = trial_row(con, "A1")
    assert observe(con) == [] and terminal_events(con) == [] and trial_row(con, "A1") == before


def test_the_terminal_event_and_the_trial_row_change_together_or_not_at_all(con, seed):
    seed.landed_trial(1)
    con.execute("CREATE TRIGGER no_trial_update BEFORE UPDATE ON route_trials "
                "BEGIN SELECT RAISE(ABORT, 'row locked'); END")
    snapshot, row = all_events(con), trial_row(con, "A1")
    assert observe(con) == []
    assert all_events(con) == snapshot and trial_row(con, "A1") == row  # no event without the status change
    con.execute("DROP TRIGGER no_trial_update")
    assert [w["kind"] for w in observe(con)] == ["trial-accepted"]  # and the next observation still lands


def test_observing_needs_the_callers_transaction(con, seed):
    seed.landed_trial(1)
    with pytest.raises(ValueError, match="db.transaction"):
        route_learning.record_trial_outcomes(con)
    assert terminal_events(con) == []


def test_refresh_records_the_terminal_outcome_and_the_attribution_together(con, seed):
    seed.landed_trial(1)
    with db.transaction(con):
        route_learning.refresh(con)
    assert [e["kind"] for e in terminal_events(con)] == ["trial-accepted"]
    assert con.execute("SELECT route, success, attribution FROM route_attributions WHERE dispatch_id='D1'").fetchone()[:] \
        == (ROUTE, 1, "route")


def test_no_learner_path_updates_or_deletes_an_event(con, seed):
    seed.landed_trial(1)
    seed.landed_trial(2)
    before = all_events(con)
    observe(con)
    with db.transaction(con):
        route_learning.refresh(con)
    route_learning.attempt_history(con)
    route_learning.unsupported_routes(con)
    after = all_events(con)
    assert after[:len(before)] == before and len(after) == len(before) + 2
    with pytest.raises(sqlite3.IntegrityError):  # and the store itself refuses it
        con.execute("UPDATE route_discovery_events SET outcome='x'")


# ------------------------------------------------------------------ audit reads

def test_attempt_history_returns_every_event_of_each_attempt_in_order(con, seed):
    seed.landed_trial(1)
    seed.event("probe-result", "Amanual", None, None, None, outcome="pass", origin="manual")
    history = route_learning.attempt_history(con)
    assert [a["attempt_id"] for a in history] == ["A1", "Amanual"]
    trial, manual = history
    assert [e["kind"] for e in trial["events"]] == ["probe-reserved", "probe-result", "dispatch-linked",
                                                     "trial-reserved", "trial-launched"]
    assert (trial["run_id"], trial["plan_version"], trial["origin"], trial["policy_digest"]) == ("R1", 3, "preflight", DIGEST)
    assert trial["dispatches"] == ["D1"] and manual["dispatches"] == [] and manual["run_id"] is None
    assert route_learning.attempt_history(con, run_id="R1")[0]["attempt_id"] == "A1"
    assert [a["attempt_id"] for a in route_learning.attempt_history(con, run_id="R1")] == ["A1"]
    assert [a["attempt_id"] for a in route_learning.attempt_history(con, run_id="R1", include_unbound=True)] \
        == ["A1", "Amanual"]


def test_live_trial_names_the_route_and_fallback_until_the_trial_ends(con, seed):
    seed.run("R1", phase="executing")
    seed.task("R1", "T1", status="running")
    seed.dispatch("R1", "D1", "T1")
    seed.trial("A1", "R1", "T1", "D1")
    assert route_learning.live_trial(con, "D1") == {"route": TRIPLE, "fallback": FALLBACK, "status": "launched"}
    assert route_learning.live_trial(con, "Dother") is None and route_learning.live_trial(con, None) is None
    con.execute("UPDATE route_trials SET status='accepted'")
    assert route_learning.live_trial(con, "D1") is None


def test_pre_change_databases_without_discovery_tables_still_derive_outcomes(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE dispatches(id TEXT, run_id TEXT, role TEXT, task_id TEXT, triple TEXT, harness TEXT, "
                "model TEXT, effort TEXT, started_at TEXT, ended_at TEXT, outcome TEXT, attribution TEXT, money_actual REAL)")
    assert route_learning.derive_outcomes(con) == []
    assert route_learning.trial_attempts(con) == {} and route_learning.unsupported_routes(con) == {}
    assert route_learning.attempt_history(con) == [] and route_learning.live_trial(con, "D1") is None
