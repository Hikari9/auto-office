"""#494 acceptance scenarios S1-S12, one named test per scenario (`test_sNN_...`), plus the cases a scenario needs.

Every test runs against an isolated Office home, one scripted fake `codex` (tests/fixtures/route_probe/fake_harness.py,
the same one the routing, probe and dispatch suites use) and a real runs.db. No real model is called. The unit tier
drives the real `dispatch.dispatch` (jobs queued, never started), the real `route_probe.ensure` and the real `office`
inspect renderers. The tests that run the `office` CLI use the `env` fixture, so the suite's tiering marks them
integration: they run with `--all`.

Fixtures and helpers are the ones T2-T5 built and use:
  World   test_route_discovery_consumer_contract.py  decisions, probes, policies (no persisted run)
  Cold    test_discovery_dispatch.py                 World plus a persisted run `run-A` with tasks T1/T2
  Seed    test_route_learning_discovery.py           the rows dispatch, submit and the gates leave behind
"""
import copy
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office import (adapters, adaptive, candidates, config as cfg, db, dispatch, inspect_cmd, paths, route_learning,
                    route_policy, route_probe, routing, scoring, state)
from office.util import dumps, now_iso

from test_discovery_dispatch import (Tree, _audit, _between, _blast_radius, _irreversible, _large_task, _live_leases,
                                     _quarantined_fallback, _quarantined_trial_route, _route_json, _spent_quota, _until,
                                     cold, in_flight, launched, recover, task_row, trees)  # noqa: F401 (the two fixtures)
from test_route_discovery_consumer_contract import (BASE_EAF155A, EFFORTS, SOL, World, categories, recorded_requests,
                                                    skipped, world)  # noqa: F401 (the fixture)
from test_route_discovery_routing import base as synthetic_base, discovery as synthetic_discovery, go, handle, untried
from test_route_discovery_routing import failed as failed_probe, passed as passed_probe
from test_route_learning_discovery import FALLBACK, ROUTE, Seed, TRIPLE, evidence, outcome

ASTRA = "gpt-6-astra"  # the fake world's known-working route: every installed available route is user-trusted


def trial_route(cold):
    (trial,) = cold.trials()
    return trial["route"]


def probe_rows(cold):
    return {r["key"]: dict(r) for r in cold.con.execute("SELECT * FROM route_probes")}


def reservations(cold, run_id="run-A"):
    return [dict(r) for r in cold.con.execute(
        "SELECT * FROM route_probe_reservations WHERE run_id IS ? ORDER BY reserved_at, rowid", (run_id,))]


def watch_decisions(monkeypatch):
    """Every `candidates.route_role` call, in order: whether it carried a recompute handle, the intent it came back with
    and the route it selected."""
    seen = []
    real = candidates.route_role

    def spy(con, config, run, role, **kw):
        out = real(con, config, run, role, **kw)
        seen.append({"role": role, "recompute": bool(kw.get("discovery_input")), "excluded": sorted(kw.get("exclude") or ()),
                     "intent": (out.get("discovery") or {}).get("intent"), "selected": out.get("selected"),
                     "candidate": (out.get("discovery") or {}).get("candidate")})
        return out

    monkeypatch.setattr(candidates, "route_role", spy)
    return seen


# ====================================================================== S1 discovery assignment

def test_s01_discovery_assignment_from_a_cold_start_through_the_d5_preflight_only(cold, monkeypatch):
    # cold start: nothing probed, nothing reserved, nothing tried, and no manual probe was run
    assert cold.cache() == [] and cold.events() == [] and cold.trials() == []
    assert cold.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0
    decisions = watch_decisions(monkeypatch)
    probed = []
    real_ensure = route_probe.ensure

    def spy(con, run, cand, **kw):
        probed.append((cand["invocation_model_id"], cand["effort"], kw["context"]["origin"], kw["attempt_id"]))
        return real_ensure(con, run, cand, **kw)

    monkeypatch.setattr(route_probe, "ensure", spy)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (trial,) = cold.trials()
    (attempt,) = cold.attempts()
    # the trial is the one real bounded executor task, on a route Office had no receipt for
    assert d["role"] == "executor" and d["triple"] == trial["route"] and trial["id"] == attempt
    model = trial["route"].split("/", 1)[1]
    assert model.startswith(SOL + "@")
    assert scoring.evaluate_trust_state(cold.con, trial["route"])[1] == "valid-unverified"  # never verified before
    assert cold.run["risk"]["size_class"] == "S" and cold.run["risk"]["blast_radius"] == "repo"
    # D5, and only D5: the first decision named a probe intent and kept the known-working primary; the probe ran once,
    # in the preflight, before any dispatch row; the recompute carried the handle and selected the probed route
    first, second, fallback = decisions
    assert [(x["recompute"], x["intent"]) for x in (first, second)] == [(False, "probe"), (True, "trial")]
    assert fallback["excluded"] == [trial["route"]] and not fallback["recompute"]  # the fallback is decided without it
    assert first["selected"] != trial["route"] and first["candidate"] == trial["route"] and second["selected"] == trial["route"]
    assert probed == [(SOL, trial["route"].rsplit("@", 1)[1], "preflight", attempt)]
    events = cold.events(attempt)
    assert [e["kind"] for e in events][:2] == ["probe-reserved", "probe-result"]
    assert [e["dispatch_id"] for e in events][:2] == [None, None]  # the probe ran before any dispatch row existed
    assert [e["origin"] for e in events][:2] == ["preflight", "preflight"]
    # no approve trust, no --as, no recorded override, no forged trust act
    assert cold.authority() == cold.baseline
    assert not cold.con.execute("SELECT 1 FROM adapter_trust_acts WHERE triple=?", (trial["route"],)).fetchone()
    found = _route_json(d)
    assert not found.get("override") and found.get("route_source") in (None, "router")
    assert _audit(cold)["dispatch"]["source"] == "router"
    assert found["discovery"]["fallback"] and found["discovery"]["fallback_route"] == trial["fallback_route"]
    assert len(_live_leases(cold)) == 1


# ====================================================================== S2 failed probe

PROBE_FAILURES = [("unsupported_effort", "unsupported-model-effort"), ("auth", "auth-quota-blocked"),
                  ("transient", "transient"), ("no_write", "isolation-missing"), ("malformed", "conformance-failed")]


@pytest.mark.parametrize("mode,reason_class", PROBE_FAILURES)
def test_s02_a_failed_probe_never_becomes_a_trial_and_dispatches_the_known_working_fallback(cold, mode, reason_class):
    cold.script(mode=mode)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    (attempt,) = cold.attempts()
    # the cold-start probe-fail path: the known-working route runs, no trial row, one lease
    assert not cold.trials() and d["triple"].startswith(f"codex@0/{ASTRA}@")
    assert len(cold.leases()) == 1 and len(_live_leases(cold)) == 1
    (probe,) = cold.cache()
    assert (probe["result"], probe["reason_class"]) == ("fail", reason_class)
    assert cold.kinds(attempt) == ["probe-reserved", "probe-result", "dispatch-linked"]
    assert cold.events(attempt)[1]["reason_class"] == reason_class and cold.events(attempt)[-1]["outcome"] == "fallback"
    found = _route_json(d)["discovery"]
    assert found["intent"] == "none" and found["blocked"] == f"probe-failed:{reason_class}" and not found["fallback"]
    assert not cold.con.execute("SELECT 1 FROM outbox WHERE kind='trial_recovery'").fetchone()
    # the precise class is on the audit, never a verdict on the model
    assert _audit(cold)["dispatch"]["discovery"]["blocked"] == f"probe-failed:{reason_class}"
    assert cold.authority() == cold.baseline


def test_s02_other_efforts_of_the_same_model_stay_routable_after_an_unsupported_effort(cold):
    cold.script(mode="unsupported_effort")
    cold.dispatch("T1")
    (failed_attempt,) = cold.attempts()
    failed_route = cold.events(failed_attempt)[0]["candidate_route"]
    effort = failed_route.rsplit("@", 1)[1]
    (probe,) = cold.cache()
    assert probe["effort"] == effort and probe["reason_class"] == "unsupported-model-effort"
    # the next task probes a different effort of the same model and gets its trial
    cold.script(mode="pass")
    cold.dispatch("T2")
    (trial,) = cold.trials()
    assert trial["task_id"] == "T2" and trial["route"].split("/", 1)[1].startswith(SOL + "@")
    assert trial["route"].rsplit("@", 1)[1] != effort
    rows = probe_rows(cold)
    assert sorted((r["effort"], r["result"]) for r in rows.values()) == sorted(
        [(effort, "fail"), (trial["route"].rsplit("@", 1)[1], "pass")])
    # and the failed exact effort is not offered again, however often it is decided
    again = cold.decide(effort)
    assert categories(again)[cold.sol[effort]] == "probe-failed" and again["discovery"]["intent"] == "none"


def test_s02_a_failed_probe_leaves_other_harnesses_routable():
    """A harness-neutral decision: the failed codex route is rejected as `probe-failed`, every other route stays."""
    c = untried(probe=failed_probe("unsupported-model-effort"))
    d = go(synthetic_base() + [c], disc=synthetic_discovery(), discovery_input=handle(c))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "probe-failed:unsupported-model-effort"
    assert categories(d)["codex@2/newmodel@high"] == "probe-failed"
    assert {e["route"] for e in d["slate"]} >= {"claude@2/opus@medium", "codex@2/luna@medium"}
    assert d["selected"] in ("claude@2/opus@medium", "codex@2/luna@medium")


# ====================================================================== S3 exact and fingerprint scoped

def test_s03_a_pass_qualifies_only_its_exact_fingerprint(world):
    _, attempt, outcome_, trial = world.preflight("high")
    assert outcome_["result"] == "pass" and trial["discovery"]["intent"] == "trial"
    (probe,) = world.cache()
    assert (probe["invocation_model_id"], probe["effort"]) == (SOL, "high")
    # no sibling effort inherits the pass: each is an untried candidate with no record of its own
    for effort in EFFORTS:
        if effort == "high":
            continue
        sibling = world.decide(effort)
        assert sibling["discovery"]["probe"] is None and sibling["discovery"]["intent"] == "probe", effort
        assert sibling["discovery"]["probe_key"] != trial["discovery"]["probe_key"]
    # a trial recompute for another effort, with the high record in the cache, is dropped
    probe_med = world.decide("medium")
    handle_ = {"candidate": world.sol["medium"], "reservation_id": "x", "attempt_id": "x",
               "probe_key": world.candidate(probe_med, world.sol["medium"])["probe_key"]}
    blocked = world.decide("medium", discovery_input=handle_)
    assert blocked["discovery"]["intent"] == "none" and blocked["discovery"]["blocked"] == "probe-missing"
    # no model-family inference: the pass is on exactly one cache row, and every other discovery candidate in the
    # request (any model) still has no record of its own
    assert [(c["invocation_model_id"], c["effort"]) for c in world.cache()] == [(SOL, "high")]
    pool = [c for c in world.decide(None)["request"]["candidates"] if c["discovery"]]
    assert len(pool) == len(EFFORTS) and {c["invocation_model_id"] for c in pool} == {SOL}
    assert all(c["probe"] is None for c in pool if c["effort"] != "high")
    assert [c["probe"]["attempt_id"] for c in pool if c["effort"] == "high"] == [attempt]


def test_s03_a_changed_harness_version_adapter_or_launch_profile_invalidates_the_cached_pass(world):
    first, attempt, outcome_, _ = world.preflight("high")
    assert outcome_["result"] == "pass"
    cand = world.candidate(first, first["discovery"]["candidate"])
    adapter = adapters.load_all()[cand["adapter_id"]]
    fresh = route_probe.status(world.con, cand, adapter=adapter)
    assert fresh and fresh["attempt_id"] == attempt
    # adapter hash: any change to the adapter is a new key
    changed = copy.deepcopy(adapter)
    changed.setdefault("office_profiles", {}).setdefault("worker", {})["_t6_changed"] = True
    assert adapters.adapter_hash(changed) != adapters.adapter_hash(adapter)
    assert route_probe.key(cand, changed) != route_probe.key(cand, adapter)
    assert route_probe.status(world.con, cand, adapter=changed) is None
    # launch profile: another profile is another key
    assert route_probe.key(cand, adapter, "plan") != route_probe.key(cand, adapter)
    assert route_probe.key(cand, adapter).endswith("|worker") and route_probe.key(cand, adapter, "plan").endswith("|plan")
    # harness version: the 0.162.0 pass is not the 0.163.0 route's record
    world.upgrade_harness("0.163.0")
    after = world.decide("high")
    assert after["discovery"]["intent"] == "probe" and after["discovery"]["probe"] is None
    assert "|0.163.0|" in after["discovery"]["probe_key"] != first["discovery"]["probe_key"]
    assert [c["harness_version"] for c in world.cache()] == ["0.162.0"]  # the old record stays as it was: history


# ====================================================================== S4 user denial

DENIED_ROUTE = f"codex/{SOL}@high"


def nothing_was_probed(w):
    return (w.cache() == [] and w.events() == []
            and w.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0)


def test_s04_a_denied_route_is_excluded_from_every_selection_path_and_never_probed(cold):
    cold.write_policy("user", denied=[f"codex/{SOL}"])  # every effort of the model
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    # automatic routing: the known-working route runs, nothing was drawn, probed, reserved or tried
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials() and nothing_was_probed(cold)
    assert "discovery" not in _route_json(d) or _route_json(d)["discovery"].get("intent") in (None, "none")
    auto = cold.decide(None)
    denied = {e["candidate"] for e in auto["rejected"] if e.get("category") == "denied"}
    assert denied == set(cold.sol.values()) and auto["discovery"]["candidate"] is None
    # plan `route:` choices
    audit = auto["routing"]
    choice = adaptive.apply_planner_choice(audit, {"routes": [cold.sol["high"]], "why": "the planner prefers it here"})
    assert "rejected at stage 1" in choice["planner_error"] and "denied by user" in choice["planner_error"]
    assert choice["chooser"] == "router" and choice["primary"] == audit["slate"][0]["route"]
    # `--as`, `--review-as`, `amend route` (declared_candidate) and `--route`
    for flag in ("--as", "--review-as"):
        with pytest.raises(state.Refused) as err:
            candidates.declared_decision(DENIED_ROUTE, flag=flag)
        assert err.value.category == "route-denied" and "denied by user" in err.value.message
    with pytest.raises(state.Refused) as err:
        candidates.declared_candidate("codex", SOL, "high")
    assert err.value.category == "route-denied"
    forced = cold.decide("high", override=DENIED_ROUTE)
    assert forced["status"] != "selected" and forced["selected"] is None
    # an explicit probe request is refused before it reserves or launches anything
    pre = cold.decide(None)
    attempt, refused = cold.ensure(pre, route=cold.sol["high"])
    assert refused == route_probe.Refused("route-denied") and refused.attempt_id == attempt
    assert cold.cache() == [] and cold.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0
    assert [e["kind"] for e in cold.events()] == ["probe-refused"]
    assert cold.authority() == cold.baseline


def test_s04_only_an_explicit_policy_change_re_enables_a_denied_route(cold):
    cold.write_policy("user", denied=[f"codex/{SOL}"])
    cold.dispatch("T1")
    assert not cold.trials() and nothing_was_probed(cold)
    cold.user_config.write_text("routing: {}\n")  # the user lifts the denial
    cold.dispatch("T2")
    (trial,) = cold.trials()
    assert trial["task_id"] == "T2" and trial["route"].split("/", 1)[1].startswith(SOL + "@")
    assert candidates.declared_candidate("codex", SOL, "high")["override"] is True


@pytest.mark.parametrize("tier", ["user", "repo"])
def test_s04_a_denial_in_either_config_tier_is_a_hard_exclusion(cold, tier):
    cold.write_policy(tier, denied=[f"codex/{SOL}"])
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials() and nothing_was_probed(cold)
    cold.write_policy(tier, denied=["harness:codex"])  # a harness-wide denial leaves nothing to route to, and still no probe
    with pytest.raises(state.Refused) as err:
        cold.dispatch("T2")
    assert err.value.category == "no-route" and f"denied by {tier} routing.user_policy.denied_models" in err.value.message
    assert nothing_was_probed(cold)


def test_s04_cli_every_manual_path_refuses_a_denied_route_and_nothing_is_probed(env):
    from conftest import approved_run
    from test_route_discovery_consumer_contract import CLI_DENIED, EXTERNAL, cli_authority, cli_deny
    approved_run(env)
    before = cli_authority(env)
    cli_deny(env)
    for args in (("dispatch", "T1", "--as", CLI_DENIED),
                 ("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--review-as", CLI_DENIED),
                 ("dispatch", "T1", "--review-as", CLI_DENIED),
                 ("dispatch", "T1", "--route", CLI_DENIED),
                 ("amend", "route", "T1", "--as", CLI_DENIED, "--quote", "use it")):
        code, out = env.office(*args, env=EXTERNAL)
        assert code != 0 and "denied by user routing.user_policy.denied_models" in out, (args, out)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM route_probes").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM route_discovery_events").fetchone()[0] == 0
    assert cli_authority(env) == before
    cli_deny(env, spec="gpt-9-nothing")  # an explicit policy change re-enables the route
    code, out = env.office("dispatch", "T1", "--as", CLI_DENIED, env=EXTERNAL)
    assert code == 0, out


# ====================================================================== S5 overkill

def overkill(route, **scope):
    fields = ", ".join([f"route: {route}"] + [f"{k}: [{', '.join(v)}]" for k, v in scope.items()])
    return "{" + fields + "}"


def test_s05_overkill_skips_a_route_only_in_the_automatic_selection_its_scope_matches(cold):
    route = f"codex/{SOL}"
    cold.write_policy("user", overkill=[overkill(route, roles=["executor"], size_classes=["S"])])
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    # in scope (executor, size S): automatic selection and the discovery draw skip the route
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials() and nothing_was_probed(cold)
    in_scope = cold.decide(None)
    assert {c for c, cat in categories(in_scope).items() if cat == "overkill"} == set(cold.sol.values())
    entry = next(e for e in in_scope["rejected"] if e["candidate"] == cold.sol["high"])
    assert entry["reason"] == f"overkill by user routing.user_policy.overkill_rules: {route}"
    # explicit manual selection still reaches an overkill route (the primary here: an untried route is not selectable by hand)
    primary = in_scope["selected"]
    spec = f"codex/{primary.split('/', 1)[1]}"
    cold.write_policy("user", overkill=[overkill(spec, roles=["executor"], size_classes=["S"])])
    skipped_ = cold.decide(None)
    assert skipped_["selected"] != primary and categories(skipped_)[primary] == "overkill"
    manual = cold.decide(None, override=spec)
    assert manual["selected"] == primary and "overkill" not in categories(manual).values()
    assert candidates.declared_decision(spec)["selected"].endswith(primary.split("/", 1)[1])
    # outside the rule's scope (another role, or another size) the route stays automatic and discoverable
    cold.write_policy("user", overkill=[overkill(route, roles=["worker"])])
    cold.dispatch("T2")
    (trial,) = cold.trials()
    assert trial["task_id"] == "T2" and trial["route"].split("/", 1)[1].startswith(SOL + "@")
    cold.write_policy("user", overkill=[overkill(route, size_classes=["L"])])
    assert "overkill" not in categories(cold.decide(None)).values()


def test_s05_shipped_defaults_hold_no_denied_or_overkill_entries(monkeypatch, tmp_path):
    import yaml
    shipped = yaml.safe_load((paths.resources_root() / "config" / "config.default.yaml").read_text())
    assert shipped["routing"]["user_policy"] == {"denied_models": [], "overkill_rules": []}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    assert cfg.resolve(None)[0]["routing"]["user_policy"] == {"denied_models": [], "overkill_rules": []}
    assert candidates.current_user_policies() == []
    # nothing in the shipped catalog or config marks a route denied or overkill by brand, price, size or age
    assert "overkill" not in json.dumps({k: v for k, v in shipped.items() if k != "routing"}).lower()


# ====================================================================== S6 cost

def priced(*, ceiling=None, source=None, policy="balanced", extra=None):
    from test_route_discovery_consumer_contract import cand as ccand, request as crequest
    cheap, pricey = ccand("codex", "luna", out=0.5, inp=0.1), ccand("claude", "opus", out=200, inp=40, effort="xhigh")
    rq = crequest([cheap, pricey], adaptive={"budget_ceiling_usd": ceiling, **(extra or {})})
    rq["cost_policy"] = policy
    if source:
        rq["budget_ceiling"] = {"usd": ceiling, "source": source}
    return routing.route(rq)


def test_s06_without_a_user_ceiling_cost_ranks_routes_and_removes_none():
    free = priced(policy="money_saver")
    assert free["status"] == "selected" and not [e for e in free["rejected"] if "budget ceiling" in e.get("reason", "")]
    rows = {r["route"]: r for r in free["routing"]["candidates"]}
    assert set(rows) == {"codex@2/luna@medium", "claude@2/opus@xhigh"}  # the expensive route is still a candidate
    assert rows["claude@2/opus@xhigh"]["cost_to_success"] > 25  # over the shipped scale and still not removed
    assert free["routing"]["numeric_order"][0] == "codex@2/luna@medium"  # cost is a ranking factor
    assert free["routing"]["budget_ceiling_usd"] is None


def test_s06_a_user_ceiling_is_a_hard_removal_that_names_its_source_tier():
    capped = priced(ceiling=1.0, source="user")
    gone = [e for e in capped["rejected"] if "budget ceiling" in e.get("reason", "")]
    assert [e["candidate"] for e in gone] == ["claude@2/opus@xhigh"] and gone[0]["stage"] == 8
    assert capped["routing"]["budget_ceiling_source"] == "user" and capped["selected"] == "codex@2/luna@medium"


@pytest.mark.parametrize("tier", ["shipped", "user", "repo"])
def test_s06_the_shipped_economic_scale_is_not_a_ceiling_for_new_runs(world, tier):
    if tier != "shipped":
        path = world.user_config if tier == "user" else world.repo_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("routing:\n  adaptive:\n    budget_ceiling_usd: 3\n")
    resolved = cfg.resolve(world.tmp / "repo")[0]
    assert resolved["routing"]["adaptive"]["cost_scale_usd"] == 25
    assert route_policy.budget_ceiling(resolved) == {"usd": None if tier == "shipped" else 3.0, "source": tier}
    config = world.policy()
    config["routing"]["adaptive"]["budget_ceiling_usd"] = resolved["routing"]["adaptive"]["budget_ceiling_usd"]
    config[route_policy.PROVENANCE_KEY] = resolved[route_policy.PROVENANCE_KEY]
    d = world.decide("high", config=config, run=world.make_run(config))
    assert d["request"]["budget_ceiling"] == {"usd": None if tier == "shipped" else 3.0, "source": tier}
    assert d["routing"]["budget_ceiling_source"] == tier


def test_s06_a_number_that_came_from_the_shipped_tier_is_never_a_hard_ceiling():
    shipped = copy.deepcopy(cfg.resolve(None)[0])
    shipped["routing"]["adaptive"]["budget_ceiling_usd"] = 25  # e.g. an old shipped default
    shipped[route_policy.PROVENANCE_KEY][route_policy.CEILING_KEY] = "shipped"
    assert route_policy.budget_ceiling(shipped) == {"usd": None, "source": "shipped"}
    for tier in ("user", "repo", "run"):
        shipped[route_policy.PROVENANCE_KEY][route_policy.CEILING_KEY] = tier
        assert route_policy.budget_ceiling(shipped) == {"usd": 25.0, "source": tier}


def test_s06_a_run_pinned_before_this_change_keeps_its_own_ceiling():
    pinned = priced(ceiling=25.0, extra={"cost_scale_usd": 25})  # no provenance in the request: the old default stands
    assert pinned["status"] == "selected" and pinned["routing"]["budget_ceiling_usd"] == 25.0
    assert "budget_ceiling_source" not in pinned["routing"]


# ====================================================================== S7 unchanged authority

GATE_ROLES = ["planner", "plan_reviewer", "code_reviewer", "integration_reviewer", "visual_reviewer",
              "browser_verifier", "closeout_verifier"]


@pytest.mark.parametrize("role", GATE_ROLES)
def test_s07_a_planner_reviewer_or_verifier_never_takes_a_trial_route(world, role):
    d = world.decide(None, role=role)
    assert "discovery" not in d and all(c["route_status"] == "available" for c in d["request"]["candidates"])
    assert d["selected"] not in world.sol.values()
    assert skipped(d).get(f"codex/{SOL}@high") == "untried"
    # a recompute handle cannot smuggle one in either
    first = world.decide("high")
    forged = {"candidate": world.sol["high"], "probe_key": first["discovery"]["probe_key"], "reservation_id": "x", "attempt_id": "x"}
    assert "discovery" not in world.decide(None, role=role, discovery_input=forged)


@pytest.mark.parametrize("size,blast,irreversible", [
    ("L", "repo", False), ("XL", "repo", False), ("S", "production", False), ("S", "production-data", False),
    ("S", "repo", True), ("M", "production", False)])
def test_s07_risky_tasks_never_get_a_trial_and_nothing_is_probed(world, size, blast, irreversible):
    run = world.make_run(size_class=size, blast_radius=blast, irreversible=irreversible)
    d = world.decide("high", run=run)
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "risk" and d["discovery"]["candidate"] is None
    assert d["selected"] not in world.sol.values() and nothing_was_probed(world)


@pytest.mark.parametrize("change,blocked", [
    (_large_task, "risk"), (_irreversible, "risk"), (_blast_radius, "risk"), (_spent_quota, "quota-reserve"),
    (_quarantined_fallback, "no-fallback"), (_quarantined_trial_route, "quarantined")])
def test_s07_each_authority_gate_is_rechecked_at_dispatch_and_holds(cold, monkeypatch, change, blocked):
    _between(cold, monkeypatch, change)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials() and len(_live_leases(cold)) == 1
    assert _route_json(d)["discovery"]["blocked"] == blocked
    assert cold.authority() == cold.baseline  # a blocked trial grants and clears nothing


def test_s07_capability_and_permission_gates_still_block_a_discovery_candidate():
    c = untried(probe=passed_probe(), caps=())
    d = go(synthetic_base() + [c], disc=synthetic_discovery(), discovery_input=handle(c))
    assert d["discovery"]["blocked"] == "permission" and d["selected"] != "codex@2/newmodel@high"
    spent = untried(probe=passed_probe(), quota=3)
    d = go(synthetic_base() + [spent], disc=synthetic_discovery(), discovery_input=handle(spent))
    assert d["discovery"]["blocked"] == "quota" and d["selected"] != "codex@2/newmodel@high"


def test_s07_an_archived_row_is_never_probed_or_routed(world):
    import yaml
    archive = yaml.safe_load((paths.resources_root() / "catalog" / "archive" / "legacy-nonrouting-2026-10-08.yaml").read_text())
    row = next(r for r in archive["models"] if r["invocation_harness"] == "codex" and route_probe.catalog_row(
        {"harness": "codex", "model_id": r["model_id"], "effort": r["effort"]}) is None)
    spec = f"codex/{row['model_id']}@{row['effort']}"
    with pytest.raises(state.Usage) as err:
        route_probe.candidate_from_spec(spec)
    assert err.value.category == "route-archived"
    assert not [c for c in world.decide(None)["request"]["candidates"]
                if (c["model_id"], c["effort"]) == (row["model_id"], row["effort"])]
    assert nothing_was_probed(world)


def test_s07_review_and_landing_stay_independent_and_human(env, monkeypatch):
    """A trial executor is an ordinary executor: its revision still goes to a reviewer on another route, and the run
    is not landed or approved by anything the discovery did."""
    from test_discovery_dispatch import _cli_run
    from conftest import GOOD_ADD
    _cli_run(env, monkeypatch, {"write": {"calc.py": GOOD_ADD}, "submit": True})
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    trial = dict(con.execute("SELECT * FROM route_trials").fetchone())
    assert trial["status"] == "submitted"
    reviewers = [dict(r) for r in con.execute("SELECT triple, role FROM dispatches WHERE role != 'executor'")]
    assert all(r["triple"] != trial["route"] for r in reviewers)
    run = dict(con.execute("SELECT status, phase FROM runs").fetchone())
    assert run["status"] != "landed" and run["phase"] != "closed"
    assert not con.execute("SELECT 1 FROM adapter_trust_acts WHERE triple=?", (trial["route"],)).fetchone()


# ====================================================================== S8 atomic allocation

def add_tasks(cold, *ids):
    now = now_iso()
    with db.transaction(cold.con):
        for tid in ids:
            cold.con.execute(
                "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
                "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run-A", tid, f"task {tid}", "executor", dumps([f"{tid.lower()}.py"]), "[]", "[]", "[]", "[]", None,
                 "planned", 3, 3, 3, now, now))


def pin_policy(cold, **discovery):
    cold.config = cold.policy(**discovery)
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (dumps(cold.config),))


def seed_decisions(cold, n, *, trials=0, role="executor"):
    """`n` recorded executor dispatch decisions of other runs, then `trials` trials they took (the rolling window's input)."""
    route_learning.ensure_schema(cold.con)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with db.transaction(cold.con):
        for i in range(n):
            cold.con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, decision_hash, disclosure_json, created_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (f"ra-{i}", "run-B", f"T{i}", role, "dispatch", f"h{i}", "{}",
                                                         (base + timedelta(minutes=i)).isoformat()))
        for i in range(trials):
            cold.con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                             "policy_digest, reason, status, created_at, updated_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"old-{i}", "run-B", f"T{i}", f"Dold{i}", "executor", "x", "k", "y",
                                                                  "d", "r", "accepted", now_iso(), now_iso()))


def always_draw(monkeypatch):
    """The discovery draw lands (rate is `max_trial_percent_rolling_20`, so at 15 percent most seeds do not draw): the
    caps are what these tests are about. The draw itself is pinned by the routing suite."""
    real = adaptive._draw
    monkeypatch.setattr(adaptive, "_draw", lambda seed, label: 0.0 if label == "discovery" else real(seed, label))


def test_s08_per_run_caps_hold_across_a_parallel_wave(cold):
    add_tasks(cold, "T3", "T4")
    cold.dispatch("T1", "T2", "T3", "T4", parallel=True)
    ds = cold.dispatches()
    assert len(ds) == 4 and len({d["task_id"] for d in ds}) == 4
    # 2 probes per run, 1 live trial per run, whatever the wave size
    assert len(reservations(cold)) == 2 and len(cold.cache()) == 2
    (trial,) = cold.trials()
    assert sum(d["triple"] == trial["route"] for d in ds) == 1
    assert [e["kind"] for e in cold.events()].count("trial-reserved") == 1
    blocked = [_route_json(d)["discovery"]["blocked"] for d in ds if d["id"] != trial["dispatch_id"]]
    assert len(blocked) == 3 and set(blocked) <= {"probe-cap", "trial-cap"}
    # every task has exactly one live lease: no orphan, no second writer
    assert [len(_live_leases(cold, t)) for t in ("T1", "T2", "T3", "T4")] == [1, 1, 1, 1]
    assert cold.authority() == cold.baseline


@pytest.mark.parametrize("decisions,trials,allowed", [
    (5, 0, False),    # a cold history opens no window: 6 decisions at 15 percent is 0 trials
    (6, 0, True),     # 7 in the window: one trial
    (6, 1, False),
    (20, 2, True),    # 15 percent of the last 20 is 3
    (20, 3, False),
    (30, 3, False)])  # only the last 20 count
def test_s08_the_rolling_cap_is_fifteen_percent_of_the_last_twenty_decisions(cold, monkeypatch, decisions, trials, allowed):
    always_draw(monkeypatch)
    pin_policy(cold, max_trial_percent_rolling_20=15, max_trials_per_run=5)
    seed_decisions(cold, decisions, trials=trials)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    if allowed:
        (mine,) = [t for t in cold.trials() if t["dispatch_id"] == d["id"]]
        assert d["triple"] == mine["route"] and len(cold.trials()) == 1 + trials
    else:
        assert d["triple"].startswith(f"codex@0/{ASTRA}@") and len(cold.trials()) == trials
        assert _audit(cold)["discovery"]["blocked"] == "rolling-cap" and cold.events() == []  # decided before any probe
    assert len(_live_leases(cold)) == 1


def test_s08_the_rolling_cap_binds_in_the_transaction_when_another_run_takes_the_last_slot(cold, monkeypatch):
    always_draw(monkeypatch)
    pin_policy(cold, max_trial_percent_rolling_20=15, max_trials_per_run=5)
    seed_decisions(cold, 6)

    def another_runs_trial(cold, _decision):
        seed_decisions(cold, 0, trials=1)

    _between(cold, monkeypatch, another_runs_trial)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and [t["id"] for t in cold.trials()] == ["old-0"]
    assert _route_json(d)["discovery"]["blocked"] == "rolling-cap"


def test_s08_concurrent_dispatch_commands_cannot_both_take_the_last_rolling_slot(cold, monkeypatch):
    always_draw(monkeypatch)
    pin_policy(cold, max_trial_percent_rolling_20=15, max_trials_per_run=5)  # the per-run caps are not what stops the second
    seed_decisions(cold, 6)
    gate = threading.Barrier(2, timeout=45)
    real = dispatch.preflight_discovery
    arrived = []

    def together(con, run, task, decision, **kw):
        out = real(con, run, task, decision, **kw)
        if out.get("trial"):
            arrived.append(task["id"])
            gate.wait()  # both hold a trial decision before either opens its transaction
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
    assert sorted(arrived) == ["T1", "T2"] and not gate.broken
    (trial,) = cold.trials()
    ds = cold.dispatches()
    assert len(ds) == 2 and sum(d["triple"] == trial["route"] for d in ds) == 1
    (loser,) = [d for d in ds if d["id"] != trial["dispatch_id"]]
    assert _route_json(loser)["discovery"]["blocked"] == "rolling-cap" and loser["triple"].startswith(f"codex@0/{ASTRA}@")
    assert sum(len(_live_leases(cold, t)) for t in ("T1", "T2")) == 2


def test_s08_the_reservation_commits_with_the_dispatch_row_or_not_at_all(cold, monkeypatch):
    real = dispatch.record_discovery

    def then_fails(*a, **kw):
        real(*a, **kw)  # the trial row and its events are written in the transaction ...
        raise RuntimeError("the transaction dies after them")

    monkeypatch.setattr(dispatch, "record_discovery", then_fails)
    with pytest.raises(RuntimeError):
        cold.dispatch("T1")
    # ... and none of it survives: no trial, no dispatch, no lease, and the run's trial slot is still free
    assert not cold.trials() and not cold.dispatches() and not cold.leases()
    settings = route_policy.discovery_settings(cold.config)
    assert route_probe.allocation(cold.con, "run-A", settings)["trials"]["used"] == 0
    monkeypatch.setattr(dispatch, "record_discovery", real)
    cold.dispatch("T1")  # the slot is intact, and the probe already on record is reused, not run again
    (d,) = cold.dispatches()
    assert d["triple"] == trial_route(cold) and len(cold.cache()) == 1
    assert len(reservations(cold)) == 1  # the second dispatch reused the first one's probe (a cache hit)
    assert [e["kind"] for e in cold.events() if e["kind"] == "probe-cache-hit"] == ["probe-cache-hit"]


# ====================================================================== S9 safe fallback

@pytest.mark.parametrize("mode", ["auth", "transient"])
def test_s09_a_cold_start_probe_failure_dispatches_the_known_working_fallback_with_no_trial(cold, mode):
    cold.script(mode=mode)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials()
    assert len(cold.leases()) == 1 and len(_live_leases(cold)) == 1 and not _live_leases(cold)[0]["revoked_at"]
    assert not cold.con.execute("SELECT 1 FROM outbox WHERE kind='trial_recovery'").fetchone()
    (probe,) = cold.cache()
    assert probe["result"] == "fail"
    assert task_row(cold)["status"] in ("launching", "running")


def test_s09_a_probe_that_cannot_run_dispatches_the_primary_and_costs_nothing(cold, monkeypatch):
    def broken(con, run, cand, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(route_probe, "ensure", broken)
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"].startswith(f"codex@0/{ASTRA}@") and not cold.trials()
    assert _route_json(d)["discovery"]["blocked"] == "probe-error: OperationalError" and len(_live_leases(cold)) == 1


def test_s09_a_trial_that_fails_before_any_work_releases_its_lease_and_session_and_runs_the_fallback(cold):
    d, wt, ddir = in_flight(cold)
    with db.transaction(cold.con):
        cold.con.execute("UPDATE dispatches SET session_id='sess-1', harness='codex' WHERE id=?", (d["id"],))
        cold.con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) "
                         "VALUES('codex','sess-1','run-A',?,'x'), ('codex','sess-other','run-A',?,'x')", (now_iso(), now_iso()))
    trial = cold.trials()[0]
    recover(cold, d["id"])
    old, new = cold.dispatches()
    assert new["triple"] == trial["fallback_route"] and old["ended_at"] and old["status"] == "failed"
    # one writer at a time: the trial's lease was released (not revoked) before the fallback's was acquired
    first, second = cold.leases()
    assert first["released_at"] and not first["revoked_at"] and first["released_at"] <= second["acquired_at"]
    assert [l["id"] for l in _live_leases(cold)] == [new["lease_id"]]
    # the failed dispatch's session binding ended; another session of the run is untouched
    ended = {r["session_id"]: r["ended_at"] for r in cold.con.execute("SELECT * FROM session_bindings")}
    assert ended["sess-1"] and ended["sess-other"] is None
    # recorded as a failure of the launch, not of the model
    (attempt,) = cold.attempts()
    assert cold.kinds(attempt)[-2:] == ["trial-launch-failed", "trial-fell-back"]
    assert cold.trials()[0]["status"] == "fell-back" and cold.authority() == cold.baseline


def test_s09_a_worker_that_could_still_write_is_stopped_before_the_fallback_starts(cold, trees, monkeypatch):
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
        seen["alive_at_fallback_launch"] = tree.alive()
        seen["live_leases"] = len(_live_leases(cold))
        return real(*a, **kw)

    monkeypatch.setattr(dispatch, "request_launch", spy)
    recover(cold, d["id"])
    assert seen["alive_at_fallback_launch"] is False  # never a second live writer
    assert len(_live_leases(cold)) == 1


def test_s09_work_that_already_started_is_never_discarded(cold, trees):
    d, wt, ddir = in_flight(cold)
    launched(cold, d)
    work = wt / "new_module.py"
    tree = Tree(d["id"], work)
    trees.append(tree)
    tree.record("run-A", d["id"])
    _until(lambda: work.exists() and work.stat().st_size > 0)
    recover(cold, d["id"])
    assert work.exists() and work.stat().st_size > 0 and not tree.alive()
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "abandoned"  # no fallback started on top of it
    assert [bool(l["released_at"] or l["revoked_at"]) for l in cold.leases()] == [False]  # the lease stays with the work
    assert task_row(cold)["status"] == "blocked" and "work had started" in task_row(cold)["pause_reason"]


def test_s09_a_user_pinned_route_is_never_switched_silently(cold):
    # --as: dispatched as asked, never probed, never a trial; its failure surfaces as it does today (a same-route relaunch)
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-A"), ["T1"], as_model=f"codex/{SOL}@high")
    (d,) = cold.dispatches()
    assert d["triple"] == cold.sol["high"] and not cold.trials() and not cold.events() and not cold.cache()
    launched(cold, d)
    dispatch._finish(d["id"], 1, None, "nonzero", 0.1)
    assert not cold.con.execute("SELECT 1 FROM outbox WHERE kind='trial_recovery'").fetchone()
    assert cold.dispatches()[-1]["triple"] == cold.sol["high"]
    # a route the plan or the user recorded for the task is kept as recorded
    pick = cold.decide("medium")["candidate"]
    with db.transaction(cold.con):
        cold.con.execute("UPDATE tasks SET route_json=? WHERE id='T2'", (dumps({"candidate": pick}),))
    cold.dispatch("T2")
    t2 = [x for x in cold.dispatches() if x["task_id"] == "T2"]
    assert [x["triple"] for x in t2] == [routing.candidate_id(pick)] and not cold.trials()
    assert _audit(cold)["discovery"]["blocked"] == "pinned-route"


def test_s09_no_trial_is_dispatched_without_a_known_working_fallback(cold, monkeypatch):
    pin_policy(cold, require_known_fallback=False)  # routing alone would allow it; the dispatch transaction still refuses
    real = scoring.evaluate_trust_state

    def unproven(con, triple):
        return (0, "valid-unverified") if SOL not in triple else real(con, triple)

    _between(cold, monkeypatch, lambda cold, decision: monkeypatch.setattr(scoring, "evaluate_trust_state", unproven))
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert not cold.trials() and d["triple"].startswith(f"codex@0/{ASTRA}@")
    assert _route_json(d)["discovery"]["blocked"] == "no-fallback"


def test_s09_a_fallback_that_no_longer_qualifies_blocks_with_a_next_step_and_starts_nothing(cold):
    d, wt, ddir = in_flight(cold)
    cold.monkeypatch.setenv("OFFICE_QUOTA_FIXTURE", json.dumps({"codex": 1}))
    recover(cold, d["id"])
    assert len(cold.dispatches()) == 1 and cold.trials()[0]["status"] == "launch-failed"
    assert "no longer qualifies" in task_row(cold)["pause_reason"]
    assert "office dispatch T1 --reroute" in [e for e in cold.con.execute("SELECT summary FROM events WHERE kind='task.blocked'")][-1][0]
    assert len(_live_leases(cold)) == 1  # still the one lease, held by the dispatch that failed


# ====================================================================== S10 learning without trust

@pytest.fixture
def lcon(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    scoring.ensure_trust_schema(con)
    yield con
    con.close()


def trust_rows(con):
    return con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]


def test_s10_accepted_trials_teach_the_route_and_grant_it_no_trust(lcon):
    seed = Seed(lcon)
    for i in range(1, 13):
        seed.landed_trial(i)
    outcomes = route_learning.derive_outcomes(lcon)
    assert len(outcomes) == 12 and all(o["success"] and o["attribution"] == "route" and o["learn_weight"] == 1.0
                                       and o["trial"] for o in outcomes)
    assert evidence(lcon, outcomes)["successes"] > 11  # effectiveness moved
    before = scoring.evaluate_trust_state(lcon, TRIPLE)
    with db.transaction(lcon):
        written = route_learning.refresh(lcon, {"executor": {ROUTE: {"prior_p": 0.5}}})
    # the existing maturity and replay rules: 12 accepted trials in 12 runs clear the bar and two do not
    assert [w["state"] for w in written] == ["learned-eligible"]
    eps = [e for e in route_learning.episodes(outcomes) if e["role"] == "executor"]
    assert route_learning.eligibility_transitions(eps[:2], {ROUTE: {"prior_p": 0.5}}, {}) == []
    # no trust act, no recorded override, no label: learning is not trust
    assert trust_rows(lcon) == 0 and scoring.evaluate_trust_state(lcon, TRIPLE) == before == (0, "valid-unverified")
    assert lcon.execute("SELECT COUNT(*) FROM outcome_labels").fetchone()[0] == 0
    # each accepted trial got one immutable terminal event and its row says so
    assert [r["status"] for r in lcon.execute("SELECT status FROM route_trials ORDER BY id")] == ["accepted"] * 12
    assert lcon.execute("SELECT COUNT(*) FROM route_discovery_events WHERE kind='trial-accepted'").fetchone()[0] == 12


def test_s10_learning_never_clears_a_standing_quarantine(lcon, monkeypatch):
    seed = Seed(lcon)
    for i in range(1, 13):
        seed.landed_trial(i)
    real = scoring.evaluate_trust_state
    monkeypatch.setattr(scoring, "evaluate_trust_state", lambda con, t: (1, "quarantined") if t == TRIPLE else real(con, t))
    monkeypatch.setattr(scoring, "record_trust_act", lambda *a, **k: pytest.fail("learning wrote a trust act"))
    with db.transaction(lcon):
        route_learning.refresh(lcon, {"executor": {ROUTE: {"prior_p": 0.5}}})
        route_learning.refresh(lcon, {"executor": {ROUTE: {"prior_p": 0.5}}})
    assert scoring.evaluate_trust_state(lcon, TRIPLE) == (1, "quarantined") and trust_rows(lcon) == 0


@pytest.mark.parametrize("reason,what", [
    ("transient", "launch"), ("conformance-failed", "harness or adapter"), ("isolation-missing", "environment"),
    ("auth-quota-blocked", "quota"), (None, "launch with no recorded class")])
def test_s10_launch_harness_adapter_environment_and_quota_failures_are_not_the_models(lcon, reason, what):
    seed = Seed(lcon)
    seed.run("R1")
    seed.task("R1", "T1", accepted="V2")
    seed.dispatch("R1", "D1", "T1", term="nonzero", exit_code=2)
    seed.trial("A1", "R1", "T1", "D1", ending="fell-back", reason_class=reason)
    seed.dispatch("R1", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R1", "V2", "D2", "T1")
    o = outcome(lcon, "D1")
    assert o["attribution"] == "environment" and o["learn_weight"] == 0 and not o["success"], what
    stats = evidence(lcon, route_learning.derive_outcomes(lcon))
    assert stats["failures"] == 0 and stats["n_effective"] == 0
    with db.transaction(lcon):
        route_learning.refresh(lcon)
    assert trust_rows(lcon) == 0


def test_s10_a_brief_defect_is_not_the_models_and_a_code_review_rejection_is(lcon):
    seed = Seed(lcon)
    seed.run("R1")
    seed.task("R1", "T1", status="cancelled")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    seed.finding("R1", "D1", "T1", "V1", "brief")
    seed.trial("A1", "R1", "T1", "D1")
    brief = outcome(lcon, "D1")
    assert brief["attribution"] == "plan" and brief["learn_weight"] == 0
    seed.run("R2")
    seed.task("R2", "T1", accepted="V4")
    seed.dispatch("R2", "D3", "T1")
    seed.revision("R2", "V3", "D3", "T1")
    seed.finding("R2", "D3", "T1", "V3", "code_review")
    seed.trial("A2", "R2", "T1", "D3")
    seed.dispatch("R2", "D4", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R2", "V4", "D4", "T1")
    rejected = outcome(lcon, "D3")
    assert rejected["attribution"] == "route" and rejected["learn_weight"] > 0.8 and not rejected["success"]


def discovery_cli_env(env, monkeypatch, **probe):
    from test_discovery_dispatch import _discovery_env
    _discovery_env(env, monkeypatch, **probe)


def test_s10_cli_an_accepted_trial_revision_is_learned_from_and_grants_nothing(env, monkeypatch):
    """The whole path with scripted harnesses: cold start, probe, trial, submit, independent review, acceptance."""
    from conftest import GOOD_ADD, start_inline
    discovery_cli_env(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, extra=("--size-class", "S"))
    env.office("approve", "plan", "--quote", "approved", check=0)
    con = env.con()
    trust_before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    trial = dict(con.execute("SELECT * FROM route_trials").fetchone())
    (d,) = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE role='executor'")]
    assert d["triple"] == trial["route"]
    reviewers = [dict(r) for r in con.execute("SELECT triple FROM dispatches WHERE role='code_reviewer'")]
    assert reviewers and all(r["triple"] != trial["route"] for r in reviewers)  # independent review, on another route
    # the learner reads the accepted revision as the route's evidence ...
    (o,) = [o for o in route_learning.derive_outcomes(con) if o["dispatch_id"] == d["id"]]
    assert o["success"] and o["attribution"] == "route" and o["learn_weight"] == 1.0 and o["trial"]["attempt_id"] == trial["id"]
    with db.transaction(con):
        route_learning.record_trial_outcomes(con)
    assert dict(con.execute("SELECT status, outcome FROM route_trials").fetchone()) == {"status": "accepted", "outcome": "accepted"}
    kinds = [r[0] for r in con.execute("SELECT kind FROM route_discovery_events WHERE attempt_id=? ORDER BY seq", (trial["id"],))]
    assert kinds == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved", "trial-launched",
                     "trial-submitted", "trial-accepted"]
    # ... and nothing else: no trust act, no quarantine change, the route is still valid-unverified
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == trust_before
    assert scoring.evaluate_trust_state(con, trial["route"])[1] == "valid-unverified"
    code, out = env.office("inspect", "learner")
    assert code == 0 and "trial" in out.lower(), out


# ====================================================================== S11 audit

def add_run(cold, run_id, *, plan_version, **discovery):
    """A second persisted run with its own pinned policy (so its own digest), task T1 and its own plan version."""
    config = cold.policy(**discovery)
    now = now_iso()
    sdir = paths.run_dir(run_id)
    sdir.mkdir(parents=True, exist_ok=True)
    with db.transaction(cold.con):
        cold.con.execute(
            "INSERT INTO runs(id, family_id, created_at, status, office_version, repo_root, git_common_dir, goal, phase, "
            "gear, playbook, base_sha, state_dir, requirements_version, plan_version, routing_version, policy_json, "
            "risk_json, gates_json, envelope_json, plan_review_json, planner_mode, updated_at, escalations_used) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (run_id, run_id, now, "executing", cold.con.execute("SELECT office_version FROM runs WHERE id='run-A'").fetchone()[0],
             cold.run["repo_root"], cold.run["git_common_dir"], "goal", "executing", "", "Change", cold.base, str(sdir), 1,
             plan_version, 1, dumps(config), dumps(cold.run["risk"]), "{}", "[]", "{}", "inline", now))
        cold.con.execute(
            "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, checks_json, "
            "visual_json, status, introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "T1", "task T1", "executor", dumps(["t1.py"]), "[]", "[]", "[]", "[]", None, "planned", plan_version,
             plan_version, plan_version, now, now))
    return config


def run_events(cold, run_id):
    """A run's events exactly as stored: every column of every row."""
    return [tuple(r) for r in cold.con.execute("SELECT * FROM route_discovery_events WHERE run_id IS ? ORDER BY seq", (run_id,))]


def every_attempt(cold):
    out = {}
    for e in cold.events():
        out.setdefault(e["attempt_id"], []).append(e)
    return out


def test_s11_repeated_probes_of_one_fingerprint_keep_separate_histories(cold, monkeypatch):
    from office import doctor
    p1 = cold.config[route_policy.DIGEST_KEY]
    p2 = add_run(cold, "run-B", plan_version=5, probe_ttl_days=3)[route_policy.DIGEST_KEY]
    assert p1 != p2
    # --- run A, policy P1: the probe fails (a transient wall) ...
    cold.script(mode="transient")
    cold.dispatch("T1")
    (a1,) = cold.attempts()
    effort = cold.events(a1)[0]["candidate_route"].rsplit("@", 1)[1]
    key = cold.events(a1)[0]["probe_key"]
    assert probe_rows(cold)[key]["result"] == "fail" and not cold.trials()
    # ... and a later reservation of the same fingerprint is never finished: it expires (its owner is gone)
    spec = f"codex/{SOL}@{effort}"
    cand = route_probe.candidate_from_spec(spec)
    real_now = route_probe._utcnow
    monkeypatch.setattr(route_probe, "_utcnow", lambda: real_now() + timedelta(hours=2))  # the transient failure is stale
    a2 = route_policy.new_attempt_id()
    run_a = state.get_run(cold.con, "run-A")
    held = route_probe.reserve(cold.con, run_a, cand, attempt_id=a2, context={
        "origin": "preflight", "role": "executor", "task_id": "T2", "reason": "discovery: retry after a transient failure",
        "primary_route": cold.decide(None)["selected"], "fallback_route": cold.decide(None)["selected"]})
    assert isinstance(held, dict) and held["reserved"]
    route_probe._release(a2)  # the process that held the probe is gone
    monkeypatch.setattr(route_probe, "_utcnow", real_now)
    assert route_probe.expire_stale(cold.con, now=real_now() + timedelta(hours=1)) == [a2]
    assert cold.kinds(a2) == ["probe-reserved", "probe-expired"]
    assert [r["status"] for r in reservations(cold)] == ["failed", "expired"] and key not in probe_rows(cold)
    history_a = run_events(cold, "run-A")
    assert {e[2] for e in history_a} == {a1, a2}  # seq, id, attempt_id
    # --- run B, policy P2, another plan version: the same fingerprint is probed again, passes, and is tried
    cold.script(mode="pass")
    denied_others = [f"codex/{SOL}@{e}" for e in EFFORTS if e != effort]
    cold.write_policy("user", denied=denied_others)  # the only effort B may draw is the one A could not prove
    dispatch.dispatch(cold.con, state.get_run(cold.con, "run-B"), ["T1"])
    (b_trial,) = [t for t in cold.trials() if t["run_id"] == "run-B"]
    b1 = b_trial["id"]
    assert b_trial["probe_key"] == key and probe_rows(cold)[key]["result"] == "pass" and probe_rows(cold)[key]["attempt_id"] == b1
    # run A's history is byte for byte what it was before run B existed
    assert run_events(cold, "run-A") == history_a
    # --- a standalone, unbound `office doctor --probe-route`, and a cache hit on the same key
    cold.write_policy("user", denied=[])
    unbound_hit = doctor.probe_route_record(spec)
    assert unbound_hit.exit_code == 0 and "cached record, no new launch" in "\n".join(unbound_hit.lines)
    other = next(e for e in EFFORTS if e != effort)
    unbound_probe = doctor.probe_route_record(f"codex/{SOL}@{other}")
    assert unbound_probe.exit_code == 0 and "fresh probe" in "\n".join(unbound_probe.lines)
    hit_attempt, probe_attempt = unbound_hit.data["attempt_id"], unbound_probe.data["attempt_id"]
    assert run_events(cold, "run-A") == history_a
    # --- every attempt keeps its own events, with its run (or NULL), plan version, policy digest, outcome and time
    attempts = every_attempt(cold)
    assert set(attempts) == {a1, a2, b1, hit_attempt, probe_attempt}
    expect = {a1: ("run-A", 3, p1), a2: ("run-A", 3, p1), b1: ("run-B", 5, p2), hit_attempt: (None, None, None),
              probe_attempt: (None, None, None)}
    for attempt, events in attempts.items():
        run_id, plan, digest = expect[attempt]
        assert {(e["run_id"], e["plan_version"]) for e in events} == {(run_id, plan)}, attempt
        assert len({e["policy_digest"] for e in events}) == 1, attempt
        if digest:
            assert events[0]["policy_digest"] == digest
        assert all(e["outcome"] and e["created_at"] and e["reason"] and e["probe_key"] == events[0]["probe_key"] for e in events)
    unbound_digest = attempts[hit_attempt][0]["policy_digest"]
    assert unbound_digest not in (p1, p2) and attempts[probe_attempt][0]["policy_digest"] == unbound_digest
    assert all(e["origin"] == "manual" and e["dispatch_id"] is None and e["task_id"] is None
               for a in (hit_attempt, probe_attempt) for e in attempts[a])
    assert [(e["kind"], e["outcome"]) for e in attempts[hit_attempt]] == [("probe-cache-hit", "cache-hit:pass")]
    assert attempts[hit_attempt][0]["source_attempt_id"] == b1 and attempts[hit_attempt][0]["probe_key"] == key
    assert [(e["kind"], e["outcome"]) for e in attempts[a1][:2]] == [("probe-reserved", "reserved"), ("probe-result", "fail")]
    assert attempts[a1][1]["reason_class"] == "transient" and attempts[a2][1]["outcome"] == "expired"
    assert [(e["kind"], e["outcome"]) for e in attempts[b1]][:2] == [("probe-reserved", "reserved"), ("probe-result", "pass")]
    # the audited fields of a trial: run, plan, dispatch, digest, fingerprint, reason, primary, fallback, freshness, outcome
    trial_events = {e["kind"]: e for e in attempts[b1]}
    reserved = trial_events["trial-reserved"]
    assert reserved["dispatch_id"] and reserved["run_id"] == "run-B" and reserved["plan_version"] == 5
    assert reserved["primary_route"] == b_trial["route"] or reserved["primary_route"]
    assert reserved["fallback_route"] == b_trial["fallback_route"] != b_trial["route"]
    assert json.loads(reserved["fingerprint_json"])["effort"] == effort and reserved["reason"]
    assert {e["probe_freshness"] for e in attempts[b1]} <= {"none", "fresh-run", "cached-fresh", "stale"}
    # --- `office inspect route` shows both runs' attempts separately (and the unbound manual probes in each)
    view_a = "\n".join(inspect_cmd._route(cold.con, state.get_run(cold.con, "run-A"), "executor").lines)
    view_b = "\n".join(inspect_cmd._route(cold.con, state.get_run(cold.con, "run-B"), "executor").lines)
    assert f"attempt {a1} run run-A plan p3" in view_a and f"attempt {a2} run run-A plan p3" in view_a
    assert f"attempt {b1}" not in view_a
    assert f"attempt {b1} run run-B plan p5" in view_b and f"digest {p2[:19]}" in view_b
    assert f"attempt {a1}" not in view_b and f"attempt {a2}" not in view_b
    for view in (view_a, view_b):
        assert f"attempt {hit_attempt} run none plan none" in view and f"attempt {probe_attempt} run none plan none" in view
    assert cold.authority() == cold.baseline


@pytest.fixture
def iworld(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('run-A','Change','executing','{}')")
    yield w


def category_of(lines):
    start = next(i for i, line in enumerate(lines) if line.startswith("candidates by category"))
    out = {}
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        category, candidate = line.split()[:2]
        out[candidate] = category
    return out


def test_s11_inspect_route_distinguishes_every_state_a_route_can_be_in(iworld):
    w = iworld
    w.config = w.policy(max_probes_per_run=6)
    w.run = w.make_run(w.config)
    w.write_policy("user", denied=[f"codex/{SOL}@low"], overkill=[overkill(f"codex/{SOL}@medium", roles=["executor"])])
    w.script(modes={"high": "unsupported_effort", "*": "pass"})
    w.preflight("high")                                            # a confirmed unsupported effort
    w.con.execute(                                                 # a probe in flight
        "INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, effort, result, "
        "probed_at, run_id, attempt_id) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
        (w.decide("xhigh")["discovery"]["probe_key"], "codex", "0.162.0", "x", "worker", SOL, "xhigh",
         route_probe._utcnow().isoformat(), "run-A", "Apending"))
    res = inspect_cmd._route(w.con, w.run, "executor")
    cats = category_of(res.lines)
    assert cats[w.sol["low"]] == "denied" and cats[w.sol["medium"]] == "overkill"
    assert cats[w.sol["high"]] == "unsupported" and cats[w.sol["xhigh"]] == "probe-pending"
    assert cats[w.sol["max"]] in ("untried", "probe-candidate")
    text = "\n".join(res.lines)
    assert "preference source tiers: denied_models user | overkill_rules user | budget ceiling shipped" in text
    assert any(line.strip().startswith("denied") and "denied by user routing.user_policy.denied_models" in line for line in res.lines)
    assert "  caps: probes " in text and "trials 0/1 used" in text and "rolling " in text   # allocation and cap state
    assert "discovery intent" in text
    # the same view after the other efforts have been tried: passed and failed are told apart from the rest
    w.script(modes={"max": "pass", "xhigh": "transient"})
    w.con.execute("DELETE FROM route_probes WHERE result='pending'")
    w.preflight("max")
    w.preflight("xhigh")
    after = category_of(inspect_cmd._route(w.con, w.run, "executor").lines)
    # a passed route is `probe-passed`, or `probe-candidate` while it is the one the draw would try next (its pass is on the line)
    assert after[w.sol["max"]] in ("probe-passed", "probe-candidate") and after[w.sol["xhigh"]] == "probe-failed"
    assert after[w.sol["high"]] == "unsupported"
    passed_view = "\n".join(inspect_cmd._route(w.con, w.run, "executor").lines)
    assert after[w.sol["max"]] == "probe-passed" or "probe: pass, fresh" in passed_view


def test_s11_inspect_route_names_recovered_launches_and_a_trial_with_its_fallback(iworld):
    seed = Seed(iworld.con)
    seed.task("run-A", "T2", accepted="V3")
    seed.dispatch("run-A", "D2", "T2", term="nonzero", exit_code=2)
    seed.trial("A2", "run-A", "T2", "D2", ending="fell-back", reason_class="transient")
    seed.dispatch("run-A", "D3", "T2", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("run-A", "V3", "D3", "T2")
    res = inspect_cmd._route(iworld.con, iworld.run, "executor")
    (row,) = [t for t in res.data["trials"] if t["attempt_id"] == "A2"]
    assert row["recovered_launch"] is True and row["fallback"] == FALLBACK and row["status"] == "fell-back"
    assert any(line.strip().startswith("A2 T2 dispatch D2") and line.endswith("recovered launch: the fallback ran")
               for line in res.lines)


# ====================================================================== S12 migration and replay

ROOT = Path(__file__).resolve().parents[2]
BASE_COMMIT = "eaf155a"  # the tree the S12 reference hashes come from
SCHEMA_BASE = "5ad1594"  # main as #494 lands on it: the previous runtime whose schema S12 extends


def test_s12_recorded_requests_replay_to_the_same_decision_hash_with_discovery_off():
    from test_route_discovery_consumer_contract import off_block, untried as pool_row
    for name, base_hash in sorted(BASE_EAF155A.items()):
        request = recorded_requests()[name]
        assert routing.route(request)["decision_hash"] == base_hash, name
        assert routing.route(json.loads(json.dumps(recorded_requests()[name])))["decision_hash"] == base_hash, name
        if name.startswith(("worker", "pinned")):  # the shapes a discovery-era request can take with discovery off
            request["candidates"] = [*request["candidates"], pool_row()]
            request["discovery"] = off_block()
            decision = routing.route(request)
            assert decision["decision_hash"] == base_hash and "discovery" not in decision, name


def test_s12_runs_pinned_before_this_change_keep_their_config_and_route_identically(cold):
    pre = copy.deepcopy(cold.config)
    for key in (route_policy.PROVENANCE_KEY, route_policy.DIGEST_KEY):
        pre.pop(key)
    pre["routing"]["adaptive"]["budget_ceiling_usd"] = 25  # the old default ceiling stays hard for a run pinned with it
    off = copy.deepcopy(pre)
    off["routing"]["discovery"]["enabled"] = False
    for pinned in (pre, off):
        assert route_policy.discovery_settings(pinned)["enabled"] is False  # `enabled: true` in a pin is not honoured
    run = state.get_run(cold.con, "run-A")
    on_pin = candidates.route_role(cold.con, pre, run, "executor", task_id="T1", probe=False)
    off_pin = candidates.route_role(cold.con, off, run, "executor", task_id="T1", probe=False)
    assert on_pin["decision_hash"] == off_pin["decision_hash"] and on_pin["selected"] == off_pin["selected"]
    assert "discovery" not in on_pin and "discovery" not in on_pin["request"]
    assert on_pin["request"]["adaptive_config"]["budget_ceiling_usd"] == 25
    # and the dispatch of such a run never probes, reserves or tries anything
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (dumps(pre),))
    cold.dispatch("T1")
    (d,) = cold.dispatches()
    assert d["triple"] == on_pin["selected"] and not cold.trials() and not cold.events() and not cold.cache()
    assert "discovery" not in _route_json(d)
    assert cold.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0


def test_s12_the_replay_script_reports_identical_decisions_for_discovery_off_and_pinned_runs():
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "route_replay.py"), "--self-test"],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok   discovery off and pinned pre-change decisions are identical" in proc.stdout
    assert "ok   the source database is untouched" in proc.stdout and "ok   the live runs.db is refused" in proc.stdout


def test_s12_archived_rows_and_confirmed_unsupported_rows_never_return(world, monkeypatch):
    import yaml
    archive = yaml.safe_load((paths.resources_root() / "catalog" / "archive" / "legacy-nonrouting-2026-10-08.yaml").read_text())
    archived = {(r["invocation_harness"], r["model_id"], r["effort"]) for r in archive["models"]}
    pool = world.decide(None)
    seen = {(c["harness"], c["model_id"], c["effort"]) for c in pool["request"]["candidates"]}
    assert not (seen & archived)
    assert not [s for s in pool["skipped"] if s.get("category") == "archived"]  # archived rows are not even listed as skipped
    # a confirmed unsupported effort stays unsupported however long ago it was proven, for this exact fingerprint only
    world.script(mode="unsupported_effort")
    first, attempt, outcome_, _ = world.preflight("high")
    assert outcome_["reason_class"] == "unsupported-model-effort"
    real_now = route_probe._utcnow
    monkeypatch.setattr(route_probe, "_utcnow", lambda: real_now() + timedelta(days=400))
    cand = route_probe.candidate_from_spec(f"codex/{SOL}@high")
    again = route_probe.ensure(world.con, world.run, cand, attempt_id=route_policy.new_attempt_id(), context={
        "origin": "preflight", "role": "executor", "task_id": "T1", "reason": "a year later"})
    assert again["cached"] is True and again["result"] == "fail" and again["reason_class"] == "unsupported-model-effort"
    assert len(world.cache()) == 1 and [e["kind"] for e in world.events(again["attempt_id"])] == ["probe-cache-hit"]
    after = world.decide("high")
    assert after["discovery"]["intent"] == "none" and categories(after)[world.sol["high"]] == "probe-failed"
    assert any(route.endswith(f"{SOL}@high") for route in route_learning.unsupported_routes(world.con))
    # another effort of the same model is not touched by it
    assert world.decide("medium")["discovery"]["intent"] == "probe"


def test_s12_db_changes_are_additive_and_the_previous_runtime_keeps_working_on_the_same_file(tmp_path):
    import subprocess
    import types
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{SCHEMA_BASE}:src/office/db.py"], capture_output=True, text=True)
    if shown.returncode:
        pytest.skip(f"{SCHEMA_BASE} is not in this clone")
    old = types.ModuleType("office_db_before_494")
    old.__package__ = "office"
    exec(compile(shown.stdout, "db_before_494.py", "exec"), old.__dict__)
    path = tmp_path / "runs.db"
    before = old.connect(path)
    tables_before = {r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    before.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R1','Change','executing','{}')")
    before.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                   "introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                   "VALUES('R1','T1','t','executor','[]','[]','[]','[]','accepted',1,1,1,'t','t')")

    def dump(con, tables):
        return {t: ([tuple(r) for r in con.execute(f"PRAGMA table_info({t})")],
                    [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid")]) for t in sorted(tables)}

    expected = dump(before, tables_before)
    old_version = old.SCHEMA_VERSION
    before.close()
    after = db.connect(path)  # this runtime opens the file the previous one wrote
    tables_after = {r[0] for r in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert db.SCHEMA_VERSION > old_version
    now_dump = dump(after, tables_before)
    # v12 appended nullable evidence columns (T1: dispatch receipts; T2: convergence membership and finding
    # attribution); every other column and row is unchanged.
    v12 = {"predecessor_dispatch_id", "first_executor_dispatch_id", "accepted_producer_dispatch_id",
           "members_json", "attribution_basis", "attributed_task", "clone_of"}
    for table, (cols, rows) in now_dump.items():
        keep = [c[0] for c in cols if c[1] in v12]
        now_dump[table] = ([c for c in cols if c[1] not in v12],
                           [tuple(v for i, v in enumerate(r) if i not in keep) for r in rows])
    assert now_dump.pop("schema_meta")[0] == expected.pop("schema_meta")[0]  # the version stamp is the one row that moves
    assert after.execute("SELECT value FROM schema_meta WHERE key='office_schema'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert now_dump == expected  # no column changed, no row changed, in any pre-existing table
    new = tables_after - tables_before
    assert new == {"route_probes", "route_probe_reservations", "route_trials", "route_discovery_events"}
    assert all(after.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in new)
    after.close()
    again = old.connect(path)  # the previous runtime still opens it and keeps writing its own tables
    again.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R2','Change','executing','{}')")
    assert again.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    again.close()
    final = db.connect(path)
    assert final.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    final.close()


@pytest.mark.review_contract("v3.1")
def test_s12_cli_a_v31_run_started_before_discovery_never_discovers_after_it_is_enabled(env):
    """A run pins its policy at start. Enabling discovery afterwards leaves it, and a v3.1 review-contract run, as pinned."""
    from conftest import GOOD_ADD, approved_run
    user = env.tmp / "user-config.yaml"
    user.write_text("review:\n  contract: v3.1\nrouting:\n  discovery:\n    enabled: false\n")
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    pinned = json.loads(env.con().execute("SELECT policy_json FROM runs").fetchone()[0])
    assert pinned["routing"]["discovery"]["enabled"] is False
    user.write_text("review:\n  contract: v3.1\nrouting:\n  discovery:\n    enabled: true\n    max_trial_percent_rolling_20: 100\n")
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    con = env.con()
    for table in ("route_probes", "route_probe_reservations", "route_trials", "route_discovery_events"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    (d,) = [dict(r) for r in con.execute("SELECT triple FROM dispatches WHERE role='executor'")]
    assert SOL not in d["triple"]


def test_s12_cli_a_legacy_3_0_run_is_served_by_its_pinned_runtime_with_discovery_enabled(env):
    from office import runtime_default
    from test_discovery_pinning import _legacy_run
    (env.tmp / "user-config.yaml").write_text("routing:\n  discovery:\n    enabled: true\n    max_trial_percent_rolling_20: 100\n")
    run_id, sdir = _legacy_run(env, runtime_default.LEGACY_V3_FINAL)
    code, out = env.office("list")
    assert "3.0 legacy" in out and run_id[:8] in out
    code, out = env.office("resume", run_id[:8])
    assert code == 0 and "stays on its pinned runtime" in out, out
    con = env.con()
    for table in ("route_probes", "route_probe_reservations", "route_trials", "route_discovery_events"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
