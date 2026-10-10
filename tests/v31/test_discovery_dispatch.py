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


@pytest.mark.parametrize("change,blocked", [
    (_take_the_trial, "trial-cap"), (_large_task, "risk"), (_irreversible, "risk"), (_spent_quota, "quota-reserve"),
    (_quarantined_fallback, "no-fallback"), (_quarantined_trial_route, "quarantined"), (_stale_probe, "probe-stale"),
    (_disabled, "discovery-disabled")])
def test_any_recheck_that_fails_in_the_transaction_dispatches_the_fallback_and_writes_no_trial(cold, monkeypatch, change, blocked):
    _between(cold, monkeypatch, change)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    assert d["triple"].startswith("codex@0/gpt-6-astra@"), d["triple"]
    assert [t["id"] for t in cold.trials()] in ([], ["other"]) and len(_live_leases(cold)) == 1
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

    def together(con, run, task, decision, **kw):
        out = real(con, run, task, decision, **kw)
        if out.get("trial"):
            try:
                gate.wait()  # both have a trial decision in hand before either opens its transaction
            except threading.BrokenBarrierError:
                pass
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
    (trial,) = cold.trials()
    ds = cold.dispatches()
    assert len(ds) == 2 and sum(d["triple"] == trial["route"] for d in ds) == 1
    assert sum(len(_live_leases(cold, t)) for t in ("T1", "T2")) == 2
    assert [e["kind"] for e in cold.events()].count("trial-reserved") == 1


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
        assert dispatch.trial_submitted(cold.con, d["id"])
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
    assert len(cold.attempts()) == 1 and len(cold.dispatches()) == 1 and not cold.trials() or len(cold.trials()) <= 1
    assert state.get_task(cold.con, "run-A", "T2")["status"] == "queued"


