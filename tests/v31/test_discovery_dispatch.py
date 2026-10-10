"""#494 T4: dispatch reservation, probe preflight and trial launch-failure recovery.

Unit tier. An isolated Office home, one scripted fake `codex` (tests/fixtures/route_probe/fake_harness.py, the
same one the routing and probe suites use), a real runs.db with a persisted run and tasks, and the real
`dispatch.dispatch` (jobs queued, never started). No model is called.
"""
import json
import sqlite3
import subprocess

import pytest

from test_route_discovery_consumer_contract import EFFORTS, SOL, World

from office import candidates, db, dispatch, paths, plans, prs, route_policy, route_probe, routing, state, version
from office.util import dumps, now_iso


class Cold(World):
    """World plus a persisted run (`run-A`) with tasks T1 and T2, and a repository with a base commit."""

    def __init__(self, tmp, monkeypatch):
        super().__init__(tmp, monkeypatch)
        monkeypatch.setenv("OFFICE_JOBS", "manual")
        repo = tmp / "repo"
        for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        self.base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                                   text=True).stdout.strip()
        now = now_iso()
        sdir = paths.run_dir("run-A")
        sdir.mkdir(parents=True, exist_ok=True)
        with db.transaction(self.con):
            self.con.execute(
                "INSERT INTO runs(id, family_id, created_at, status, office_version, repo_root, git_common_dir, goal, phase, "
                "gear, playbook, base_sha, state_dir, requirements_version, plan_version, routing_version, policy_json, "
                "risk_json, gates_json, envelope_json, plan_review_json, planner_mode, updated_at, escalations_used) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                ("run-A", "run-A", now, "executing", version.current(), str(repo), str(repo / ".git"), "goal", "executing",
                 "", "Change", self.base, str(sdir), 1, 3, 1, dumps(self.config), dumps(self.run["risk"]), "{}", "[]",
                 "{}", "inline", now))
            for tid in ("T1", "T2"):
                self.con.execute(
                    "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
                    "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("run-A", tid, f"task {tid}", "executor", dumps([f"{tid.lower()}.py"]), "[]", "[]", "[]", "[]", None,
                     "planned", 3, 3, 3, now, now))
        self.run = state.get_run(self.con, "run-A")
        monkeypatch.setattr(plans, "require_dispatchable", lambda con, run: None)
        monkeypatch.setattr(plans, "require_scope_clear", lambda con, run, tid: None)
        monkeypatch.setattr(prs, "settings", lambda con, run: None)

    def dispatch(self, *tasks, parallel=False):
        self.run = state.get_run(self.con, "run-A")
        return dispatch.dispatch(self.con, self.run, list(tasks), parallel=parallel)

    def dispatches(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM dispatches ORDER BY started_at, id")]

    def trials(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM route_trials ORDER BY created_at")]

    def kinds(self, attempt_id):
        return [e["kind"] for e in self.events(attempt_id)]

    def attempts(self):
        return list(dict.fromkeys(e["attempt_id"] for e in self.events()))

    def leases(self, task="T1"):
        return [dict(r) for r in self.con.execute("SELECT * FROM leases WHERE task_id=? ORDER BY acquired_at", (task,))]


@pytest.fixture
def cold(tmp_path, monkeypatch):
    w = Cold(tmp_path, monkeypatch)
    yield w
    assert w.authority() == w.baseline, "a dispatch wrote an authority row"



def _route_json(d):
    return json.loads(d["route_json"])


def _live_leases(cold, task="T1"):
    return [l for l in cold.leases(task) if not l["released_at"] and not l["revoked_at"]]


def _audit(cold, phase="dispatch"):
    row = cold.con.execute("SELECT disclosure_json FROM route_audit WHERE phase=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (phase,)).fetchone()
    return json.loads(row[0])


def _revision(cold, d, rid="R1"):
    cold.con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, "
                     "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                     "VALUES(?,'run-A','T1',1,?,?,?,1,3,3,'e',?,'submitted',?)",
                     (rid, d["id"], "a" * 40, "b" * 40, f"op-{rid}", now_iso()))


def _frozen(cold):
    """Every discovery event row exactly as stored, to prove later steps only append."""
    return {e["seq"]: tuple(e.items()) for e in cold.events()}


# ------------------------------------------------------------------ D5: cold start, end to end

def test_untried_route_probe_pass_dispatches_the_trial_on_one_lease(cold):
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (trial,) = cold.trials()
    (attempt,) = cold.attempts()
    assert d["triple"] == trial["route"] and d["triple"].startswith("codex@0/gpt-6.1-sol@")
    assert trial["id"] == attempt and trial["dispatch_id"] == d["id"] and trial["status"] == "reserved"
    assert trial["fallback_route"] != trial["route"] and trial["role"] == "executor"
    assert len(cold.leases()) == 1 and _live_leases(cold)[0]["id"] == d["lease_id"]
    # one probe, recorded in route_probes against this exact fingerprint
    (probe,) = cold.cache()
    assert probe["result"] == "pass" and probe["attempt_id"] == attempt
    # the dispatch record and the route audit name the reason, the probe key, the fallback and the policy digest
    found = _route_json(d)["discovery"]
    assert (found["intent"], found["probe_key"], found["fallback_route"], found["policy_digest"]) == (
        "trial", trial["probe_key"], trial["fallback_route"], trial["policy_digest"])
    assert found["reason"] == trial["reason"] and "fresh exact conformance probe passed" in found["reason"]
    assert found["fallback"]["candidate"]["model_id"] == "gpt-6-astra"
    audit = _audit(cold)
    assert audit["dispatch"]["discovery"] == {
        "attempt_id": attempt, "probe_key": trial["probe_key"], "policy_digest": trial["policy_digest"],
        "intent": "trial", "blocked": None, "trial_reason": trial["reason"], "fallback_route": trial["fallback_route"]}
    assert audit["discovery"]["intent"] == "trial" and audit["discovery"]["attempt_id"] == attempt
    # the event sequence: the probe's events exist before any dispatch row, then the link and the reservation
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved"]
    events = cold.events(attempt)
    assert [e["dispatch_id"] for e in events] == [None, None, d["id"], d["id"]]
    assert events[2]["outcome"] == "trial" and events[3]["outcome"] == "reserved"
    assert {e["policy_digest"] for e in events} == {trial["policy_digest"]}


@pytest.mark.parametrize("mode,reason_class", [("unsupported_effort", "unsupported-model-effort"), ("auth", "auth-quota-blocked")])
def test_a_failed_probe_dispatches_the_known_working_fallback_with_no_trial(cold, mode, reason_class):
    cold.script(mode=mode)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert not cold.trials() and d["triple"].startswith("codex@0/gpt-6-astra@")
    assert len(cold.leases()) == 1 and len(_live_leases(cold)) == 1
    (probe,) = cold.cache()
    assert (probe["result"], probe["reason_class"]) == ("fail", reason_class)
    # the failed attempt stays attributed to the dispatch that replaced it
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]
    link = cold.events(attempt)[-1]
    assert link["dispatch_id"] == d["id"] and link["outcome"] == "fallback"
    assert cold.events(attempt)[1]["reason_class"] == reason_class
    found = _route_json(d)["discovery"]
    assert found["intent"] == "none" and found["blocked"] == f"probe-failed:{reason_class}" and not found["fallback"]
    assert _audit(cold)["dispatch"]["discovery"]["blocked"] == f"probe-failed:{reason_class}"
    assert not cold.con.execute("SELECT 1 FROM outbox WHERE kind='trial_recovery'").fetchone()


def test_a_refused_probe_reservation_dispatches_the_primary_and_records_probe_cap(cold, monkeypatch):
    real = route_probe.ensure

    def crowded(con, run, cand, **kw):
        # another dispatch took both of the run's probe slots between the decision and the reservation
        with db.transaction(con):
            for i in range(2):
                con.execute("INSERT INTO route_probe_reservations(id, run_id, probe_key, status, reserved_at) "
                            "VALUES(?,?,?,?,?)", (f"other-{i}", run["id"], f"k{i}", "completed", now_iso()))
        return real(con, run, cand, **kw)

    monkeypatch.setattr(route_probe, "ensure", crowded)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert not cold.trials() and not cold.cache() and d["triple"].startswith("codex@0/gpt-6-astra@")
    assert cold.kinds(attempt) == ["probe-refused", "dispatch-linked"]
    assert cold.events(attempt)[0]["detail"].startswith("probe-cap")
    assert cold.events(attempt)[-1]["dispatch_id"] == d["id"]
    assert _route_json(d)["discovery"]["blocked"] == "probe-cap"
    assert _audit(cold)["dispatch"]["discovery"]["blocked"] == "probe-cap"
    assert _audit(cold)["discovery"]["blocked"] == "probe-cap"
    assert len(_live_leases(cold)) == 1


def test_a_denial_written_between_the_probe_and_the_dispatch_dispatches_the_fallback(cold, monkeypatch):
    real = route_probe.ensure

    def then_denied(con, run, cand, **kw):
        out = real(con, run, cand, **kw)
        cold.write_policy("user", denied=[f"gpt-6.1-sol"])  # the permission changes after the probe passed
        return out

    monkeypatch.setattr(route_probe, "ensure", then_denied)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert not cold.trials() and d["triple"].startswith("codex@0/gpt-6-astra@")
    assert cache_pass(cold)  # the probe itself passed: only the permission changed
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]
    assert _route_json(d)["discovery"]["blocked"] == "candidate-gone"
    assert len(_live_leases(cold)) == 1


def cache_pass(cold):
    return [c for c in cold.cache() if c["result"] == "pass"]


def test_a_quota_change_between_the_probe_and_the_dispatch_dispatches_the_fallback(cold, monkeypatch):
    low = {"on": False}
    build = candidates.build_candidates

    def quota_after(*args, **kw):
        found, skipped = build(*args, **kw)
        for c in found:
            if low["on"] and c["invocation_model_id"] == SOL:  # the probed model's own limit is now nearly spent
                c["quota"] = {"status": "ok", "tightest_remaining_percent": 1.0, "projected_burn_percent": 0.0}
        return found, skipped

    real = route_probe.ensure

    def then_low(con, run, cand, **kw):
        out = real(con, run, cand, **kw)
        low["on"] = True
        return out

    monkeypatch.setattr(candidates, "build_candidates", quota_after)
    monkeypatch.setattr(route_probe, "ensure", then_low)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert not cold.trials() and d["triple"].startswith("codex@0/gpt-6-astra@")
    assert cache_pass(cold) and cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]
    assert _route_json(d)["discovery"]["blocked"] == "quota"
    assert len(_live_leases(cold)) == 1


# ------------------------------------------------------------------ the transaction's own rechecks

def _between(cold, monkeypatch, change):
    """Run `change(cold)` after the preflight and before the dispatch transaction opens."""
    real = dispatch.preflight_discovery

    def hooked(con, run, task, decision, **kw):
        out = real(con, run, task, decision, **kw)
        if out.get("trial") and not cold.__dict__.get("_changed"):
            cold._changed = True
            change(cold, out)
        return out

    monkeypatch.setattr(dispatch, "preflight_discovery", hooked)


def _take_the_trial(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, "
                         "fallback_route, policy_digest, reason, status, created_at, updated_at) "
                         "VALUES('other','run-A','T2','Dother','executor','x','k','y','d','r','launched',?,?)",
                         (now_iso(), now_iso()))


def _large_task(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET risk_json=? WHERE id='run-A'",
                         (dumps({"size_class": "L", "blast_radius": "repo", "irreversible": False}),))


def _irreversible(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET risk_json=? WHERE id='run-A'",
                         (dumps({"size_class": "S", "blast_radius": "repo", "irreversible": True}),))


def _spent_quota(cold, decision):
    decision["candidate"]["quota"] = {"status": "ok", "tightest_remaining_percent": 4.0, "projected_burn_percent": 0.0}


def _quarantine(triple):
    """The route's trust state reads `quarantined` from now on (the real state comes from adapter-attributed
    failure evidence: derived, never written by an act)."""
    from office import scoring
    real = scoring.evaluate_trust_state

    def evaluate(con, target):
        return (1, "quarantined") if target == triple else real(con, target)

    return evaluate


def _quarantined_fallback(cold, decision):
    from office import scoring
    cold.monkeypatch.setattr(scoring, "evaluate_trust_state", _quarantine(decision["trial"]["fallback"]["selected"]))


def _quarantined_trial_route(cold, decision):
    from office import scoring
    cold.monkeypatch.setattr(scoring, "evaluate_trust_state", _quarantine(decision["selected"]))


def _stale_probe(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("UPDATE route_probes SET result='fail', reason_class='transient'")


def _disabled(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (dumps(cold.policy(enabled=False)),))


def _blast_radius(cold, _decision):
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET risk_json=? WHERE id='run-A'",
                         (dumps({"size_class": "S", "blast_radius": "production", "irreversible": False}),))


def _changed_fingerprint(cold, _decision):
    with db.transaction(cold.con):  # the probe on record now speaks for another harness version
        cold.con.execute("UPDATE route_probes SET key=key || '-old'")


@pytest.mark.parametrize("change,blocked,others", [
    (_take_the_trial, "trial-cap", ["other"]), (_large_task, "risk", []), (_irreversible, "risk", []),
    (_blast_radius, "risk", []), (_spent_quota, "quota-reserve", []), (_quarantined_fallback, "no-fallback", []),
    (_quarantined_trial_route, "quarantined", []), (_stale_probe, "probe-stale", []),
    (_changed_fingerprint, "probe-stale", []), (_disabled, "discovery-disabled", [])])
def test_any_recheck_that_fails_in_the_transaction_dispatches_the_fallback_and_writes_no_trial(cold, monkeypatch, change, blocked, others):
    _between(cold, monkeypatch, change)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert d["triple"].startswith("codex@0/gpt-6-astra@"), d["triple"]
    assert [t["id"] for t in cold.trials()] == others and len(_live_leases(cold)) == 1
    assert not [t for t in cold.trials() if t["dispatch_id"] == d["id"]]
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]
    assert cold.events(attempt)[-1]["dispatch_id"] == d["id"]
    assert _route_json(d)["discovery"]["blocked"] == blocked and not _route_json(d)["discovery"]["fallback"]
    assert _audit(cold)["dispatch"]["discovery"]["blocked"] == blocked


def test_the_rolling_cap_is_rechecked_in_the_transaction(cold, monkeypatch):
    # two decisions in the window allow one trial at 50 percent. None exists when the route is decided; another
    # run's trial lands before this transaction opens, and the per-run cap is nowhere near reached.
    cold.config = cold.policy(max_trial_percent_rolling_20=50, max_trials_per_run=5)
    cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (dumps(cold.config),))
    from office import route_learning
    route_learning.ensure_schema(cold.con)
    cold.con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, decision_hash, disclosure_json, created_at) "
                     "VALUES('ra-old','run-B','T9','executor','dispatch','h','{}',?)", (now_iso(),))

    def another_runs_trial(cold, _decision):
        with db.transaction(cold.con):
            cold.con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, "
                             "fallback_route, policy_digest, reason, status, created_at, updated_at) "
                             "VALUES('other','run-B','T9','Dother','executor','x','k','y','d','r','launched',?,?)",
                             (now_iso(), now_iso()))

    _between(cold, monkeypatch, another_runs_trial)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith("codex@0/gpt-6-astra@") and [t["id"] for t in cold.trials()] == ["other"]
    assert _route_json(d)["discovery"]["blocked"] == "rolling-cap"




def test_no_trial_is_launched_without_a_known_working_fallback_even_when_routing_would_allow_it(cold, monkeypatch):
    # `require_known_fallback: false` lets routing draw a trial whose fallback is unproven. The dispatch
    # transaction still refuses: a trial needs a recorded known-working fallback, whoever launches it.
    from office import scoring
    cold.config = cold.policy(require_known_fallback=False)
    cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (dumps(cold.config),))
    real = scoring.evaluate_trust_state

    def unproven(con, triple):
        return (0, "valid-unverified") if "gpt-6.1-sol" not in triple else real(con, triple)

    def fallback_loses_its_proof(cold, decision):
        monkeypatch.setattr(scoring, "evaluate_trust_state", unproven)

    _between(cold, monkeypatch, fallback_loses_its_proof)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert not cold.trials() and d["triple"].startswith("codex@0/gpt-6-astra@")
    assert _route_json(d)["discovery"]["blocked"] == "no-fallback"
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]


# ------------------------------------------------------------------ a wave, and real concurrency

def test_two_dispatches_of_one_wave_cannot_both_take_the_runs_single_trial(cold):
    cold.dispatch("T1", "T2", parallel=True)
    d1, d2 = cold.dispatches()
    (trial,) = cold.trials()
    assert {d1["triple"] == trial["route"], d2["triple"] == trial["route"]} == {True, False}
    assert trial["dispatch_id"] in (d1["id"], d2["id"]) and len(_live_leases(cold, "T1")) == len(_live_leases(cold, "T2")) == 1
    assert [e["kind"] for e in cold.events() if e["kind"] == "trial-reserved"] == ["trial-reserved"]
    losing = d1 if d2["id"] == trial["dispatch_id"] else d2
    assert _route_json(losing)["discovery"]["blocked"] == "trial-cap"
    linked = {e["dispatch_id"]: e for e in cold.events() if e["kind"] == "dispatch-linked"}
    assert set(linked) == {d1["id"], d2["id"]} and linked[losing["id"]]["outcome"] == "fallback"
    assert len(cold.attempts()) == 2


def test_concurrent_dispatch_commands_race_for_one_trial_and_exactly_one_gets_it(cold, monkeypatch):
    import threading
    gate = threading.Barrier(2, timeout=45)
    real = dispatch.preflight_discovery

    arrived = []

    def together(con, run, task, decision, **kw):
        out = real(con, run, task, decision, **kw)
        if out.get("trial"):
            arrived.append(task["id"])
            gate.wait()  # both have a trial decision in hand before either opens its transaction
        return out

    monkeypatch.setattr(dispatch, "preflight_discovery", together)
    errors = []

    def command(task):
        con = db.connect()
        try:
            dispatch.dispatch(con, state.get_run(con, "run-A"), [task])
        except BaseException as exc:  # reported on the main thread
            errors.append(exc)
        finally:
            con.close()

    threads = [threading.Thread(target=command, args=(t,)) for t in ("T1", "T2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    assert sorted(arrived) == ["T1", "T2"] and not gate.broken  # the race was real: both held a trial decision
    (trial,) = cold.trials()
    ds = cold.dispatches()
    assert len(ds) == 2 and sum(d["triple"] == trial["route"] for d in ds) == 1
    assert sum(len(_live_leases(cold, t)) for t in ("T1", "T2")) == 2
    assert [e["kind"] for e in cold.events()].count("trial-reserved") == 1
    (loser,) = [d for d in ds if d["id"] != trial["dispatch_id"]]
    assert _route_json(loser)["discovery"]["blocked"] == "trial-cap" and loser["triple"].startswith("codex@0/gpt-6-astra@")
    assert [e["outcome"] for e in cold.events() if e["kind"] == "dispatch-linked" and e["dispatch_id"] == loser["id"]] == ["fallback"]


# ------------------------------------------------------------------ events only append

def test_events_only_append_across_the_whole_trial_lifecycle(cold):
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    before = _frozen(cold)
    run = state.get_run(cold.con, "run-A")
    dispatch._record_launch(run, d["id"], launcher="process", pid=None)
    after_launch = _frozen(cold)
    assert cold.kinds(attempt)[-1] == "trial-launched" and cold.trials()[0]["status"] == "launched"
    assert {k: v for k, v in after_launch.items() if k in before} == before
    with db.transaction(cold.con):
        dispatch.renew_lease(cold.con, d["lease_id"])  # submit's call, before any revision exists: nothing to settle
        assert cold.trials()[0]["status"] == "launched"
        _revision(cold, d)
        dispatch.renew_lease(cold.con, d["lease_id"])  # ... and in the transaction that captures the revision
        assert cold.trials()[0]["status"] == "submitted"
        assert not dispatch.trial_submitted(cold.con, d["id"])  # a status change happens once
        assert not dispatch.abandon_trial(cold.con, d["id"], "late")
    final = _frozen(cold)
    assert {k: v for k, v in final.items() if k in after_launch} == after_launch
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved", "trial-launched",
                                   "trial-submitted"]
    assert cold.trials()[0]["status"] == "submitted"
    assert [e["seq"] for e in cold.events(attempt)] == sorted(e["seq"] for e in cold.events(attempt))
    with pytest.raises(sqlite3.IntegrityError):
        cold.con.execute("UPDATE route_discovery_events SET detail='x'")
    with pytest.raises(sqlite3.IntegrityError):
        cold.con.execute("DELETE FROM route_discovery_events")
    assert _frozen(cold) == final


def test_revoking_a_trial_before_its_agent_starts_abandons_it(cold):
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    dispatch.revoke(cold.con, state.get_run(cold.con, "run-A"), "T1", "changed my mind")
    assert cold.trials()[0]["status"] == "abandoned"
    assert cold.kinds(attempt)[-1] == "trial-abandoned" and cold.events(attempt)[-1]["dispatch_id"] == d["id"]
    assert not _live_leases(cold)


# ------------------------------------------------------------------ routes a person or an approved plan chose

def test_a_recorded_route_is_never_traded_for_a_trial_or_probed(cold):
    pick = cold.decide("high")
    cand = pick["candidate"]
    with db.transaction(cold.con):
        cold.con.execute("UPDATE tasks SET route_json=? WHERE id='T1'", (dumps({"candidate": cand}),))
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"] == routing.candidate_id(cand) and not cold.trials() and not cold.events() and not cold.cache()
    assert _audit(cold)["dispatch"]["source"] == "plan" and _audit(cold)["discovery"]["blocked"] == "pinned-route"


def test_a_declared_route_is_dispatched_as_asked_without_a_probe_or_a_trial(cold):
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-A"), ["T1"], as_model=f"codex/{SOL}@high")
    (d,) = cold.dispatches()
    assert d["triple"] == cold.sol["high"] and not cold.trials() and not cold.events() and not cold.cache()
    assert json.loads(d["override_json"])["declared"] is True and "discovery" not in _route_json(d)


def test_an_external_or_stacked_dispatch_is_never_probed(cold):
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-A"), ["T1"], external=True)
    assert not cold.events() and not cold.trials()
    cold2 = cold.dispatches()
    assert len(cold2) == 1 and not cold.cache()


def test_a_task_stacked_behind_another_is_not_probed_for_a_launch_it_will_not_make_now(cold):
    cold.dispatch("T1", "T2")
    assert len(cold.attempts()) == 1 and len(cold.dispatches()) == 1 and len(cold.trials()) == 1
    assert cold.trials()[0]["task_id"] == "T1" and {e["task_id"] for e in cold.events()} == {"T1"}
    assert state.get_task(cold.con, "run-A", "T2")["status"] == "queued"


def test_a_trial_decision_that_will_not_launch_now_is_never_reserved(cold):
    run = state.get_run(cold.con, "run-A")
    task = state.get_task(cold.con, "run-A", "T1")
    decision = dispatch.preflight_discovery(cold.con, run, task, dispatch.planned_route(cold.con, run, task))
    assert decision.get("trial")
    with db.transaction(cold.con):
        stacked = dispatch.settle_discovery(cold.con, run, task, decision, launching=False)
    assert "trial_record" not in stacked and stacked["discovery_link"]["blocked"] == "not-launching"
    assert stacked["selected"] == decision["trial"]["fallback"]["selected"] and not cold.trials()




# ------------------------------------------------------------------ launch-failure recovery

import os
import sys
import threading
import time

from office import jobs
from office.util import atomic_write_json, pid_alive, process_start

_REAL_PROCESS_TABLE = dispatch._process_table  # tests patch dispatch._process_table; the fake workers' own checks do not

LEADER = r"""
import os, subprocess, sys, time
did, log, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
child = ("import sys, time\nend = time.time() + %s\nwhile time.time() < end:\n"
         "    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.05)\n" % secs)
kid = subprocess.Popen([sys.executable, '-c', child, log], start_new_session=True)
print(kid.pid, flush=True)
time.sleep(secs)
"""


class Tree:
    """A fake worker: a leader and a child in its own session, both tagged with the dispatch id, the child
    appending to `log` every 50 ms. Both live at most `secs` seconds whatever the test does."""

    def __init__(self, did, log, secs=30):
        env = {**os.environ, "OFFICE_DISPATCH_ID": did}
        self.leader = subprocess.Popen([sys.executable, "-c", LEADER, did, str(log), str(secs)], stdout=subprocess.PIPE,
                                       text=True, env=env, start_new_session=True)
        self.child = int(self.leader.stdout.readline())
        threading.Thread(target=self.leader.wait, daemon=True).start()  # reaps the leader the moment it dies

    def record(self, run_id, did):
        ddir = paths.run_dir(run_id) / "dispatches" / did
        ddir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(ddir / "agent.identity", {"pid": self.leader.pid, "start": process_start(self.leader.pid),
                                                    "c_start": dispatch._c_start(self.leader.pid)})
        (ddir / "agent.pgid").write_text(str(self.leader.pid))

    def alive(self):
        """Whether either process can still run: a zombie, killed and waiting for its parent, cannot write."""
        states = {row[0]: row[3] for row in _REAL_PROCESS_TABLE()}
        return any(pid in states and not states[pid].startswith("Z") for pid in (self.child, self.leader.pid))

    def close(self):
        for pid in (self.child, self.leader.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _until(check, seconds=15):
    """Wait for a fake worker's child to do what the test relies on, however loaded the machine is."""
    end = time.time() + seconds
    while not check():
        assert time.time() < end, "the fake worker never got going"
        time.sleep(0.05)


@pytest.fixture
def trees():
    made = []
    yield made
    for t in made:
        t.close()


def in_flight(cold):
    """T1 dispatched as a trial, its worktree created and its launch baseline taken: an agent about to run."""
    if _REAL_PROCESS_TABLE() is None or dispatch._c_start(os.getpid()) is None:
        pytest.skip("recovery reads the process table: it needs a readable `ps`")
    cold.dispatch("T1")
    d = cold.dispatches()[0]
    assert cold.trials()[0]["dispatch_id"] == d["id"]
    run = state.get_run(cold.con, "run-A")
    wt = dispatch.ensure_worktree(run, d)
    ddir = paths.run_dir("run-A") / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ddir / "worktree-baseline.json", dispatch._worktree_snapshot(wt))
    return d, wt, ddir


def launched(cold, d):
    dispatch._record_launch(state.get_run(cold.con, "run-A"), d["id"], launcher="process", pid=None)


def recover(cold, did, why="launch failed"):
    with db.transaction(cold.con):
        assert dispatch.trial_launch_failed(cold.con, state.get_run(cold.con, "run-A"), did, why)
    job = cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert jobs.execute(cold.con, job["id"]) == 0
    return state.get_job(cold.con, job["id"])


def task_row(cold, tid="T1"):
    return state.get_task(cold.con, "run-A", tid)


def test_a_trial_that_failed_before_any_work_falls_back_to_one_live_writer(cold):
    d, wt, ddir = in_flight(cold)
    trial = cold.trials()[0]
    launch_job = cold.con.execute("SELECT id FROM outbox WHERE kind='launch_agent'").fetchone()[0]
    recover(cold, d["id"])
    old, new = cold.dispatches()
    assert new["triple"] == trial["fallback_route"] and new["status"] == "launching" and new["worktree"] == d["worktree"]
    assert old["ended_at"] and old["status"] == "failed" and old["terminal_classification"] == "launch_failed"
    leases = cold.leases()
    assert [bool(l["released_at"]) for l in leases] == [True, False] and not any(l["revoked_at"] for l in leases)
    assert _live_leases(cold)[0]["id"] == new["lease_id"] != old["lease_id"]
    assert cold.trials()[0]["status"] == "fell-back" and task_row(cold)["current_dispatch_id"] == new["id"]
    assert task_row(cold)["status"] == "launching"
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved",
                                   "trial-launch-failed", "trial-fell-back"]
    failed, fell = cold.events(attempt)[-2:]
    assert failed["dispatch_id"] == old["id"] and fell["dispatch_id"] == new["id"] and fell["origin"] == "recovery"
    assert json.loads(cold.trials()[0]["outcome"])["fallback_dispatch"] == new["id"]
    # the failed dispatch's own launch job can no longer start a second agent in the same worktree
    job = state.get_job(cold.con, launch_job)
    assert dispatch.job_launch_agent(cold.con, state.get_run(cold.con, "run-A"), job) == {"skipped": "failed"}
    # the route change is on the record, and the fallback is a queued launch, not a started agent
    changes = state.route_changes(cold.con, "run-A", "T1")
    assert changes[-1]["kind"] == "trial-fallback" and changes[-1]["after"] == trial["fallback_route"]
    assert cold.con.execute("SELECT COUNT(*) FROM outbox WHERE kind='launch_agent' AND status='queued'").fetchone()[0] == 2


def test_a_child_still_able_to_write_is_terminated_before_the_fallback_starts(cold, trees, monkeypatch):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    log = cold.tmp / "child.log"
    tree = Tree(d["id"], log)
    trees.append(tree)
    tree.record("run-A", d["id"])
    _until(lambda: log.exists() and log.stat().st_size > 0)
    assert tree.alive()
    seen = {}
    real = dispatch.request_launch

    def spy(*a, **kw):
        seen["alive_when_the_fallback_launches"] = tree.alive()
        return real(*a, **kw)

    monkeypatch.setattr(dispatch, "request_launch", spy)
    recover(cold, d["id"])
    assert seen == {"alive_when_the_fallback_launches": False}
    size = log.stat().st_size
    time.sleep(0.4)
    assert log.stat().st_size == size  # nothing is writing any more
    assert cold.trials()[0]["status"] == "fell-back" and len(_live_leases(cold)) == 1


def test_untracked_meaningful_work_is_preserved_and_blocks_the_fallback(cold, trees):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    work = wt / "new_module.py"
    tree = Tree(d["id"], work)  # the worker's child is writing a new, untracked file in the worktree
    trees.append(tree)
    tree.record("run-A", d["id"])
    _until(lambda: work.exists() and work.stat().st_size > 0)
    recover(cold, d["id"])
    assert not tree.alive() and work.exists() and work.stat().st_size > 0
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "abandoned"
    assert [bool(l["released_at"] or l["revoked_at"]) for l in cold.leases()] == [False]  # the lease stays with the work
    assert cold.dispatches()[0]["ended_at"] and cold.dispatches()[0]["status"] == "failed"  # no phantom live session
    task = task_row(cold)
    assert task["status"] == "blocked" and "work had started" in task["pause_reason"] and "new_module.py" in task["pause_reason"]
    blocked = [e for e in cold.con.execute("SELECT summary FROM events WHERE kind='task.blocked'")][-1][0]
    assert "no fallback was started" in blocked and "office rerun T1 --fresh" in blocked and d["worktree"] in blocked
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt)[-1] == "trial-abandoned" and "trial-fell-back" not in cold.kinds(attempt)


@pytest.mark.parametrize("claimed_gone", [False, True])
def test_a_worker_that_survives_the_termination_blocks_the_fallback_without_moving_the_lease(cold, trees, monkeypatch, claimed_gone):
    # False: the termination itself reports failure. True: it claims success while the worker is still running,
    # so only the final scan stands between the fallback and a second writer.
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    tree = Tree(d["id"], cold.tmp / "child.log")
    trees.append(tree)
    tree.record("run-A", d["id"])
    monkeypatch.setattr(dispatch, "_terminate_worker", lambda *a, **kw: claimed_gone)  # it ignored every signal
    lease = _live_leases(cold)[0]["id"]
    recover(cold, d["id"])
    assert tree.alive()
    assert not cold.dispatches()[0]["ended_at"]  # its worker may still be running: the record says so
    assert len(cold.dispatches()) == 1 and [l["id"] for l in _live_leases(cold)] == [lease] == [d["lease_id"]]
    assert cold.trials()[0]["status"] == "abandoned"
    task = task_row(cold)
    assert task["status"] == "blocked" and "could not be confirmed gone" in task["pause_reason"]
    summary = [e for e in cold.con.execute("SELECT summary FROM events WHERE kind='task.blocked'")][-1][0]
    assert "Stop the process by hand" in summary and "office rerun T1 --fresh" in summary and lease in summary


@pytest.mark.parametrize("what", ["identity", "process-table"])
def test_an_unconfirmable_identity_or_process_table_blocks_the_fallback(cold, trees, monkeypatch, what):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    tree = Tree(d["id"], cold.tmp / "child.log")
    trees.append(tree)
    tree.record("run-A", d["id"])
    if what == "identity":
        monkeypatch.setattr(dispatch, "_c_start", lambda pid: None)  # ps cannot say which process this is
    else:
        monkeypatch.setattr(dispatch, "_process_table", lambda: None)
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and len(_live_leases(cold)) == 1 and task_row(cold)["status"] == "blocked"
    assert tree.alive() or what == "process-table"  # an unreadable identity is never signalled
    assert cold.trials()[0]["status"] == "abandoned"


def test_a_commit_or_a_changed_tracked_file_is_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(wt), "add", "mod.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "w"], check=True)
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "branch moved" in task_row(cold)["pause_reason"]


def test_an_ignored_generated_file_alone_is_not_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / ".git" / "info" / "exclude").write_text(".office/\n") if False else None
    exclude = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"], check=True, capture_output=True,
                             text=True).stdout.strip()
    with open(wt / exclude if not os.path.isabs(exclude) else exclude, "a") as fh:
        fh.write("generated/\n")
    (wt / "generated").mkdir()
    (wt / "generated" / "cache.bin").write_text("x")
    recover(cold, d["id"])
    assert cold.trials()[0]["status"] == "fell-back" and len(cold.dispatches()) == 2


def test_a_submitted_revision_is_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, "
                         "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                         "VALUES('R1','run-A','T1',1,?,?,?,1,3,3,'e','op1','submitted',?)", (d["id"], "a" * 40, "b" * 40, now_iso()))
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "a revision was submitted" in task_row(cold)["pause_reason"]


# ------------------------------------------------------------------ what starts a recovery, and what never does

def _kinds(cold):
    return [r[0] for r in cold.con.execute("SELECT kind FROM outbox ORDER BY created_at, rowid")]


@pytest.mark.parametrize("classification,code", [("launch_failed", 127), ("nonzero", 1), ("supervisor_error", None)])
def test_a_trial_worker_that_ends_without_submitting_is_recovered_not_relaunched_on_the_same_route(cold, classification, code):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    dispatch._finish(d["id"], code, None, classification, 0.1)
    assert _kinds(cold).count("trial_recovery") == 1 and _kinds(cold).count("launch_agent") == 1  # no same-route relaunch
    assert task_row(cold)["status"] == "running" and len(cold.dispatches()) == 1
    assert "recovery" in json.loads(cold.trials()[0]["outcome"]) and cold.trials()[0]["status"] == "launched"
    job = cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert jobs.execute(cold.con, job["id"]) == 0
    assert cold.trials()[0]["status"] == "fell-back" and len(cold.dispatches()) == 2
    assert cold.dispatches()[1]["triple"] == cold.trials()[0]["fallback_route"] and len(_live_leases(cold)) == 1


def test_a_trial_worker_that_hit_a_quota_wall_before_any_work_falls_back(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    monkeypatch.setattr(dispatch, "_quota_wall", lambda run, dd: "you have hit your usage limit")
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)
    assert _kinds(cold).count("trial_recovery") == 1 and task_row(cold)["status"] == "running"


def test_a_trial_worker_that_submitted_settles_the_trial_without_a_recovery(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    with db.transaction(cold.con):
        cold.con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, "
                         "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                         "VALUES('R1','run-A','T1',1,?,?,?,1,3,3,'e','op1','submitted',?)", (d["id"], "a" * 40, "b" * 40, now_iso()))
    dispatch._finish(d["id"], 0, None, "success", 0.1)
    assert "trial_recovery" not in _kinds(cold) and cold.trials()[0]["status"] == "submitted"
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt)[-1] == "trial-submitted"


def test_a_trial_worker_that_ended_on_a_signal_is_abandoned_when_the_task_moved_on(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    with db.transaction(cold.con):
        state.update_task(cold.con, "run-A", "T1", status="paused", pause_reason="lease revoked: x")
    dispatch._finish(d["id"], None, 15, "signal", 0.1)
    assert "trial_recovery" not in _kinds(cold) and cold.trials()[0]["status"] == "abandoned"


def test_the_launch_job_failing_for_good_recovers_a_trial_and_still_blocks_an_ordinary_dispatch(cold):
    d, wt, ddir = in_flight(cold)
    run = state.get_run(cold.con, "run-A")
    job = state.get_job(cold.con, cold.con.execute("SELECT id FROM outbox WHERE kind='launch_agent'").fetchone()[0])
    with db.transaction(cold.con):
        jobs.on_permanent_failure(cold.con, run, job, "RuntimeError: worktree setup exploded")
    assert _kinds(cold).count("trial_recovery") == 1 and task_row(cold)["status"] == "launching"
    # an ordinary dispatch keeps today's behavior: the task blocks
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-A"), ["T2"], as_model="codex/gpt-6-astra@low")
    plain = [x for x in cold.dispatches() if x["task_id"] == "T2"][0]
    plain_job = state.get_job(cold.con, cold.con.execute(
        "SELECT id FROM outbox WHERE kind='launch_agent' AND dedup_key=?", (f"launch:{plain['id']}",)).fetchone()[0])
    with db.transaction(cold.con):
        jobs.on_permanent_failure(cold.con, state.get_run(cold.con, "run-A"), plain_job, "RuntimeError: boom")
    assert task_row(cold, "T2")["status"] == "blocked" and "launch failed" in task_row(cold, "T2")["pause_reason"]
    assert _kinds(cold).count("trial_recovery") == 1


def test_a_declared_route_that_fails_surfaces_as_it_does_today(cold):
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-A"), ["T1"], as_model=f"codex/{SOL}@high")
    (d,) = cold.dispatches()
    launched(cold, d)
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)
    assert "trial_recovery" not in _kinds(cold) and not cold.trials()
    relaunch = cold.dispatches()[-1]  # the existing environment-retry relaunch, on the route the user declared
    assert relaunch["id"] != d["id"] and relaunch["triple"] == cold.sol["high"]


def test_amending_a_trial_dispatchs_route_ends_the_trial_and_its_fallback(cold):
    from office import routechange
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    routechange.change_route(cold.con, state.get_run(cold.con, "run-A"), "T1", "codex/gpt-6-astra@high", "switch it")
    assert cold.trials()[0]["status"] == "abandoned"
    assert "discovery" not in _route_json(cold.dispatches()[0]) and json.loads(task_row(cold)["route_json"])["declared"]
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt)[-1] == "trial-abandoned"
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)  # its failure is the user's route failing
    assert "trial_recovery" not in _kinds(cold)


# ------------------------------------------------------------------ gates and a fallback that no longer holds

def test_a_gate_that_closes_during_the_recovery_blocks_the_fallback_and_keeps_the_lease(cold, monkeypatch):
    from office.state import Refused
    d, wt, ddir = in_flight(cold)

    def closed(con, run):
        raise Refused("plan-review-pending", "the plan is being reviewed again")

    monkeypatch.setattr(plans, "require_dispatchable", closed)
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and [l["id"] for l in _live_leases(cold)] == [d["lease_id"]]
    assert cold.trials()[0]["status"] == "launch-failed" and task_row(cold)["status"] == "blocked"
    assert "a gate no longer allows the fallback" in task_row(cold)["pause_reason"]


def test_a_task_the_operator_paused_gets_no_fallback(cold):
    from office import queuecmd
    d, wt, ddir = in_flight(cold)
    queuecmd.pause(cold.con, run_arg="run-A", task="T1", reason="hold")
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "paused by the operator" in task_row(cold)["pause_reason"]


def test_a_fallback_that_no_longer_qualifies_blocks_with_a_next_step(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    cold.monkeypatch.setenv("OFFICE_QUOTA_FIXTURE", json.dumps({"codex": 1}))
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "launch-failed"
    assert "no longer qualifies" in task_row(cold)["pause_reason"]
    summary = [e for e in cold.con.execute("SELECT summary FROM events WHERE kind='task.blocked'")][-1][0]
    assert "office dispatch T1 --reroute" in summary


def test_a_recovery_that_itself_fails_blocks_with_its_reason(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    monkeypatch.setattr(dispatch, "stop_worker_tree", lambda run, dd: (_ for _ in ()).throw(RuntimeError("ps exploded")))
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and task_row(cold)["status"] == "blocked"
    assert "the recovery itself failed" in task_row(cold)["pause_reason"] and cold.trials()[0]["status"] == "abandoned"


def test_a_recovery_for_a_task_that_moved_on_starts_nothing(cold):
    d, wt, ddir = in_flight(cold)
    with db.transaction(cold.con):
        state.update_task(cold.con, "run-A", "T1", status="paused", pause_reason="lease revoked: x")
        assert dispatch._queue_trial_recovery(cold.con, state.get_run(cold.con, "run-A"), d, "late")
    job = cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert jobs.execute(cold.con, job["id"]) == 0
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "abandoned"


# ------------------------------------------------------------------ through the CLI (integration tier: runs with --all)

from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE

TESTS = Path(__file__).resolve().parents[1]


def _discovery_env(env, monkeypatch, **probe):
    """The Env's fake `codex` answers a route probe (it is launched with a probe nonce, or asked its version) like
    the probe suites' fake harness, and anything else like the Env's scripted agent: one binary serves the probe,
    the trial worker and the fallback worker."""
    for other in ("claude", "gemini", "agy"):  # one harness: the draw can only land on a route the fake can probe
        (env.bin / other).unlink()
        env.fakes.pop(env.bin / other, None)
    codex = env.bin / "codex"
    codex.write_text("\n".join([
        f"#!{sys.executable}",
        "import os, runpy, sys",
        "if os.environ.get('OFFICE_PROBE_NONCE') or sys.argv[1:] == ['--version']:",
        f"    runpy.run_path({str(TESTS / 'fixtures' / 'route_probe' / 'fake_harness.py')!r}, run_name='__main__')",
        "os.environ['FAKE_HARNESS'] = 'codex'",
        f"runpy.run_path({str(TESTS / 'v31' / 'fake_agent.py')!r}, run_name='__main__')", ""]))
    codex.chmod(0o755)
    monkeypatch.setenv("FAKE_PROBE", json.dumps(probe))
    # No exploration draw (it would, at random, hold the discovery draw back), and a window that admits one trial.
    (env.tmp / "user-config.yaml").write_text("\n".join([
        "routing:", "  discovery:", "    enabled: true", "    max_trial_percent_rolling_20: 100",
        "  adaptive:", "    exploration: {rate: 0.0, margin: 1.0, max_cost_vs_primary_percent: 100000}", ""]))
    if route_probe.write_boundary_reason() is not None:
        monkeypatch.setattr(route_probe, "write_boundary_reason", lambda: None)
        monkeypatch.setattr(route_probe, "_boundary_argv", lambda ws: [])


def _cli_run(env, monkeypatch, *steps, **probe):
    _discovery_env(env, monkeypatch, **probe)
    env.trust()
    env.script(executor=list(steps))
    from conftest import start_inline
    start_inline(env, extra=("--size-class", "S"))
    env.office("approve", "plan", "--quote", "approved", check=0)


def test_cli_cold_start_trial_worker_fails_to_start_and_the_fallback_worker_submits(env, monkeypatch):
    _cli_run(env, monkeypatch, {"exit": 127, "stderr": "codex: model unavailable"},
             {"write": {"calc.py": GOOD_ADD}, "submit": True})
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    trial = dict(con.execute("SELECT * FROM route_trials").fetchone())
    dispatches = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE role='executor' ORDER BY started_at, rowid")]
    assert [x["triple"] for x in dispatches] == [trial["route"], trial["fallback_route"]], out
    first, second = dispatches
    assert first["terminal_classification"] == "nonzero" and first["ended_at"]
    assert trial["status"] == "fell-back" and json.loads(trial["outcome"])["fallback_dispatch"] == second["id"]
    leases = [dict(r) for r in con.execute("SELECT * FROM leases WHERE task_id='T1' ORDER BY acquired_at, rowid")]
    # one writer at a time: the trial's lease was released before the fallback's was acquired, and neither was revoked
    assert len(leases) == 2 and not any(l["revoked_at"] for l in leases)
    assert leases[0]["released_at"] <= leases[1]["acquired_at"] and leases[0]["id"] == first["lease_id"]
    assert con.execute("SELECT dispatch_id FROM revisions WHERE task_id='T1'").fetchone()[0] == second["id"]
    kinds = [r[0] for r in con.execute("SELECT kind FROM route_discovery_events WHERE attempt_id=? ORDER BY seq", (trial["id"],))]
    assert kinds == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved", "trial-launched",
                     "trial-launch-failed", "trial-fell-back"]
    assert not con.execute("SELECT 1 FROM adapter_trust_acts WHERE triple=?", (trial["route"],)).fetchone()
    assert env.calls() and any(c.get("role") == "executor" for c in env.calls())


def test_cli_cold_start_probe_failure_dispatches_the_known_working_route_in_the_same_command(env, monkeypatch):
    _cli_run(env, monkeypatch, {"write": {"calc.py": GOOD_ADD}, "submit": True}, mode="unsupported_effort")
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    assert not con.execute("SELECT 1 FROM route_trials").fetchone()
    (d,) = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE role='executor'")]
    assert "gpt-6.1-sol" not in d["triple"] and con.execute("SELECT COUNT(*) FROM leases WHERE task_id='T1'").fetchone()[0] == 1
    probe = dict(con.execute("SELECT * FROM route_probes").fetchone())
    assert (probe["result"], probe["reason_class"]) == ("fail", "unsupported-model-effort")
    kinds = [r[0] for r in con.execute("SELECT kind FROM route_discovery_events ORDER BY seq")]
    assert kinds == ["probe-reserved", "probe-result", "dispatch-linked"]


def test_cli_cold_start_trial_worker_submits_and_the_trial_settles_as_submitted(env, monkeypatch):
    _cli_run(env, monkeypatch, {"write": {"calc.py": GOOD_ADD}, "submit": True})
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    trial = dict(con.execute("SELECT * FROM route_trials").fetchone())
    (d,) = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE role='executor'")]
    assert d["triple"] == trial["route"] and trial["status"] == "submitted"
    kinds = [r[0] for r in con.execute("SELECT kind FROM route_discovery_events WHERE attempt_id=? ORDER BY seq", (trial["id"],))]
    assert kinds == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved", "trial-launched", "trial-submitted"]
    assert not con.execute("SELECT 1 FROM adapter_trust_acts WHERE triple=?", (trial["route"],)).fetchone()


def test_a_supervisor_that_is_this_process_is_never_the_worker_and_its_other_children_are_left_alone(cold, trees):
    # `OFFICE_LAUNCHER=sync` supervises in the dispatching process, so the recorded supervisor identity is the
    # process running the recovery. Neither it nor anything it started for another purpose is the worker.
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    threading.Thread(target=bystander.wait, daemon=True).start()
    try:
        ident = paths.run_dir("run-A") / "dispatches" / d["id"] / "supervisor.identity"
        ident.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(ident, {"pid": os.getpid(), "start": process_start(os.getpid()), "c_start": dispatch._c_start(os.getpid())})
        recover(cold, d["id"])
        assert bystander.poll() is None
        assert cold.trials()[0]["status"] == "fell-back" and len(_live_leases(cold)) == 1
    finally:
        bystander.kill()


def test_a_symlink_in_the_worktree_is_compared_by_its_link_text_and_never_followed(cold, tmp_path):
    d, wt, ddir = in_flight(cold)
    outside = tmp_path / "outside.bin"
    outside.write_text("one")
    (wt / "alias").symlink_to(outside)
    before = dispatch._worktree_snapshot(wt)
    assert before["files"]["alias"] == f"link:{outside}"
    outside.write_text("two")  # the target changes; the worktree did not
    assert dispatch._worktree_snapshot(wt) == before
    (wt / "alias").unlink()
    (wt / "alias").symlink_to(tmp_path / "elsewhere")
    assert dispatch._worktree_snapshot(wt) != before


def test_the_snapshot_reads_the_failed_agents_worktree_without_its_fsmonitor_hook(cold, tmp_path):
    d, wt, ddir = in_flight(cold)
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(wt), "config", "core.fsmonitor", str(hook)], check=True)
    assert dispatch._worktree_snapshot(wt) is not None
    assert not marker.exists()


def test_a_dispatch_with_no_worktree_is_pre_work_and_never_reads_the_current_directory(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    monkeypatch.setattr(dispatch, "_worktree_snapshot", lambda path: pytest.fail(f"snapshotted {path}"))
    for missing in (None, "", str(cold.tmp / "no-such-worktree")):
        assert dispatch.work_started(cold.con, {**d, "worktree": missing}, ddir) == (False, "the dispatch never got a worktree")


def test_a_fallback_that_is_the_trial_route_or_is_not_selected_is_no_fallback(cold):
    run = state.get_run(cold.con, "run-A")
    task = state.get_task(cold.con, "run-A", "T1")
    decision = dispatch.preflight_discovery(cold.con, run, task, dispatch.planned_route(cold.con, run, task))
    trial = decision["trial"]
    own = {k: v for k, v in decision.items() if k != "trial"}
    with db.transaction(cold.con):
        assert dispatch._trial_blocked(cold.con, run, decision, trial["fallback"]) is None
        assert dispatch._trial_blocked(cold.con, run, decision, own) == "no-fallback"
        assert dispatch._trial_blocked(cold.con, run, decision, {"status": "slate_exhausted"}) == "no-fallback"


def test_when_the_trial_route_is_the_only_one_that_qualifies_no_trial_is_taken(cold, monkeypatch):
    real = dispatch.planned_route

    def nothing_else(con, run, task, **kw):
        if kw.get("exclude"):  # the preflight asks for the known-working route without the trial route
            return {"status": "no_qualifying_candidate", "selected": None, "rejected": []}
        return real(con, run, task, **kw)

    monkeypatch.setattr(dispatch, "planned_route", nothing_else)
    run = state.get_run(cold.con, "run-A")
    task = state.get_task(cold.con, "run-A", "T1")
    out = dispatch.preflight_discovery(cold.con, run, task, real(cold.con, run, task))
    assert "trial" not in out and out["status"] == "no_qualifying_candidate"
    assert out["discovery_link"]["blocked"] == "no-fallback" and not cold.trials()


def test_the_probe_runs_before_any_dispatch_lease_session_or_worktree_exists_and_outside_a_transaction(cold, monkeypatch):
    seen = {}
    real = route_probe.ensure

    def watching(con, run, cand, **kw):
        seen.update({t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("dispatches", "leases", "route_trials", "session_bindings", "outbox")})
        seen["in_transaction"] = con.in_transaction
        seen["worktrees"] = list(paths.worktrees_dir().glob("*/*")) if paths.worktrees_dir().exists() else []
        seen["attempt"] = kw["attempt_id"]
        return real(con, run, cand, **kw)

    monkeypatch.setattr(route_probe, "ensure", watching)
    cold.dispatch("T1")
    assert seen.pop("in_transaction") is False and seen.pop("worktrees") == []
    assert {k: v for k, v in seen.items() if k != "attempt"} == {t: 0 for t in
                                                                   ("dispatches", "leases", "route_trials", "session_bindings", "outbox")}
    assert cold.attempts() == [seen["attempt"]]  # the attempt id was minted before the call and is the one linked later


def test_only_the_recomputed_decision_is_dispatched(cold, monkeypatch):
    # After the probe, the original primary and the trial route are both denied. The recompute names another route,
    # and that route (not the decision made before the probe) is what runs.
    first = {}
    real = route_probe.ensure

    def then_denied(con, run, cand, **kw):
        first["primary"] = kw["context"]["primary_route"]
        out = real(con, run, cand, **kw)
        cold.write_policy("user", denied=[SOL, "codex/gpt-6-astra@low"])
        return out

    monkeypatch.setattr(route_probe, "ensure", then_denied)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert first["primary"] == "codex@0/gpt-6-astra@low" and d["triple"] != first["primary"]
    assert "gpt-6.1-sol" not in d["triple"] and not cold.trials()
    assert _route_json(d)["candidate"]["effort"] == d["effort"] and d["route_json"]


def test_a_probe_that_raises_costs_the_dispatch_nothing(cold, monkeypatch):
    def broken(con, run, cand, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(route_probe, "ensure", broken)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith("codex@0/gpt-6-astra@") and not cold.trials()
    assert _route_json(d)["discovery"]["blocked"] == "probe-error: OperationalError"


# ------------------------------------------------------------------ one transaction

def test_the_trial_row_and_its_events_commit_with_the_dispatch_row_or_not_at_all(cold, monkeypatch):
    real = dispatch.record_discovery

    def then_fails(*a, **kw):
        real(*a, **kw)  # the trial row and both events are written ...
        raise RuntimeError("the transaction dies after them")

    monkeypatch.setattr(dispatch, "record_discovery", then_fails)
    with pytest.raises(RuntimeError):
        cold.dispatch("T1")
    # ... and none of it, nor the dispatch row or its lease, survives. Only the probe's own events (committed earlier) do.
    assert not cold.trials() and not cold.dispatches() and not cold.leases()
    assert {e["kind"] for e in cold.events()} <= {"probe-reserved", "probe-result"}


def test_a_trial_status_and_its_event_commit_together_or_not_at_all(cold):
    cold.dispatch("T1")
    d = cold.dispatches()[0]
    before = _frozen(cold)
    with pytest.raises(RuntimeError):
        with db.transaction(cold.con):
            assert dispatch.set_trial_status(cold.con, cold.trials()[0], "launched", detail="x")
            raise RuntimeError("rolled back")
    assert cold.trials()[0]["status"] == "reserved" and _frozen(cold) == before


# ------------------------------------------------------------------ what counts as work

def test_a_launch_with_no_baseline_cannot_rule_work_out(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (ddir / "worktree-baseline.json").unlink()
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "no launch baseline" in task_row(cold)["pause_reason"]


def test_a_worktree_that_never_settles_cannot_rule_work_out(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    real = dispatch._worktree_snapshot
    baseline = (ddir / "worktree-baseline.json").read_text()
    monkeypatch.setattr(dispatch, "_worktree_snapshot", lambda path: None)  # a writer keeps changing it
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "could not be snapshotted" in task_row(cold)["pause_reason"]
    assert (ddir / "worktree-baseline.json").read_text() == baseline and real is not None


def _git(wt, *args):
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


def test_an_edit_to_a_tracked_file_is_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "tracked.txt").write_text("one\n")
    _git(wt, "add", "tracked.txt")
    _git(wt, "commit", "-qm", "tracked")  # part of the launch state, not the worker's work:
    atomic_write_json(ddir / "worktree-baseline.json", dispatch._worktree_snapshot(wt))
    (wt / "tracked.txt").write_text("two\n")  # the worker's uncommitted edit
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "tracked.txt" in task_row(cold)["pause_reason"]


def test_a_new_untracked_directory_or_a_staged_file_is_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "pkg" / "sub").mkdir(parents=True)
    (wt / "pkg" / "sub" / "mod.py").write_text("x = 1\n")
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "pkg/sub/mod.py" in task_row(cold)["pause_reason"]


def test_a_staged_file_alone_is_work(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "a.txt").write_text("x\n")
    _git(wt, "add", "a.txt")
    (wt / "a.txt").unlink()  # staged, then gone from the tree: only the index remembers it
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and "the index" in task_row(cold)["pause_reason"]


def test_the_failed_dispatchs_session_binding_ends_with_a_fallback_and_only_then(cold):
    def bound(cold):
        d, wt, ddir = in_flight(cold)
        with db.transaction(cold.con):
            cold.con.execute("UPDATE dispatches SET session_id='sess-1', harness='codex' WHERE id=?", (d["id"],))
            cold.con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) "
                             "VALUES('codex','sess-1','run-A',?,'x'), ('codex','sess-other','run-A',?,'x')",
                             (now_iso(), now_iso()))
        return d

    d = bound(cold)
    recover(cold, d["id"])
    ended = {r["session_id"]: r["ended_at"] for r in cold.con.execute("SELECT * FROM session_bindings")}
    assert ended["sess-1"] and ended["sess-other"] is None


def test_a_blocked_recovery_leaves_the_session_binding_alone(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "new.py").write_text("x\n")
    with db.transaction(cold.con):
        cold.con.execute("UPDATE dispatches SET session_id='sess-1', harness='codex' WHERE id=?", (d["id"],))
        cold.con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) "
                         "VALUES('codex','sess-1','run-A',?,'x')", (now_iso(),))
    recover(cold, d["id"])
    assert cold.con.execute("SELECT ended_at FROM session_bindings").fetchone()[0] is None


# ------------------------------------------------------------------ a recovery that cannot finish

def test_a_recovery_job_that_fails_for_good_blocks_the_task_and_settles_the_trial(cold):
    d, wt, ddir = in_flight(cold)
    with db.transaction(cold.con):
        assert dispatch._queue_trial_recovery(cold.con, state.get_run(cold.con, "run-A"), d, "worker died")
    job = state.get_job(cold.con, cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()[0])
    with db.transaction(cold.con):
        jobs.on_permanent_failure(cold.con, state.get_run(cold.con, "run-A"), job, "worker process died twice")
    assert task_row(cold)["status"] == "blocked" and "the recovery itself failed" in task_row(cold)["pause_reason"]
    assert cold.trials()[0]["status"] == "abandoned" and len(_live_leases(cold)) == 1
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt)[-1] == "trial-abandoned"


def test_a_recovery_that_crashes_after_the_worker_is_gone_ends_the_dispatch(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    monkeypatch.setattr(dispatch, "request_launch", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("scope-held")))
    recover(cold, d["id"])
    assert cold.dispatches()[0]["ended_at"] and len(cold.dispatches()) == 1 and task_row(cold)["status"] == "blocked"
    assert [bool(l["released_at"]) for l in cold.leases()] == [False]  # the rolled-back transaction moved nothing


def test_the_task_is_not_announced_blocked_when_the_recovery_did_not_block_it(cold):
    d, wt, ddir = in_flight(cold)
    with db.transaction(cold.con):
        state.update_task(cold.con, "run-A", "T1", status="paused", pause_reason="lease revoked: x")
    before = cold.con.execute("SELECT COUNT(*) FROM events WHERE kind='task.blocked'").fetchone()[0]
    dispatch._recovery_blocked(cold.con, state.get_run(cold.con, "run-A"), d, cold.trials()[0], "abandoned", "x", "y")
    assert cold.con.execute("SELECT COUNT(*) FROM events WHERE kind='task.blocked'").fetchone()[0] == before
    assert task_row(cold)["status"] == "paused"


# ------------------------------------------------------------------ gates and a quota wall

def test_a_quota_wall_before_any_work_falls_back_and_after_work_blocks(cold, monkeypatch):
    monkeypatch.setattr(dispatch, "_quota_wall", lambda run, dd: "you have hit your usage limit")
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)
    job = cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert jobs.execute(cold.con, job["id"]) == 0
    assert cold.trials()[0]["status"] == "fell-back" and len(cold.dispatches()) == 2


def test_a_quota_wall_after_work_started_blocks(cold, monkeypatch):
    monkeypatch.setattr(dispatch, "_quota_wall", lambda run, dd: "you have hit your usage limit")
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    (wt / "half_done.py").write_text("x = 1\n")
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)
    job = cold.con.execute("SELECT id FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert jobs.execute(cold.con, job["id"]) == 0
    assert len(cold.dispatches()) == 1 and "half_done.py" in task_row(cold)["pause_reason"]


@pytest.mark.parametrize("what", ["scope", "terminal", "lease"])
def test_each_gate_is_rechecked_before_the_fallback_lease(cold, monkeypatch, what):
    from office.state import Refused
    d, wt, ddir = in_flight(cold)
    if what == "scope":
        monkeypatch.setattr(plans, "require_scope_clear", lambda con, run, tid: (_ for _ in ()).throw(Refused("plan-defect", "open defect")))
    elif what == "terminal":
        with db.transaction(cold.con):
            cold.con.execute("UPDATE runs SET phase='closed' WHERE id='run-A'")
    else:
        with db.transaction(cold.con):
            cold.con.execute("UPDATE leases SET revoked_at=?, revoke_reason='x' WHERE id=?", (now_iso(), d["lease_id"]))
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "launch-failed"
    assert [l["released_at"] for l in cold.leases()] == [None]  # no lease was released or handed on
    assert task_row(cold)["status"] == "blocked" and "a gate no longer allows the fallback" in task_row(cold)["pause_reason"]
    assert cold.dispatches()[0]["ended_at"]  # the worker is gone: no phantom live session


def _orphan_in(wt, secs=25):
    """A process in `wt` that nothing here started: its parent has exited, so it belongs to init. No dispatch tag."""
    code = ("import subprocess, sys; k = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%d)'], cwd=%r, "
            "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "print(k.pid, flush=True)" % (secs, str(wt)))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if not k.startswith("OFFICE_")}).stdout
    return int(out)


def test_a_helper_with_no_dispatch_tag_still_working_in_the_worktree_blocks_the_fallback(cold):
    # A worker that cleared its environment and left its group carries nothing that names the dispatch. It is
    # still in the worktree, so it may be writing there: the exit is not confirmed, and it is not signalled.
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    helper = _orphan_in(wt)
    try:
        recover(cold, d["id"])
        assert pid_alive(helper)
        assert len(cold.dispatches()) == 1 and [l["id"] for l in _live_leases(cold)] == [d["lease_id"]]
        assert "unattributed working directory" in task_row(cold)["pause_reason"] and str(helper) in task_row(cold)["pause_reason"]
        assert not cold.dispatches()[0]["ended_at"] and cold.trials()[0]["status"] == "abandoned"
    finally:
        os.kill(helper, 9)


def test_what_this_process_started_in_the_worktree_is_not_a_holder(cold):
    # A supervisor that works from the worktree gives the `lsof` and `git` it runs that directory.
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], cwd=wt)
    threading.Thread(target=mine.wait, daemon=True).start()
    try:
        assert mine.pid in dispatch._cwd_holders(wt)
        recover(cold, d["id"])
        assert cold.trials()[0]["status"] == "fell-back"
    finally:
        mine.kill()


def test_a_killed_worker_that_is_waiting_to_be_reaped_does_not_count_as_running(cold):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    zombie = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(25)"], start_new_session=True,
                              env={**os.environ, "OFFICE_DISPATCH_ID": d["id"]})
    ident = paths.run_dir("run-A") / "dispatches" / d["id"]
    ident.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ident / "agent.identity", {"pid": zombie.pid, "start": process_start(zombie.pid), "c_start": dispatch._c_start(zombie.pid)})
    (ident / "agent.pgid").write_text(str(zombie.pid))
    try:
        recover(cold, d["id"])  # this test never waits for the process, so once killed it stays a zombie
        states = {row[0]: row[3] for row in _REAL_PROCESS_TABLE()}
        assert states[zombie.pid].startswith("Z")
        assert cold.trials()[0]["status"] == "fell-back" and len(cold.dispatches()) == 2
    finally:
        zombie.kill()
        zombie.wait()


# ------------------------------------------------------------------ round 2

def test_a_dispatch_that_never_launched_has_no_work_whatever_its_worktree_holds(cold):
    d, wt, ddir = in_flight(cold)
    (ddir / "worktree-baseline.json").unlink()  # the launch job failed after the worktree and before the baseline
    (wt / "tracked.txt").write_text("x\n")
    _git(wt, "add", "tracked.txt")
    _git(wt, "commit", "-qm", "earlier dispatch's work")
    assert dispatch.work_started(cold.con, state.get_dispatch(cold.con, d["id"]), ddir) == (
        False, "no agent was ever launched in this dispatch")


def test_a_tagless_member_of_the_agents_group_that_ignores_sigterm_is_still_stopped(cold, trees):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    # leader (tagged, recorded) and a member of its group with no tag that ignores SIGTERM: killing the leader
    # leaves it reparented but still in the group
    kid_code = ("import os, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "time.sleep(25)\n")
    # the member's parent exits at once, so it is reparented to init: only its group (and its working directory)
    # connect it to the worker. It has no dispatch tag.
    middle = ("import os, subprocess, sys\n"
              "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
              "                 stderr=subprocess.DEVNULL, env={k: v for k, v in os.environ.items() if k != 'OFFICE_DISPATCH_ID'},\n"
              "                 cwd='/')\n" % kid_code)
    code = ("import subprocess, sys, time\n"
            "subprocess.run([sys.executable, '-c', %r, sys.argv[1]])\n"
            "print('up', flush=True)\ntime.sleep(25)\n" % middle)
    ready = cold.tmp / "kid-ready"
    leader = subprocess.Popen([sys.executable, "-c", code, str(ready)], stdout=subprocess.PIPE, text=True, start_new_session=True,
                              env={**os.environ, "OFFICE_DISPATCH_ID": d["id"]})
    threading.Thread(target=leader.wait, daemon=True).start()
    assert leader.stdout.readline().strip() == "up"
    _until(lambda: ready.exists() and ready.read_text().isdigit())  # the member is ignoring SIGTERM before anything is signalled
    kid = int(ready.read_text())
    ident = paths.run_dir("run-A") / "dispatches" / d["id"]
    ident.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ident / "agent.identity", {"pid": leader.pid, "start": process_start(leader.pid), "c_start": dispatch._c_start(leader.pid)})
    (ident / "agent.pgid").write_text(str(leader.pid))
    try:
        recover(cold, d["id"])
        states = {row[0]: row[3] for row in _REAL_PROCESS_TABLE()}
        assert kid not in states or states[kid].startswith("Z")
        assert cold.trials()[0]["status"] == "fell-back"
    finally:
        for pid in (kid, leader.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@pytest.mark.parametrize("listing", [None, set()])
def test_an_unreadable_holder_listing_is_not_an_empty_one(cold, monkeypatch, listing):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    monkeypatch.setattr(dispatch, "_cwd_holders", lambda path: listing)
    recover(cold, d["id"])
    if listing is None:
        assert len(cold.dispatches()) == 1 and "cannot be listed" in task_row(cold)["pause_reason"]
    else:
        assert cold.trials()[0]["status"] == "fell-back"


def test_lsof_that_fails_or_prints_nothing_is_unreadable(monkeypatch, tmp_path):
    from types import SimpleNamespace
    real_exists = dispatch.Path.exists
    monkeypatch.setattr(dispatch.Path, "exists", lambda self: False if str(self) == "/proc/self/cwd" else real_exists(self))
    for result in (SimpleNamespace(returncode=2, stdout="p1\nn/x\n"), SimpleNamespace(returncode=1, stdout=""),
                   SimpleNamespace(returncode=0, stdout="")):
        monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: result)
        assert dispatch._cwd_holders(tmp_path) is None
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=f"p7\nfcwd\nn{tmp_path}/sub\np8\nfcwd\nn/elsewhere\n"))
    assert dispatch._cwd_holders(tmp_path) == {7}


def test_a_non_utf8_filename_does_not_break_the_snapshot(cold, monkeypatch):
    # Linux lets a worker stage a name that is not UTF-8 (macOS refuses to create one): git lists it raw.
    d, wt, ddir = in_flight(cold)
    real = dispatch.subprocess.run

    def run(argv, **kw):
        out = real(argv, **kw)
        if "ls-files" in argv and "-s" in argv:
            out.stdout = b"100644 " + b"a" * 40 + b" 0\tbad-\xff.txt\0"
        return out

    monkeypatch.setattr(dispatch.subprocess, "run", run)
    assert dispatch._worktree_snapshot(wt) is not None


def test_a_scheduler_pause_and_a_task_that_changed_hands_each_stop_the_fallback(cold, monkeypatch):
    from office import queuecmd
    d, wt, ddir = in_flight(cold)
    queuecmd.pause(cold.con, run_arg="run-A", task="T1", reason="hold")
    recover(cold, d["id"])
    assert "paused by the operator" in task_row(cold)["pause_reason"] and len(cold.dispatches()) == 1


def test_the_recovery_rereads_who_owns_the_task_inside_its_transaction(cold, monkeypatch):
    d, wt, ddir = in_flight(cold)
    real = dispatch._recovery_fallback

    def then_handed_on(*a, **kw):
        out = real(*a, **kw)
        with db.transaction(cold.con):  # between the fallback routing and the transaction another path took the task
            cold.con.execute("UPDATE tasks SET current_dispatch_id='Dsomeone' WHERE id='T1'")
        return out

    monkeypatch.setattr(dispatch, "_recovery_fallback", then_handed_on)
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "launch-failed"
    assert "changed during the recovery" in json.loads(cold.trials()[0]["outcome"])["why"]
    assert task_row(cold)["current_dispatch_id"] == "Dsomeone" and task_row(cold)["status"] != "blocked"  # not ours to block


# ------------------------------------------------------------------ jobs

def test_a_job_spawned_from_inside_a_worktree_starts_elsewhere_and_without_the_dispatch_tag(cold, monkeypatch):
    seen = {}

    def popen(argv, **kw):
        seen.update(kw)

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "Dworker")
    wt = paths.worktrees_dir() / "run-A" / "T9"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    jobs.spawn("J1", "run-A")
    assert seen["cwd"] == str(paths.run_dir("run-A")) and "OFFICE_DISPATCH_ID" not in seen["env"]
    seen.clear()
    monkeypatch.chdir(cold.tmp / "repo")
    jobs.spawn("J1", "run-A")
    assert seen["cwd"] is None


def test_the_supervisor_launch_env_does_not_tag_the_supervisor_with_a_dispatch(cold, monkeypatch):
    import inspect
    assert "env.pop(_WORKER_TAG, None)" in inspect.getsource(dispatch.launch)
