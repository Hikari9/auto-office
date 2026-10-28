"""#845: a user allow is dispatchable with no proof; a runtime failure is logged and falls back.

Unit tier. The same isolated Office home and scripted fake `codex` as the discovery suites, a real runs.db
and the real `dispatch` module. No model is called.
"""
import json

import pytest
import yaml

from test_discovery_dispatch import Cold, cold, launched  # noqa: F401 (cold is a fixture)
from test_route_discovery_consumer_contract import SOL, World, world  # noqa: F401 (world is a fixture)

from office import candidates, db, dispatch, jobs, paths, route_policy, routecmd, routing, state, user_catalog
from office.util import now_iso


def allow(world_, role, route):
    world_.user_config.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": [
        {"role": role, "route": route, "state": "allow"}]}}}))


def policies(world_):
    return candidates.required_user_policies(str(world_.tmp / "repo"))


def build(world_, role="executor", **kw):
    built, skipped = candidates.build_candidates(world_.con, role, probe=False, user_policies=[],
                                                 allow_policies=policies(world_), **kw)
    return {routing.candidate_id(c): c for c in built}, skipped


# ------------------------------------------------------------------ allow is dispatchable, no proof

def test_a_route_with_no_proof_is_not_a_candidate_until_the_user_allows_it(world):
    sol_high = "codex/gpt-6.1-sol@high"
    cands, skipped = build(world)
    assert world.sol["high"] not in cands
    assert any(s["candidate"] == sol_high and s["category"] in ("not-eligible", "untried") for s in skipped)

    allow(world, "executor", sol_high)
    cands, _ = build(world)
    cand = cands[world.sol["high"]]
    assert cand["route_status"] == "available" and cand["user_allowed"] is True and cand["probe_key"] is None
    assert "allow" in cand["status_reason"]
    assert world.cache() == [] and world.events() == []  # no probe, no discovery event: an allow needs neither


def test_an_allowed_route_is_selected_and_dispatched_without_a_probe(world):
    allow(world, "executor", "codex/gpt-6.1-sol@high")
    decision = world.decide(exact=world.sol["high"])
    assert decision["status"] == "selected" and decision["selected"] == world.sol["high"]
    assert decision["candidate"]["route_status"] == "available"
    assert world.cache() == [] and world.events() == []


def test_the_allow_is_per_role_and_a_deny_still_wins(world):
    allow(world, "code_reviewer", "codex/gpt-6.1-sol@high")
    assert world.sol["high"] not in build(world, "executor")[0]
    world.user_config.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": "codex/gpt-6.1-sol@high", "state": "deny"}]}}}))
    assert world.sol["high"] not in build(world, "executor")[0]


def test_a_preferred_list_entry_makes_the_route_dispatchable_too(world):
    pol = route_policy.preferred_seed_policy("executor", [{"harness": "codex", "model_id": SOL, "effort": "high"},
                                                          {"model_id": SOL}])  # the partial entry grants nothing
    cands, _ = candidates.build_candidates(world.con, "executor", probe=False, user_policies=[], allow_policies=[pol])
    assert set(cands and [c["effort"] for c in cands if c["invocation_model_id"] == SOL]) == {"high"}


# ------------------------------------------------------------------ a legacy overlay no longer overrides an allow

def disabled_luna(monkeypatch):
    rows = candidates.catalog_rows()
    overlay = {"models": [], "disabled": [{"harness": "codex", "model_id": "gpt-6-luna", "effort": "low"}]}
    merged = user_catalog.apply(rows, overlay)
    [row] = [r for r in merged if r.get("model_id") == "gpt-6-luna" and r.get("effort") == "low"
             and r.get("invocation_harness") == "codex"]
    assert row["dispatchable"] is False and row["user_disabled"] is True
    monkeypatch.setattr(candidates, "catalog_rows", lambda: merged)


def test_a_user_disabled_overlay_no_longer_silently_overrides_a_user_allow(world, monkeypatch):
    disabled_luna(monkeypatch)
    label = "codex/gpt-6-luna@low"
    cands, skipped = build(world)
    assert not [c for c in cands.values() if c["model_id"] == "gpt-6-luna" and c["effort"] == "low"]
    assert any(s["candidate"] == label for s in skipped)

    allow(world, "executor", label)
    cands, _ = build(world)
    [cand] = [c for c in cands.values() if c["model_id"] == "gpt-6-luna" and c["effort"] == "low"]
    assert cand["user_allowed"] is True and "overlay" in cand["status_reason"]


def test_the_effective_view_says_plainly_what_dispatches_and_why(world, monkeypatch):
    disabled_luna(monkeypatch)
    before = routecmd.policy(role="executor", effective=True)
    row = next(r for r in before.data["effective"] if r["route"] == "codex/gpt-6-luna@low")
    assert row["dispatches"] is False and "overlay" in row["dispatch_reason"]

    routecmd.change("allow", role="executor", route="codex/gpt-6-luna@low")
    after = routecmd.policy(role="executor", effective=True)
    row = next(r for r in after.data["effective"] if r["route"] == "codex/gpt-6-luna@low")
    assert row["state"] == "allow" and row["dispatches"] is True and "your allow overrides" in row["dispatch_reason"]
    assert any("codex/gpt-6-luna@low: allow" in line and "-> dispatches" in line for line in after.lines)


def test_one_command_reshuffles_several_routes(world):
    result = routecmd.change("allow", role="executor", route="codex/gpt-6.1-sol@high,codex/gpt-6-luna@low")
    assert len(result.data["routes"]) == 2
    rules = yaml.safe_load(world.user_config.read_text())["routing"]["user_policy"]["role_rules"]
    assert {r["route"].split("/")[-1] for r in rules} == {"gpt-6.1-sol@high", "gpt-6-luna@low"}


# ------------------------------------------------------------------ a user allow is not held to a benchmark floor

def test_a_user_allow_skips_the_benchmark_floor_but_not_a_deny():
    low = {"harness": "claude", "harness_version": "2.1.311", "model_id": "claude-haiku-5-5",
           "invocation_model_id": "claude-haiku-5-5", "effort": "low", "capabilities": ["review"],
           "quota": {"status": "ok", "tightest_remaining_percent": 80}, "route_status": "available",
           "benchmark_indexes": {"Artificial Analysis Intelligence Index v4.3.2": 1}}
    rule = {"role": "code_reviewer", "route": "claude/claude-haiku-5-5@low", "state": "allow"}
    pol = {"role_rules": [rule], "denied": [], "overkill": [], "ceilings": {}, "sources": {"role_rules": "user"}}

    def run(*policy):
        return routing.route({"role": "code_reviewer", "candidates": [low], "preferred_seed": None, "runs_db": None,
                              "policy": {"floor": {"min_effort": "high"}, "required_capabilities": []},
                              "user_policies": list(policy)})

    assert run()["status"] == "no_qualifying_candidate"
    assert run(pol)["status"] == "selected"
    assert run({**pol, "role_rules": [{**rule, "state": "deny"}]})["status"] == "no_qualifying_candidate"


# ------------------------------------------------------------------ runtime failure: log, demote, fall back

@pytest.mark.parametrize("classification,text,reason", [
    ("launch_failed", "", "launch-failed"),
    ("timeout", "still thinking", "timeout"),
    ("nonzero", "error: unknown model 'gpt-9'", "unsupported-model-effort"),
    ("nonzero", "Error: reasoning effort 'max' is not supported by this model", "unsupported-model-effort"),
    ("nonzero", "Error: unknown option '--effort' for model gpt-x", "unsupported-model-effort"),
    ("nonzero", "authentication failed: please run /login", "auth"),
    ("nonzero", "fetch failed: ECONNRESET", "transport"),
    ("nonzero", "tests failed: 3 assertions", None),
    ("success", "unknown model", None),
])
def test_route_failures_are_classified_from_the_end_and_the_stderr_tail(classification, text, reason):
    got = dispatch.route_failure(classification, text)
    assert (got[0] if got else None) == reason


@pytest.fixture
def plain(cold):
    """`cold` with discovery off (an ordinary dispatch, not a trial) and executors allowed on two codex routes."""
    config = json.loads(cold.con.execute("SELECT policy_json FROM runs WHERE id='run-A'").fetchone()[0])
    config["routing"]["discovery"]["enabled"] = False
    with db.transaction(cold.con):
        cold.con.execute("UPDATE runs SET policy_json=? WHERE id='run-A'", (json.dumps(config),))
    return cold


def fail(cold, d, text, classification="nonzero"):
    ddir = paths.run_dir("run-A") / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "output.log").write_text(text + "\n")
    launched(cold, d)
    dispatch._finish(d["id"], 1, None, classification, 0.1)


def events(cold, kind):
    return [dict(r) for r in cold.con.execute("SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))]


def test_a_runtime_failure_is_logged_with_the_route_and_reason_then_falls_back(plain):
    plain.dispatch("T1")
    (d,) = plain.dispatches()
    assert not plain.trials()
    fail(plain, d, "starting\nerror: unknown model 'x' for this account")

    [failed] = events(plain, "route.failed")
    payload = json.loads(failed["payload_json"])
    assert payload["route"] == d["triple"] and payload["reason"] == "unsupported-model-effort"
    assert "unknown model" in payload["stderr_tail"] and payload["declared"] is False
    assert payload["effort"] == d["effort"] and payload["demoted_until"]
    assert "falling back" in failed["summary"]
    assert plain.con.execute("SELECT 1 FROM outbox WHERE kind='route_fallback'").fetchone()

    job = plain.con.execute("SELECT id FROM outbox WHERE kind='route_fallback'").fetchone()
    assert jobs.execute(plain.con, job["id"]) == 0
    first, second = plain.dispatches()
    assert second["triple"] != first["triple"] and second["status"] == "launching"
    assert state.get_task(plain.con, "run-A", "T1")["current_dispatch_id"] == second["id"]
    [fell] = events(plain, "route.fallback")
    assert first["triple"] in fell["summary"] and second["triple"] in fell["summary"]
    changed = [json.loads(e["payload_json"]) for e in events(plain, "route.changed")]
    assert any(c["kind"] == "runtime-fallback" and c["before"] == first["triple"] for c in changed)


def test_the_failed_route_is_demoted_for_a_ttl_and_never_permanently(plain):
    plain.dispatch("T1")
    (d,) = plain.dispatches()
    fail(plain, d, "error: unknown model")
    key = routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])
    row = plain.con.execute("SELECT expires_at FROM route_cooldowns WHERE route=?", (key,)).fetchone()
    assert row and row["expires_at"] > now_iso()
    blocked = plain.decide(role="executor", exact=d["triple"])
    assert blocked["status"] != "selected"
    assert any(r["candidate"] == d["triple"] and r.get("category") == "route-demoted" for r in blocked["rejected"])
    # expired: the route is eligible again
    plain.con.execute("UPDATE route_cooldowns SET expires_at='2000-01-01T00:00:00+00:00' WHERE route=?", (key,))
    again = plain.decide(role="executor", exact=d["triple"])
    assert again["status"] == "selected" and again["selected"] == d["triple"]


def test_no_eligible_fallback_blocks_the_task_with_the_reasons(plain, monkeypatch):
    plain.dispatch("T1")
    (d,) = plain.dispatches()
    fail(plain, d, "error: unknown model")
    monkeypatch.setattr(dispatch, "planned_route", lambda *a, **k: {"status": "no_qualifying_candidate", "rejected": []})
    job = plain.con.execute("SELECT id FROM outbox WHERE kind='route_fallback'").fetchone()
    assert jobs.execute(plain.con, job["id"]) == 0
    task = state.get_task(plain.con, "run-A", "T1")
    assert task["status"] == "blocked" and "no fallback is eligible" in task["pause_reason"]
    assert len(plain.dispatches()) == 1


def test_a_declared_route_is_logged_and_demoted_but_never_swapped_silently(plain):
    quote = f"Use codex/{SOL}@high for this task."
    dispatch.dispatch(plain.con, state.get_run(plain.con, "run-A"), ["T1"], as_model=f"codex/{SOL}@high", quote=quote)
    (d,) = plain.dispatches()
    fail(plain, d, "error: unknown model")
    [failed] = events(plain, "route.failed")
    assert json.loads(failed["payload_json"])["declared"] is True
    assert state.get_task(plain.con, "run-A", "T1")["status"] == "blocked"
    assert not plain.con.execute("SELECT 1 FROM outbox WHERE kind='route_fallback'").fetchone()
    assert len(plain.dispatches()) == 1


def test_a_clean_run_clears_the_demotion(plain):
    plain.dispatch("T1")
    (d,) = plain.dispatches()
    key = routing.demotion_key(d["harness"], d["invocation_model_id"], d["effort"])
    plain.con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES(?,?,?)",
                      (key, "2999-01-01T00:00:00+00:00", now_iso()))
    launched(plain, d)
    dispatch._finish(d["id"], 0, None, "success", 0.1)
    assert plain.con.execute("SELECT 1 FROM route_cooldowns WHERE route=?", (key,)).fetchone() is None


def test_a_stale_fallback_job_failing_does_not_block_a_task_that_moved_on(plain):
    plain.dispatch("T1")
    (d,) = plain.dispatches()
    fail(plain, d, "error: unknown model")
    job = state.get_job(plain.con, plain.con.execute("SELECT id FROM outbox WHERE kind='route_fallback'").fetchone()["id"])
    with db.transaction(plain.con):
        state.update_task(plain.con, "run-A", "T1", current_dispatch_id="Dnewer")
        jobs.on_permanent_failure(plain.con, state.get_run(plain.con, "run-A"), job, "boom")
    assert state.get_task(plain.con, "run-A", "T1")["status"] != "blocked"


def test_the_effective_view_does_not_claim_an_uninstalled_harness_dispatches(world, monkeypatch):
    routecmd.change("allow", role="executor", route="codex/gpt-6-luna@low")
    monkeypatch.setattr(routecmd.adapters, "installed", lambda adapter: False)
    row = next(r for r in routecmd.policy(role="executor", effective=True).data["effective"]
               if r["route"] == "codex/gpt-6-luna@low")
    assert row["dispatches"] is False and "not installed" in row["dispatch_reason"]


def test_the_effective_view_counts_a_preferred_seed_allow_and_checks_the_role_launch_profile(world, monkeypatch):
    disabled_luna(monkeypatch)
    world.user_config.write_text(yaml.safe_dump({"roles": {"executor": {"preferred_seed": [
        {"harness": "codex", "model_id": "gpt-6-luna", "effort": "low"}]}}}))
    row = next(r for r in routecmd.policy(role="executor", effective=True).data["effective"]
               if r["route"] == "codex/gpt-6-luna@low")
    assert row["dispatches"] is True and row["state"] == "allow"
    monkeypatch.setattr(routecmd.adapters, "profile", lambda adapter, kind: None)
    row = next(r for r in routecmd.policy(role="executor", effective=True).data["effective"]
               if r["route"] == "codex/gpt-6-luna@low")
    assert row["dispatches"] is False and "launch profile" in row["dispatch_reason"]


def test_an_allowed_route_still_needs_the_adapter_to_have_a_launch_profile_for_the_role(world, monkeypatch):
    allow(world, "visual_reviewer", "codex/gpt-6.1-sol@high")
    real = candidates.adapters.profile
    monkeypatch.setattr(candidates.adapters, "profile", lambda adapter, kind: None if kind == "vision" else real(adapter, kind))
    cands, skipped = build(world, "visual_reviewer")
    assert world.sol["high"] not in cands  # no vision profile: the allow cannot invent one
    assert any(s["candidate"] == "codex/gpt-6.1-sol@high" and s["category"] == "no-profile" for s in skipped)


def test_an_allow_for_a_route_the_catalog_lacks_is_reported_not_dropped(world):
    allow(world, "executor", "codex/gpt-9-nope@high")
    _, skipped = build(world)
    [entry] = [x for x in skipped if x["candidate"] == "codex/gpt-9-nope@high"]
    assert entry["category"] == "not-in-catalog" and "office model add" in entry["reason"]
    assert any("gpt-9-nope" in line and "no such route" in line
               for line in routecmd.policy(role="executor", effective=True).lines)


# ------------------------------------------------------------------ #850: shipped denies and size-scoped allows

def _disable(monkeypatch, model, effort):
    rows = candidates.catalog_rows()
    merged = user_catalog.apply(rows, {"models": [], "disabled": [{"harness": "codex", "model_id": model, "effort": effort}]})
    monkeypatch.setattr(candidates, "catalog_rows", lambda: merged)


def test_a_user_allow_overrides_a_shipped_executor_deny_in_candidate_building(world, monkeypatch):
    """Astra is denied for every executor effort by the shipped policy; a user's own allow still wins (#850)."""
    _disable(monkeypatch, "gpt-6-astra", "low")
    label = "codex/gpt-6-astra@low"
    cands, skipped = build(world)
    assert not [c for c in cands.values() if c["model_id"] == "gpt-6-astra" and c["effort"] == "low"]
    allow(world, "executor", label)
    cands, _ = build(world)
    [cand] = [c for c in cands.values() if c["model_id"] == "gpt-6-astra" and c["effort"] == "low"]
    assert cand["user_allowed"] is True


def test_a_size_scoped_user_allow_makes_a_disabled_route_a_candidate(world, monkeypatch):
    """Candidates are built before any task size is known; the allow must still admit the route (#850)."""
    _disable(monkeypatch, "gpt-6-luna", "low")
    world.user_config.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": "codex/gpt-6-luna@low", "state": "allow", "sizes": ["XL"]}]}}}))
    cands, _ = build(world)
    assert [c for c in cands.values() if c["model_id"] == "gpt-6-luna" and c["effort"] == "low"]
