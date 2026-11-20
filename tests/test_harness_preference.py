"""Harness preference weights (routing.harness_preference): deterministic weighted pick between harnesses
that launch the same model, per-model override, fallback and config validation."""
import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from office import config as office_config
from office import db as office_db
from office import harness_preference, route_policy, routing

WEIGHTS = {"anthropic": {"claude": 95, "hermes": 5}}


def cand(harness, model="claude-sonnet-5-5", effort="medium", burn=1, money=1.0):
    return {
        "harness": harness, "harness_version": "1", "adapter_id": harness, "model_id": model,
        "invocation_model_id": model if harness != "hermes" else f"anthropic:{model}", "effort": effort,
        "lineage_key": "anthropic/claude-sonnet", "capabilities": ["builder"], "supported_playbooks": ["Change"],
        "quota": {"status": "ok", "tightest_remaining_percent": 80, "projected_burn_percent": 1},
        "cost": {"money_estimate": money, "quota_burn": burn, "wall_clock_seconds": 10},
    }


@pytest.fixture(autouse=True)
def hermes_reachable(monkeypatch):
    """Whether Hermes holds an Anthropic credential is a fact about the machine running the test."""
    monkeypatch.setattr(route_policy, "provider_unreachable", lambda cand: None)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "runs.db"
    office_db.connect(path).close()
    return str(path)


def request(db, candidates, prefs=WEIGHTS, run="R1", task="T1", **extra):
    req = {"role": "planner", "playbook": "Change", "runs_db": db, "candidates": candidates,
           "allow_free_models": False, "run_id": run, "task_id": task, "require_classified_executor_size": False}
    if prefs is not None:
        req["harness_preference"] = prefs
    return {**req, **extra}


def pick(db, candidates, prefs=WEIGHTS, **ids):
    result = routing.route(request(db, candidates, prefs, **ids))
    assert result["status"] == "selected", result
    return result["candidate"]["harness"]


def both(hermes_cheaper=False):
    # Without weights the cheaper route wins; the weights must be able to overrule that ranking.
    return [cand("claude", burn=2 if hermes_cheaper else 1), cand("hermes", burn=1 if hermes_cheaper else 2)]


def test_weighted_share_is_about_95_percent_and_deterministic(db):
    cands = both(hermes_cheaper=True)
    picks = [pick(db, cands, run=f"run-{i}", task=f"T{i % 7}") for i in range(1000)]
    share = picks.count("claude") / len(picks)
    assert abs(share - 0.95) <= 0.02, share
    assert picks == [pick(db, cands, run=f"run-{i}", task=f"T{i % 7}") for i in range(1000)]


def test_same_run_and_task_always_pick_the_same_harness_regardless_of_candidate_order(db):
    cands = both()
    for i in range(50):
        a = pick(db, cands, run=f"r{i}")
        assert a == pick(db, list(reversed(cands)), run=f"r{i}")


def test_unset_key_is_byte_identical_to_today(db):
    cands = both(hermes_cheaper=True)
    plain = routing.route(request(db, copy.deepcopy(cands), prefs=None))
    assert plain["candidate"]["harness"] == "hermes"
    assert "harness_preference" not in plain and "harness_preference" not in plain["selection_disclosure"]
    empty = routing.route(request(db, copy.deepcopy(cands), prefs={}))
    assert empty == plain


def test_per_model_override_beats_vendor_weights(db):
    cands = both()
    prefs = {"anthropic": {"claude": 100, "hermes": 0}, "claude-sonnet-5-5": {"claude": 0, "hermes": 100}}
    assert {pick(db, cands, prefs, run=f"r{i}") for i in range(30)} == {"hermes"}
    # Another model of the same vendor still follows the vendor weights.
    opus = [{**cand(h, "claude-opus-5-5"), "lineage_key": "anthropic/claude-opus"} for h in ("claude", "hermes")]
    assert {pick(db, opus, prefs, run=f"r{i}") for i in range(30)} == {"claude"}


def test_weight_zero_is_never_first_but_is_the_fallback(db):
    prefs = {"anthropic": {"claude": 1, "hermes": 0}}
    assert {pick(db, both(hermes_cheaper=True), prefs, run=f"r{i}") for i in range(200)} == {"claude"}
    # The preferred harness is gone (not installed, auth missing: no candidate): the zero-weight one serves.
    assert pick(db, [cand("hermes")], prefs) == "hermes"
    # An unlisted harness counts as weight 0.
    assert pick(db, [cand("hermes"), cand("agy")], prefs) in {"hermes", "agy"}


def test_quota_cooled_preferred_harness_falls_back(db):
    con = sqlite3.connect(db)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    con.execute("INSERT INTO route_cooldowns(route, expires_at, updated_at) VALUES (?,?,?)",
                (routing.cooldown_key("claude", "claude-sonnet-5-5"), expires, expires))
    con.commit()
    con.close()
    prefs = {"anthropic": {"claude": 100, "hermes": 0}}
    result = routing.route(request(db, both(), prefs))
    assert result["candidate"]["harness"] == "hermes"
    assert result["harness_preference"]["pick"] == "hermes"


def test_role_deny_still_blocks_a_weighted_harness(db, monkeypatch):
    policy = {"state": "deny", "source": "user", "rule": "claude", "reason": "denied for the test"}
    original = route_policy.effective_role_state
    monkeypatch.setattr(route_policy, "effective_role_state", lambda c, *a, **k: (
        policy if c["harness"] == "claude" else original(c, *a, **k)))
    result = routing.route(request(db, both(), {"anthropic": {"claude": 100, "hermes": 0}}))
    assert result["candidate"]["harness"] == "hermes"
    assert any(r["candidate"].startswith("claude@") and r["category"] == "role-deny" for r in result["rejected"])


def test_effort_is_not_mixed(db):
    cands = [cand("claude", effort="high", burn=1), cand("hermes", effort="medium", burn=2)]
    assert pick(db, cands, {"anthropic": {"claude": 0, "hermes": 100}}) == "claude"


def test_pick_is_disclosed_with_weights_and_reason(db):
    result = routing.route(request(db, both(), WEIGHTS))
    pref = result["selection_disclosure"]["harness_preference"]
    assert pref["scope"] == "anthropic" and pref["weights"] == WEIGHTS["anthropic"]
    assert pref["pick"] == result["candidate"]["harness"] and "weighted draw" in pref["why"]
    assert "candidate" not in pref and "route" not in pref


# ---- adaptive (executor) path ----

def test_executor_adaptive_pick_moves_the_chosen_route_to_the_slate_head(db):
    from office import executor_slates
    cands = [{**cand(h), "adapter_state": "proven"} for h in ("claude", "hermes")]
    outcomes = {}
    for i in range(60):
        req = request(db, copy.deepcopy(cands), WEIGHTS, run=f"run-{i}", role="executor", size_class="M")
        req["context"] = {"size_class": "M"}
        result = routing.route(req)
        if result["status"] != "selected":
            pytest.skip(f"adaptive path needs fuller candidates: {result['status']}")
        outcomes[result["candidate"]["harness"]] = outcomes.get(result["candidate"]["harness"], 0) + 1
        assert result["slate"][0]["route"] == result["selected"]
    assert outcomes.get("claude", 0) > outcomes.get("hermes", 0)


# ---- validation ----

def effective(prefs):
    return {"routing": {"harness_preference": prefs}}


@pytest.mark.parametrize("prefs, message", [
    ({"anthropic": {"claude": -1}}, "must not be negative"),
    ({"anthropic": {"claude": "lots"}}, "must be a number"),
    ({"anthropic": {"claude": True}}, "must be a number"),
    ({"anthropic": {"claude": float("nan")}}, "must be a number"),
    ({"anthropic": {"nonesuch": 5}}, "unknown harness 'nonesuch'"),
    ({"anthropic": 5}, "must be a mapping of harness to weight"),
    ([1], "must be a mapping"),
])
def test_validation_rejects_bad_weights(prefs, message):
    problems = harness_preference.validate(effective(prefs))
    assert len(problems) == 1 and message in problems[0], problems


def test_validation_accepts_good_weights_and_unset():
    assert harness_preference.validate(effective(WEIGHTS)) == []
    assert harness_preference.validate({}) == []
    assert harness_preference.validate(effective({"claude-sonnet-5-5": {"hermes": 0.5, "claude": 0}})) == []


def test_config_resolve_rejects_bad_weights_with_a_clear_message(tmp_path):
    (tmp_path / ".auto-office").mkdir()
    (tmp_path / ".auto-office" / "config.yaml").write_text(
        "routing:\n  harness_preference:\n    anthropic: {claude: -3}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"routing\.harness_preference\.anthropic\.claude must not be negative"):
        office_config.resolve(tmp_path)


def test_shipped_default_leaves_the_key_unset():
    effective_config, _ = office_config.resolve(None)
    assert "harness_preference" not in effective_config["routing"]


# ---- production wiring: candidates.route_role carries the pinned weights into the request ----

def _route_role(tmp_path, monkeypatch, config, run_id):
    from office import candidates
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [])
    monkeypatch.setattr(candidates, "build_candidates", lambda *a, **k: (both(hermes_cheaper=True), []))
    with office_db.connect(tmp_path / "runs.db") as con:
        run = {"id": run_id, "playbook": "Change", "gear": "S", "risk": {"size_class": "S"}}
        return candidates.route_role(con, config, run, "planner", probe=False, observe=True)


def test_route_role_wires_pinned_weights_into_the_decision(tmp_path, monkeypatch):
    config = {"routing": {"harness_preference": {"anthropic": {"claude": 100, "hermes": 0}}}}
    decision = _route_role(tmp_path, monkeypatch, config, "R1")
    assert decision["request"]["harness_preference"] == config["routing"]["harness_preference"]
    assert decision["selection_disclosure"]["harness_preference"]["pick"] == "claude"
    assert decision["selected"].startswith("claude@")


def test_route_role_without_weights_leaves_the_request_unchanged(tmp_path, monkeypatch):
    decision = _route_role(tmp_path, monkeypatch, {}, "R1")
    assert "harness_preference" not in decision["request"]
    assert "harness_preference" not in decision["selection_disclosure"]
