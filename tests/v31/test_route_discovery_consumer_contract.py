"""#494 consumer verification of the core routing and probe interface.

This is T4's and T5's view of T2's interface, through the shared-contract names only:
`candidates.route_role` (the decision and its `discovery` block), `route_probe.ensure`
(the exact probe, with a minted `attempt_id`), `route_policy.new_attempt_id`, the
`route_discovery_events` rows T5 attributes by `attempt_id`, and `declared_decision` /
`declared_candidate` / `adaptive.apply_planner_choice` for manual and planned routes.

Unit-tier tests use an isolated Office home, one scripted fake `codex` that answers like
`codex exec` (tests/fixtures/route_probe/fake_harness.py) and a real runs.db. No model is
called. The `office` CLI tests at the end use the `env` fixture, so the suite's tiering
marks them integration: they run with `--all`.

S12 reference hashes were captured from base eaf155a source, not from this tree:
`git archive eaf155a`, then `routing.route(request)["decision_hash"]` for each request
that `recorded_requests()` returns, run with that tree's `src` first on `sys.path`.
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from office import adaptive, candidates, config as cfg, db, paths, route_policy, route_probe, routing, scoring, state

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests" / "fixtures" / "route_probe" / "fake_harness.py"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SOL = "gpt-6.1-sol"
AUTHORITY_TABLES = ("adapter_trust_acts", "recorded_overrides")


# ------------------------------------------------------------------ the world

class World:
    """An isolated Office home with one scripted `codex`, one runs.db, a repo for the repo config tier,
    and a run whose pinned config has discovery on and a 100 percent rolling cap."""

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp, self.monkeypatch = tmp, monkeypatch
        for name in ("bin", "data", "repo"):
            (tmp / name).mkdir()
        subprocess.run(["git", "-C", str(tmp / "repo"), "init", "-q"], check=True)  # the repo config tier needs a repository
        self.user_config = tmp / "user-config.yaml"
        self.repo_config = tmp / "repo" / ".auto-office" / "config.yaml"
        for key, value in (("OFFICE_DATA_HOME", tmp / "data"), ("OFFICE_STATE_HOME", tmp / "state"),
                           ("OFFICE_USER_CONFIG", self.user_config), ("OFFICE_QUOTA_PROBE", "off"),
                           ("CODEX_HOME", tmp / "codex-home"), ("CLAUDE_CONFIG_DIR", tmp / "claude-home")):
            monkeypatch.setenv(key, str(value))
        monkeypatch.chdir(tmp / "repo")  # manual-route denial reads the repo tier from the repository it runs in
        system = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp / "bin")] + system))
        self.codex = tmp / "bin" / "codex"
        self.codex.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
        self.codex.chmod(0o755)
        if route_probe.write_boundary_reason() is not None:
            # No OS write boundary here: the launch path still runs, only the sandbox wrapper is dropped.
            monkeypatch.setattr(route_probe, "write_boundary_reason", lambda: None)
            monkeypatch.setattr(route_probe, "_boundary_argv", lambda ws: [])
        self.script()
        self.con = db.connect(paths.runs_db())
        scoring.ensure_trust_schema(self.con)
        # The known-working fallback: the user's trust in every installed available route, as `env.trust()` records it.
        built, _ = candidates.build_candidates(self.con, "executor", probe=False)
        for c in built:
            scoring.record_trust_act(paths.runs_db(), routing.candidate_id(c), "proven", "user",
                                     "test fixture: user trusts the fake route")
        self.baseline = self.authority()
        self.config = self.policy()
        self.run = self.make_run()
        pool, _ = candidates.build_candidates(self.con, "executor", probe=False, user_policies=[],
                                              discovery={**route_policy.DISCOVERY_DEFAULTS, "enabled": True})
        self.sol = {c["effort"]: routing.candidate_id(c) for c in pool if c["invocation_model_id"] == SOL}
        assert set(self.sol) == set(EFFORTS), self.sol

    def script(self, **fields):
        self.monkeypatch.setenv("FAKE_PROBE", json.dumps(fields))

    def upgrade_harness(self, version):
        """The installed `codex` now reports another version. The version is memoized per binary mtime, so the
        wrapper is rewritten, as an upgrade replaces the binary."""
        self.script(version=f"codex-cli {version}")
        stat = self.codex.stat()
        self.codex.write_text(self.codex.read_text() + f"# {version}\n")
        os.utime(self.codex, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))

    def policy(self, **discovery):
        config = copy.deepcopy(cfg.resolve(None)[0])
        config["routing"]["discovery"].update({"enabled": True, "max_trial_percent_rolling_20": 100, **discovery})
        config["routing"]["adaptive"]["exploration"] = {"rate": 0.0, "margin": 1.0, "max_cost_vs_primary_percent": 100000}
        config[route_policy.DIGEST_KEY] = route_policy.policy_digest(config)
        return config

    def make_run(self, config=None, **risk):
        config = config or self.config
        return {"id": "run-A", "plan_version": 3, "playbook": "Change", "gear": "", "policy": config,
                "repo_root": str(self.tmp / "repo"),
                "risk": {"size_class": "S", "blast_radius": "repo", "irreversible": False, **risk}}

    def write_policy(self, tier, denied=(), overkill=()):
        """The user's policy in one config tier. `overkill` entries are rule mappings, as YAML flow text."""
        path = self.user_config if tier == "user" else self.repo_config
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["routing:", "  user_policy:", f"    denied_models: [{', '.join(denied)}]"]
        if overkill:
            lines.append("    overkill_rules:")
            lines += [f"      - {rule}" for rule in overkill]
        path.write_text("\n".join(lines) + "\n")

    def authority(self) -> dict:
        return {t: self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in AUTHORITY_TABLES}

    def decide(self, effort=None, *, role="executor", config=None, run=None, discovery_input=None, **kw):
        """`candidates.route_role` as T4's preflight calls it. `effort` keeps only that one discovery effort in play."""
        exclude = {rid for e, rid in self.sol.items() if effort and e != effort}
        return candidates.route_role(self.con, config or self.config, run or self.run, role, task_id="T1", probe=False,
                                     exclude=exclude or None, discovery_input=discovery_input, **kw)

    def candidate(self, decision, route):
        return next(c for c in decision["request"]["candidates"] if routing.candidate_id(c) == route)

    def ensure(self, decision, attempt_id=None, *, route=None, run=None, **context):
        """The exact probe for the candidate the decision chose (or `route`), with a minted attempt id."""
        attempt_id = attempt_id or route_policy.new_attempt_id()
        ctx = {"origin": "preflight", "role": "executor", "task_id": "T1", "reason": "discovery: untried candidate",
               "primary_route": decision["selected"], "fallback_route": decision["selected"], **context}
        result = route_probe.ensure(self.con, run or self.run, self.candidate(decision, route or decision["discovery"]["candidate"]),
                                    attempt_id=attempt_id, context=ctx, dispatch_id="Dprobe")
        return attempt_id, result

    def preflight(self, effort="high", **kw):
        """The D5 sequence: decision with `intent: "probe"`, `ensure` with a minted attempt id, recompute with the
        handle naming that one candidate. Returns (first decision, attempt id, ensure result, recompute)."""
        first = self.decide(effort)
        assert first["discovery"]["intent"] == "probe", first["discovery"]
        attempt_id, outcome = self.ensure(first, **kw)
        disc = first["discovery"]
        handle = {"candidate": disc["candidate"], "probe_key": disc["probe_key"], "reservation_id": attempt_id,
                  "attempt_id": attempt_id}
        return first, attempt_id, outcome, self.decide(effort, discovery_input=handle)

    def events(self, attempt_id=None):
        sql, args = "SELECT * FROM route_discovery_events", ()
        if attempt_id:
            sql, args = sql + " WHERE attempt_id=?", (attempt_id,)
        return [dict(r) for r in self.con.execute(sql + " ORDER BY seq", args)]

    def cache(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM route_probes")]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    # Every routing and probe call a test makes: no trust act, no recorded override.
    assert w.authority() == w.baseline, "a routing or probe call wrote an authority row"


def categories(decision):
    return {e["candidate"]: e["category"] for e in decision["rejected"] if e.get("category")}


def skipped(decision):
    """Candidates `build_candidates` left out of the request, by route label, with their category."""
    return {e["candidate"]: e["category"] for e in decision["skipped"] if e.get("category")}


# ------------------------------------------------------------------ D5: decision, ensure, recompute

def test_the_cold_decision_names_one_probe_candidate_and_keeps_the_known_working_primary(world):
    d = world.decide("high")
    disc = d["discovery"]
    assert d["status"] == "selected" and disc["active"] and disc["intent"] == "probe"
    assert disc["candidate"] == world.sol["high"] and disc["probe"] is None
    assert disc["probe_key"].startswith("codex|0.162.0|gpt-6.1-sol|high|") and disc["probe_key"].endswith("|worker")
    assert disc["fallback"] == d["selected"] and d["selected"] != disc["candidate"]
    assert "blocked" not in disc
    # T5 reads the caps and the candidate category from this decision
    assert disc["caps"]["probes"] == {"used": 0, "max": 2} and disc["caps"]["trials"] == {"used": 0, "max": 1}
    assert disc["caps"]["rolling"]["max"] == 1 and disc["caps"]["rolling"]["window"] == 1
    assert categories(d)[disc["candidate"]] == "probe-candidate"
    assert not [e for e in EFFORTS if e != "high" and world.sol[e] in categories(d)]  # the caller excluded them
    assert disc["candidate"] not in d["qualifying"] and all(e["route"] != disc["candidate"] for e in d["slate"])
    assert world.cache() == [] and world.events() == []  # deciding never probes


def test_d5_ends_in_a_trial_after_a_fresh_exact_pass(world):
    first, attempt, outcome, second = world.preflight("high")
    assert outcome["result"] == "pass" and outcome["freshness"] == "fresh-run" and outcome["attempt_id"] == attempt
    assert outcome["cached"] is False
    disc = second["discovery"]
    assert disc["intent"] == "trial" and disc["candidate"] == first["discovery"]["candidate"]
    assert disc["probe"]["result"] == "pass" and disc["probe"]["fresh"] is True and disc["probe"]["reason_class"] is None
    assert disc["fallback"] == first["selected"] and "blocked" not in disc
    assert (disc["attempt_id"], disc["reservation_id"]) == (attempt, attempt)
    assert disc["caps"]["probes"] == {"used": 1, "max": 2}  # the probe this attempt reserved is counted
    assert second["selected"] == disc["candidate"] and second["slate"][0]["rank"] == "PRIMARY"
    assert second["slate"][1]["route"] == disc["fallback"]
    # the candidate's row discloses why it is on the slate, and which attempt proved it
    row = next(r for r in second["routing"]["candidates"] if r["route"] == disc["candidate"])
    assert row["eligibility"]["source"] == "trial" and row["eligibility"]["attempt_id"] == attempt
    assert scoring.evaluate_trust_state(world.con, disc["candidate"])[1] == "valid-unverified"  # probe never grants trust
    # the recorded request replays to the same decision
    replay = routing.route(json.loads(json.dumps(second["request"])))
    assert replay["decision_hash"] == second["decision_hash"] and replay["selected"] == disc["candidate"]
    # the recompute tries the candidate the handle names and never draws another untried route
    wide = world.decide(None, discovery_input=second["request"]["discovery_input"])
    assert wide["discovery"]["intent"] == "trial" and wide["selected"] == disc["candidate"]
    assert {categories(wide)[world.sol[e]] for e in EFFORTS if e != "high"} == {"untried"}


def test_a_changed_harness_version_makes_the_cached_pass_unusable(world):
    first, _, outcome, _ = world.preflight("high")
    assert outcome["result"] == "pass"
    world.upgrade_harness("0.163.0")
    after = world.decide("high")
    disc = after["discovery"]
    assert disc["intent"] == "probe" and disc["probe"] is None  # the 0.162.0 pass is not this route's record
    assert "|0.163.0|" in disc["probe_key"] and disc["probe_key"] != first["discovery"]["probe_key"]
    assert [c["harness_version"] for c in world.cache()] == ["0.162.0"]


@pytest.mark.parametrize("mode,reason_class", [
    ("unsupported_effort", "unsupported-model-effort"), ("auth", "auth-quota-blocked"),
    ("transient", "transient"), ("no_write", "isolation-missing"), ("malformed", "conformance-failed")])
def test_d5_ends_in_none_with_blocked_when_the_probe_fails(world, mode, reason_class):
    world.script(mode=mode)
    first, attempt, outcome, second = world.preflight("high")
    assert (outcome["result"], outcome["reason_class"]) == ("fail", reason_class)
    disc = second["discovery"]
    assert disc["intent"] == "none" and disc["blocked"] == f"probe-failed:{reason_class}"
    assert disc["candidate"] == first["discovery"]["candidate"] and disc["probe"]["result"] == "fail"
    assert disc["probe"]["reason_class"] == reason_class
    assert disc["fallback"] is None and second["selected"] == first["selected"]  # nothing is tried: the primary stands
    assert categories(second)[disc["candidate"]] == "probe-failed"
    assert second["selected"] != disc["candidate"] and disc["candidate"] not in second["qualifying"]


def test_a_failed_effort_leaves_sibling_efforts_routable_and_a_pass_is_never_inherited(world):
    world.script(modes={"high": "unsupported_effort", "*": "pass"})
    _, _, outcome, blocked = world.preflight("high")
    assert outcome["reason_class"] == "unsupported-model-effort" and blocked["discovery"]["intent"] == "none"
    # medium is still discoverable: it gets its own exact probe and its own trial
    first, attempt, outcome, second = world.preflight("medium")
    assert outcome["result"] == "pass" and second["discovery"]["intent"] == "trial"
    assert second["discovery"]["candidate"] == world.sol["medium"]
    # a pass for medium says nothing about xhigh: no record on that candidate, so no trial
    probe = world.decide("xhigh")
    handle = {"candidate": world.sol["xhigh"], "reservation_id": "x", "attempt_id": "x",
              "probe_key": world.candidate(probe, world.sol["xhigh"])["probe_key"]}
    other = world.decide("xhigh", discovery_input=handle)
    assert other["discovery"]["intent"] == "none" and other["discovery"]["blocked"] == "probe-missing"
    assert other["discovery"]["probe"] is None and other["selected"] == probe["selected"]
    # and the failed route does not come back as a probe candidate
    again = world.decide("high")
    assert again["discovery"]["intent"] == "none" and categories(again)[world.sol["high"]] == "probe-failed"


def test_a_cached_pass_is_reused_by_a_later_attempt_without_a_second_probe(world):
    first, attempt, outcome, _ = world.preflight("high")
    later = world.decide("high")
    # the exact fresh record is now on the candidate, so the next cold decision still names a probe intent for it
    assert later["discovery"]["intent"] == "probe" and later["discovery"]["probe"]["result"] == "pass"
    second_attempt, cached = world.ensure(later)
    assert cached["cached"] is True and cached["result"] == "pass" and cached["freshness"] == "cached-fresh"
    assert cached["source_attempt_id"] == attempt and cached["attempt_id"] == second_attempt
    assert [e["kind"] for e in world.events(attempt)] == ["probe-reserved", "probe-result"]
    (hit,) = world.events(second_attempt)
    assert hit["kind"] == "probe-cache-hit" and hit["source_attempt_id"] == attempt
    assert hit["probe_freshness"] == "cached-fresh" and hit["outcome"] == "cache-hit:pass"
    assert len(world.cache()) == 1


def test_the_event_rows_t5_attributes_by_attempt_id_carry_the_audit_fields(world):
    first, attempt, _, _ = world.preflight("high")
    reserved, result = world.events(attempt)
    assert (reserved["kind"], result["kind"]) == ("probe-reserved", "probe-result")
    digest = world.config[route_policy.DIGEST_KEY]
    for row in (reserved, result):
        assert row["attempt_id"] == attempt and row["origin"] == "preflight"
        assert (row["run_id"], row["plan_version"], row["task_id"], row["role"]) == ("run-A", 3, "T1", "executor")
        assert row["dispatch_id"] == "Dprobe" and row["policy_digest"] == digest
        assert row["policy_version"] == route_policy.POLICY_VERSION
        assert row["candidate_route"] == world.sol["high"] and row["probe_key"] == first["discovery"]["probe_key"]
        assert (row["primary_route"], row["fallback_route"]) == (first["selected"], first["discovery"]["fallback"])
        assert row["reason"] == "discovery: untried candidate"
        fp = json.loads(row["fingerprint_json"])
        assert fp["harness"] == "codex" and fp["invocation_model_id"] == SOL and fp["effort"] == "high"
        assert fp["profile"] == "worker" and fp["harness_version"] == "0.162.0" and fp["adapter_hash"].startswith("sha256:")
        assert row["probe_key"] == "|".join(fp[k] for k in ("harness", "harness_version", "invocation_model_id", "effort",
                                                            "adapter_hash", "profile"))
        alloc = json.loads(row["allocation_json"])
        assert set(alloc) >= {"probes", "trials", "rolling"} and alloc["probes"]["max"] == 2
    assert (reserved["probe_freshness"], reserved["outcome"]) == ("none", "reserved")
    assert (result["probe_freshness"], result["outcome"], result["reason_class"]) == ("fresh-run", "pass", None)
    assert json.loads(reserved["allocation_json"])["probes"]["used"] == 1
    assert [e["attempt_id"] for e in world.events()] == [attempt, attempt]
    with pytest.raises(sqlite3.DatabaseError):  # append-only: an attributed row cannot be rewritten
        world.con.execute("UPDATE route_discovery_events SET outcome='x' WHERE attempt_id=?", (attempt,))


def test_a_probe_the_cap_refuses_leaves_the_recompute_blocked_and_launches_nothing(world):
    cap = world.policy(max_probes_per_run=1)
    run = world.make_run(cap)
    first = world.decide("high", config=cap, run=run)
    attempt, outcome = world.ensure(first, run=run)
    assert outcome["result"] == "pass"
    nxt = world.decide("medium", config=cap, run=run)
    assert nxt["discovery"]["caps"]["probes"] == {"used": 1, "max": 1}
    assert nxt["discovery"]["intent"] == "none" and nxt["discovery"]["blocked"] == "probe-cap"
    assert categories(nxt)[world.sol["medium"]] == "untried"
    # a preflight that asks anyway is refused, and the refusal is attributable
    refused_attempt, refused = world.ensure(nxt, route=world.sol["medium"], run=run)
    assert not refused and refused == route_probe.Refused("probe-cap") and refused.attempt_id == refused_attempt
    (row,) = world.events(refused_attempt)
    assert (row["kind"], row["outcome"], row["candidate_route"]) == ("probe-refused", "refused", world.sol["medium"])
    assert row["detail"].startswith("probe-cap") and json.loads(row["allocation_json"])["refused"].startswith("probe-cap")
    assert [f"{c['invocation_model_id']}@{c['effort']}" for c in world.cache()] == [f"{SOL}@high"]  # medium never launched


@pytest.mark.parametrize("gate,policy,risk", [
    ("risk", {}, {"size_class": "L"}), ("risk", {}, {"blast_radius": "prod"}), ("risk", {}, {"irreversible": True}),
    ("trial-cap", {"max_trials_per_run": 0}, {})])
def test_a_blocked_decision_is_none_with_blocked_and_nothing_is_probed(world, gate, policy, risk):
    config = world.policy(**policy)
    d = world.decide("high", config=config, run=world.make_run(config, **risk))
    assert d["status"] == "selected" and d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == gate
    assert d["discovery"]["candidate"] is None and d["selected"] not in world.sol.values()
    assert categories(d)[world.sol["high"]] == "untried"
    assert world.cache() == [] and world.events() == []


def test_a_discovery_that_is_off_refuses_the_probe_and_the_decision_has_no_discovery_block(world):
    off = world.policy(enabled=False)
    d = world.decide("high", config=off, run=world.make_run(off))
    assert "discovery" not in d and skipped(d)[f"codex/{SOL}@high"] == "untried"
    on = world.decide("high")
    attempt, refused = world.ensure(on, run=world.make_run(off))
    assert refused == route_probe.Refused("discovery-disabled")
    (row,) = world.events(attempt)
    assert row["kind"] == "probe-refused" and row["probe_freshness"] == "none"
    assert world.cache() == []


@pytest.mark.parametrize("role", ["planner", "plan_reviewer", "code_reviewer", "integration_reviewer"])
def test_only_executor_and_worker_decisions_discover(world, role):
    d = world.decide(None, role=role)
    assert "discovery" not in d
    assert all(c["route_status"] == "available" for c in d["request"]["candidates"])
    assert d["status"] == "selected" and d["selected"] not in world.sol.values()
    assert skipped(d)[f"codex/{SOL}@high"] == "untried"


# ------------------------------------------------------------------ S4: denial for discovery and probes

@pytest.mark.parametrize("tier", ["user", "repo"])
def test_a_denied_route_is_never_decided_for_discovery_and_names_its_source_tier(world, tier):
    world.write_policy(tier, denied=[f"codex/{SOL}@high"])
    d = world.decide("high")
    denied = [e for e in d["rejected"] if e.get("category") == "denied"]
    assert [e["candidate"] for e in denied] == [world.sol["high"]] and denied[0]["stage"] == 1
    assert denied[0]["reason"] == f"denied by {tier} routing.user_policy.denied_models: codex/{SOL}@high"
    assert d["discovery"]["intent"] == "none" and d["discovery"]["candidate"] is None
    assert world.cache() == [] and world.events() == []


def test_the_probe_refuses_a_route_the_runs_pinned_policy_denies_without_reserving_or_launching(world):
    pre = world.decide("high")
    pinned = copy.deepcopy(world.config)
    pinned["routing"]["user_policy"]["denied_models"] = [f"codex/{SOL}@high"]
    pinned[route_policy.PROVENANCE_KEY]["routing.user_policy.denied_models"] = "user"
    attempt, refused = world.ensure(pre, route=world.sol["high"], run=world.make_run(pinned))
    assert refused == route_probe.Refused("route-denied")
    assert refused.detail == f"denied by user routing.user_policy.denied_models: codex/{SOL}@high"
    (row,) = world.events(attempt)
    assert (row["kind"], row["outcome"]) == ("probe-refused", "refused")
    assert world.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0
    assert world.cache() == []


@pytest.mark.parametrize("tier", ["user", "repo"])
def test_the_probe_refuses_a_route_denied_after_the_run_pinned_its_config(world, tier):
    """A denial applies the moment the user writes it, even to a run pinned earlier (candidates.current_user_policies):
    a candidate the preflight decided on before the denial must not be probed once it is in force. The repo tier is
    reachable from the run's `repo_root` and from the cwd, which is the run's repository here."""
    pre = world.decide("high")
    world.write_policy(tier, denied=[f"codex/{SOL}@high"])
    attempt, refused = world.ensure(pre, route=world.sol["high"])
    assert refused == route_probe.Refused("route-denied"), refused
    assert f"denied by {tier}" in refused.detail
    assert world.con.execute("SELECT COUNT(*) FROM route_probe_reservations").fetchone()[0] == 0
    assert world.cache() == [] and [e["kind"] for e in world.events(attempt)] == ["probe-refused"]


@pytest.mark.parametrize("tier", ["shipped", "user", "repo"])
def test_the_budget_ceiling_and_its_source_tier_are_disclosed_and_shipped_sets_none(world, tier):
    if tier != "shipped":
        path = world.user_config if tier == "user" else world.repo_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("routing:\n  adaptive:\n    budget_ceiling_usd: 3\n")
    config = world.policy()
    resolved = cfg.resolve(world.tmp / "repo")[0]
    config["routing"]["adaptive"]["budget_ceiling_usd"] = resolved["routing"]["adaptive"]["budget_ceiling_usd"]
    config[route_policy.PROVENANCE_KEY] = resolved[route_policy.PROVENANCE_KEY]
    d = world.decide("high", config=config, run=world.make_run(config))
    want = {"usd": None if tier == "shipped" else 3.0, "source": tier}
    assert d["request"]["budget_ceiling"] == want and d["routing"]["budget_ceiling_source"] == tier
    assert d["request"]["adaptive_config"]["budget_ceiling_usd"] == want["usd"]


# ------------------------------------------------------------------ S12: decision_hash is unchanged

IDX = "Artificial Analysis Intelligence Index v4.3.2"


def cand(harness, model, effort="medium", *, out=2.0, inp=0.5, idx=45, tps=100.0, ttft=1000, quota=60,
         caps=("builder",)):
    return {
        "harness": harness, "harness_version": "2.0", "model_id": model, "invocation_model_id": model,
        "invocation_source": "documented:test", "effort": effort, "benchmark_indexes": {IDX: idx} if idx else {},
        "capabilities": list(caps), "price_fields": {"output_per_mtok": out, "input_per_mtok": inp},
        "speed_fields": {"output_tok_per_s": tps, "ttft_ms": ttft}, "cost": {"money_estimate": out},
        "quota": {"status": "ok", "tightest_remaining_percent": quota},
    }


def evidence(routes):
    out = {}
    for route, (s, f, attempts) in routes.items():
        out[route] = {"n_effective": s + f, "n_raw": s + f, "runs": 4, "successes": s, "failures": f,
                      "review_rounds_mean": 0.5, "attempts_mean": attempts, "wall_seconds_median": None,
                      "money_actual_median": None}
    return {"as_of": "2026-10-05T00:00:00+00:00", "routes": out, "pooling": {"prior_strength": 8},
            "context": {"role": "worker"}}


def request(cands, *, role="worker", ev=None, seed="seed-1", adaptive=None, **extra):
    return {"role": role, "playbook": "Change", "candidates": cands, "policy": {"cost_policy": "balanced"},
            "evidence": ev or {"routes": {}, "pooling": {"prior_strength": 8}}, "routing_seed": seed,
            "adaptive_config": {"exploration": {"rate": 0.0}, **(adaptive or {})}, **extra}


def recorded_requests():
    """Name -> request: the shapes `route_audit` rows record for executor/worker and reviewer decisions."""
    trio = [cand("claude", "opus", idx=55), cand("codex", "luna", idx=40, out=0.5), cand("agy", "flash", idx=42, out=1)]
    smart, plain = cand("a", "smart", idx=58), cand("b", "plain", idx=36)
    warm = evidence({"a/smart@medium": (4, 26, 2.0), "b/plain@medium": (27, 3, 1.1)})
    proven, fresh = cand("a", "m1", idx=50), cand("b", "m2", idx=49)
    proven_ev = evidence({"a/m1@medium": (20, 4, 1.1)})
    cheap, pricey = cand("codex", "luna", out=0.5, inp=0.1), cand("claude", "opus", out=20, inp=4, effort="xhigh")
    reviewers = [cand("codex", "luna", out=0.5), cand("claude", "opus", out=20)]
    for c in reviewers:
        c["capabilities"] = ["review"]
    return {
        "worker-three-strong": request(trio),
        "worker-local-evidence": request([smart, plain], ev=warm, adaptive={"competitive_band": 0.0}),
        "worker-close-call-draw": request([cand("a", "m1"), cand("b", "m2")], seed="fixed"),
        "worker-preference": request([cand("a", "m1", idx=46), cand("b", "m2", idx=45)],
                                     adaptive={"competitive_band": 0.0}, preferred_seed=[{"model_id": "m2"}]),
        "worker-wave-spread": request([cand("a", "m1", idx=46), cand("b", "m2", idx=45)],
                                      adaptive={"competitive_band": 0.0}, wave_load={"a@2/m1@medium": 1}),
        "worker-exploration": request([proven, fresh], ev=proven_ev,
                                      adaptive={"competitive_band": 0.0, "exploration": {"rate": 1.0}}),
        "pinned-ceiling-25": request([cheap, pricey], adaptive={"budget_ceiling_usd": 25}),
        "pinned-ceiling-1": request([cheap, pricey], adaptive={"budget_ceiling_usd": 1.0}),
        "reviewer-legacy": {"role": "plan_reviewer", "candidates": reviewers, "policy": {"cost_policy": "balanced"}},
    }


# decision_hash of each request above, computed by routing.route from the eaf155a tree.
BASE_EAF155A = {
    "worker-three-strong": "sha256:04c6dc5d95f977acec17c5cb84e23786c81d7d077e1625d01c5a54769e29c7b8",
    "worker-local-evidence": "sha256:8d6b7440aa3294fc8e5827784451bbe4cedcd43f96567564422427f5fe635c13",
    "worker-close-call-draw": "sha256:b3d8b053dbea83c6d4e9ccd664aae979a02636e642b1580e656976111d9f2481",
    "worker-preference": "sha256:a6b21f01459e0592b8c1263f615c902efd89427901cda3ccdc6621f09124260c",
    "worker-wave-spread": "sha256:2ad13869914d3d562b63b02854224f30ec853147e4cb6230dfeffbca2c22925f",
    "worker-exploration": "sha256:30447478e42ac83ab43130b6eb0f985f8f1910a3e07161aed620933a3f9fddec",
    "pinned-ceiling-25": "sha256:12c934de8c97398e0bd65dc5c25997e4efd23242849bdc4af2338797ba99ba02",
    "pinned-ceiling-1": "sha256:f1d4aadccae18083c9c93f71f498cde4779d9d00f5d34df4c8747d136ae7923d",
    "reviewer-legacy": "sha256:ece59075a9739fa6d397c868a32439e59b85e31dbcfd57bae0e4b456f7a8efc9",
}
ADAPTIVE = [n for n in BASE_EAF155A if n.startswith(("worker", "pinned"))]


def test_the_fixture_set_is_the_set_the_base_hashes_were_captured_for():
    assert set(recorded_requests()) == set(BASE_EAF155A)


@pytest.mark.parametrize("name", sorted(BASE_EAF155A))
def test_decision_hash_equals_the_hash_base_eaf155a_computed(name):
    decision = routing.route(recorded_requests()[name])
    assert decision["status"] == "selected" and decision["decision_hash"] == BASE_EAF155A[name]
    assert "discovery" not in decision and "discovery" not in decision.get("routing", {})


@pytest.mark.parametrize("name", sorted(BASE_EAF155A))
def test_a_recorded_request_replays_to_the_base_hash_after_a_json_round_trip(name):
    replayed = routing.route(json.loads(json.dumps(recorded_requests()[name])))
    assert replayed["decision_hash"] == BASE_EAF155A[name]


def untried(model="newmodel", effort="high"):
    return cand("codex", model, effort, idx=60) | {
        "route_status": "discovered-unconfirmed", "status_reason": "documented; probe required", "discovery": True,
        "probe_key": f"codex|2.0|{model}|{effort}|hash|worker", "probe": None}


def off_block(**settings):
    return {"settings": {**route_policy.DISCOVERY_DEFAULTS, "enabled": False, **settings},
            "allocation": {"probes": {"used": 0, "max": 2}, "trials": {"used": 0, "max": 1},
                           "rolling": {"used": 0, "max": 0, "window": 20, "warmup": True}},
            "known_working": [], "quarantined": [], "risk": {"size_class": "S", "blast_radius": "repo", "irreversible": False}}


@pytest.mark.parametrize("name", ADAPTIVE)
def test_discovery_off_and_untried_pool_rows_leave_the_hash_at_the_base_value(name):
    rq = recorded_requests()[name]
    rq["candidates"] = [*rq["candidates"], untried()]
    rq["discovery"] = off_block()
    decision = routing.route(rq)
    assert decision["decision_hash"] == BASE_EAF155A[name]
    assert "discovery" not in decision and categories(decision) == {"codex@2/newmodel@high": "untried"}
    # the same with the block absent: discovery was never configured for the run
    del rq["discovery"]
    assert routing.route(rq)["decision_hash"] == BASE_EAF155A[name]


@pytest.mark.parametrize("name", ADAPTIVE)
def test_available_candidates_in_the_new_shape_leave_the_hash_at_the_base_value(name):
    rq = recorded_requests()[name]
    for c in rq["candidates"]:
        c.update(route_status="available", status_reason="catalog invocation available", discovery=False, probe_key=None)
    assert routing.route(rq)["decision_hash"] == BASE_EAF155A[name]


def test_a_pinned_pre_change_run_never_discovers_and_keeps_its_own_ceiling(world):
    pinned = copy.deepcopy(world.config)
    for key in (route_policy.PROVENANCE_KEY, route_policy.DIGEST_KEY):
        pinned.pop(key)
    pinned["routing"]["adaptive"]["budget_ceiling_usd"] = 25
    assert route_policy.discovery_settings(pinned)["enabled"] is False  # discovery.enabled=true in a pin is not honoured
    run = world.make_run(pinned)
    pinned_on = world.decide("high", config=pinned, run=run)
    off = copy.deepcopy(pinned)
    off["routing"]["discovery"]["enabled"] = False
    pinned_off = world.decide("high", config=off, run=run)
    for d in (pinned_on, pinned_off):
        assert "discovery" not in d and "discovery" not in d["request"]
        assert all(c["route_status"] == "available" for c in d["request"]["candidates"])
        assert d["request"]["adaptive_config"]["budget_ceiling_usd"] == 25 and "budget_ceiling" not in d["request"]
        assert skipped(d)[f"codex/{SOL}@high"] == "untried"
    assert pinned_on["decision_hash"] == pinned_off["decision_hash"] and pinned_on["selected"] == pinned_off["selected"]
    replay = routing.route(json.loads(json.dumps(pinned_on["request"])))
    assert replay["decision_hash"] == pinned_on["decision_hash"]


def test_a_run_with_discovery_off_records_a_request_that_replays_and_names_no_discovery(world):
    off = world.policy(enabled=False)
    d = world.decide("high", config=off, run=world.make_run(off))
    assert "discovery" not in d["request"] and "discovery_input" not in d["request"]
    assert routing.route(json.loads(json.dumps(d["request"])))["decision_hash"] == d["decision_hash"]
    assert skipped(d)[f"codex/{SOL}@high"] == "untried"


# ------------------------------------------------------------------ S4 and S5: manual routes

DENIED = f"codex/{SOL}@high"


def deny(world, tier="user", spec=DENIED):
    world.write_policy(tier, denied=[spec])


@pytest.mark.parametrize("tier", ["user", "repo"])
@pytest.mark.parametrize("spec", [f"codex/{SOL}@high", f"codex/{SOL}", SOL, "harness:codex"])
def test_a_denied_route_is_refused_for_as_and_review_as(world, spec, tier):
    deny(world, tier, spec)
    for flag in ("--as", "--review-as"):
        with pytest.raises(state.Refused) as err:
            candidates.declared_decision(DENIED, flag=flag)
        assert err.value.category == "route-denied"
        assert f"denied by {tier} routing.user_policy.denied_models: {spec}" in err.value.message
        assert "office config routing.user_policy.denied_models" in err.value.next_step


def test_amend_route_and_dispatch_declare_through_the_same_denial_seam(world):
    """`amend route --as` and `dispatch --as` both call `declared_candidate`, which names only the exact route."""
    deny(world)
    with pytest.raises(state.Refused) as err:
        candidates.declared_candidate("codex", SOL, "high")
    assert err.value.category == "route-denied"
    assert candidates.declared_candidate("codex", SOL, "low")["override"] is True  # a sibling effort stays declarable
    world.user_config.write_text("routing: {}\n")  # only an explicit policy change re-enables the route
    assert candidates.declared_candidate("codex", SOL, "high")["override"] is True


def test_route_flag_refuses_a_denied_route_and_a_plan_route_choice_is_ignored_with_the_reason(world):
    deny(world)
    forced = world.decide("high", override=DENIED)
    assert forced["status"] != "selected" and forced["selected"] is None
    world.user_config.write_text("routing: {}\n")  # the same `--route`, undenied, selects: the denial is the cause
    assert world.decide(None, override="codex/gpt-6-astra@low")["selected"] == "codex@0/gpt-6-astra@low"
    deny(world)
    auto = world.decide(None)
    audit, denied = auto["routing"], [e for e in auto["rejected"] if e.get("category") == "denied"]
    assert {e["candidate"] for e in denied} == {world.sol["high"]}
    choice = adaptive.apply_planner_choice(audit, {"routes": [world.sol["high"]], "why": "the planner prefers it here"})
    assert "rejected at stage 1" in choice["planner_error"] and "denied by user" in choice["planner_error"]
    assert choice["chooser"] == "router" and choice["primary"] == audit["slate"][0]["route"]


def test_shipped_defaults_contain_no_denied_or_overkill_entries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    assert cfg.resolve(None)[0]["routing"]["user_policy"] == {"denied_models": [], "overkill_rules": []}
    assert candidates.current_user_policies() == []


def test_an_overkill_route_is_skipped_only_in_matching_automatic_scope_and_stays_manually_selectable(world):
    primary = world.decide(None)["selected"]
    spec = primary.split("/", 1)[1]  # model@effort, without the harness major
    route = f"codex/{spec}"
    world.write_policy("user", overkill=[f"{{route: {route}, roles: [executor], size_classes: [S]}}"])
    skipped = world.decide(None)
    assert skipped["selected"] != primary and categories(skipped)[primary] == "overkill"
    entry = next(e for e in skipped["rejected"] if e["candidate"] == primary)
    assert entry["stage"] == 1 and entry["reason"] == f"overkill by user routing.user_policy.overkill_rules: {route}"
    # outside the rule's scope the same route stays automatic
    for scope in ("{route: %s, roles: [worker]}" % route, "{route: %s, size_classes: [L]}" % route):
        world.write_policy("user", overkill=[scope])
        outside = world.decide(None)
        assert outside["selected"] == primary and "overkill" not in categories(outside).values(), scope
    # inside the scope an explicit manual route still selects it, and a declared route is never blocked
    world.write_policy("user", overkill=[f"{{route: {route}, roles: [executor], size_classes: [S]}}"])
    manual = world.decide(None, override=route)
    assert manual["selected"] == primary and "overkill" not in categories(manual).values()
    assert candidates.declared_decision(route)["selected"].endswith(spec)


# ------------------------------------------------------------------ the office CLI (integration tier)

PLAN_WITH_ROUTE = """# Plan

## Requirements
done:
- add() returns the sum
blast_radius: repo
non_goals:
- no CLI

## Tasks
### T1: Implement add
scope: calc.py
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
route: {route}
route_why: the planner wants this route for this task
visual: none
"""
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
CLI_DENIED = "codex/gpt-6-astra@low"


def cli_authority(env):
    con = env.con()
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in AUTHORITY_TABLES}


def cli_deny(env, spec=CLI_DENIED):
    env.tmp.joinpath("user-config.yaml").write_text(f"routing:\n  user_policy:\n    denied_models: [{spec}]\n")


def test_cli_manual_routes_refuse_a_denied_route_and_change_nothing(env):
    from conftest import approved_run
    approved_run(env)
    before = cli_authority(env)
    cli_deny(env)
    con = env.con()
    dispatches = con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    routes = con.execute("SELECT COUNT(*) FROM events WHERE kind='route.changed'").fetchone()[0]
    for args in (("dispatch", "T1", "--as", CLI_DENIED),
                 ("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--review-as", CLI_DENIED),
                 ("dispatch", "T1", "--review-as", CLI_DENIED),
                 ("dispatch", "T1", "--route", CLI_DENIED),
                 ("amend", "route", "T1", "--as", CLI_DENIED, "--quote", "use it")):
        code, out = env.office(*args, env=EXTERNAL)
        assert code != 0 and "denied by user routing.user_policy.denied_models" in out, (args, out)
        if "--route" not in args:  # `--route` is routed, not declared: it fails as "no qualifying route"
            assert "office config routing.user_policy.denied_models" in out, (args, out)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == dispatches
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='route.changed'").fetchone()[0] == routes
    assert cli_authority(env) == before
    cli_deny(env, spec="gpt-9-nothing")  # a policy change re-enables the route
    code, out = env.office("dispatch", "T1", "--as", CLI_DENIED, env=EXTERNAL)
    assert code == 0, out


def cli_overkill(env):
    env.tmp.joinpath("user-config.yaml").write_text(
        f"routing:\n  user_policy:\n    overkill_rules:\n      - {{route: {CLI_DENIED}}}\n")


def test_cli_an_overkill_route_stays_selectable_by_as_and_amend_route(env):
    from conftest import approved_run
    approved_run(env)
    before = cli_authority(env)
    cli_overkill(env)
    code, out = env.office("amend", "route", "T1", "--as", CLI_DENIED, "--quote", "use astra here")
    assert code == 0 and "declared" in out, out
    (event,) = [json.loads(r[0]) for r in env.con().execute("SELECT payload_json FROM events WHERE kind='route.changed'")]
    assert event["after"] == CLI_DENIED.replace("codex/", "codex@1/") and event["actor"] == "user"
    code, out = env.office("dispatch", "T1", "--as", CLI_DENIED, env=EXTERNAL)
    assert code == 0 and "(user override)" in out, out
    assert env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0] \
        == CLI_DENIED.replace("codex/", "codex@1/")
    assert cli_authority(env) == before


def test_cli_a_declared_overkill_route_is_still_followed_by_the_next_plain_dispatch(env):
    """A route the user declared (`amend route --as`) is manual: dispatch restores it as recorded (#426), and an
    overkill rule, which only skips automatic selection, must not make it unavailable."""
    from conftest import approved_run
    approved_run(env)
    cli_overkill(env)
    code, out = env.office("amend", "route", "T1", "--as", CLI_DENIED, "--quote", "use astra here")
    assert code == 0, out
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    assert code == 0 and "overkill" not in out, out
    assert env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0] \
        == CLI_DENIED.replace("codex/", "codex@1/")


def test_cli_a_plan_route_naming_a_denied_route_is_ignored_and_says_why(env):
    from conftest import start_inline
    env.trust()
    before = cli_authority(env)
    cli_deny(env)
    out = start_inline(env, plan=PLAN_WITH_ROUTE.format(route=CLI_DENIED))
    assert "T1 route:" in out and "rejected at stage 1" in out and "denied by user" in out, out
    assert "the ranked slate stands" in out, out
    assert cli_authority(env) == before
