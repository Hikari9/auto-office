"""H5 (Rico 2026-11-15): Hydrogen also reviews plans, as the exact user-allowed agy/gemini-4-hydrogen@high.

Same gates as the H3/H4 code reviewer: the user's exact allow for the role and a fresh exact probe,
through the route-scoped read-only agy `reviewer` profile. Visual review stays vetoed (agy's `vision`
profile has no write barrier, Luna on H5).
"""
import pytest

from office import adapters, candidates, configcmd, db, hydrogen_trials, route_policy, route_probe, routecmd, routing
from office.util import now_iso

HYDROGEN = "agy/gemini-4-hydrogen@high"
ALLOW = "agy@1/gemini-4-hydrogen@high"
SEED = {"model_id": "gemini-4-hydrogen", "harness": "agy", "effort": "high"}
CAND = {"model_id": hydrogen_trials.MODEL, "harness": "agy", "effort": "high",
        "invocation_model_id": "gemini-4-hydrogen-high"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [])
    monkeypatch.setattr(adapters, "installed", lambda _a: True)
    monkeypatch.setattr(adapters, "route_version", lambda *_a: "1.2.3")
    return tmp_path


def _config(role="plan_reviewer", route=ALLOW):
    return {"routing": {"user_policy": {"role_rules": [{"role": role, "route": route, "state": "allow"}]}}}


def _probe_pass(con, cand):
    agy = adapters.load_all()["agy"]
    fp = route_probe.fingerprint(cand, agy)
    con.execute("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                "invocation_model_id, effort, result, probed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (route_probe.key(cand, agy), fp["harness"], fp["harness_version"], fp["adapter_hash"],
                 fp["profile"], fp["invocation_model_id"], fp["effort"], "pass", now_iso()))
    con.commit()


def _live(monkeypatch, cfg):
    """`cfg`'s user policy as the user's live policy: routing reads it, not a run's pinned copy (#1120)."""
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [route_policy.user_policy(cfg)])


def _route(env, cfg, *, probe=True):
    with pytest.MonkeyPatch.context() as mp, db.connect(env / "runs.db") as con:
        _live(mp, cfg)
        built, _ = candidates.build_candidates(con, "plan_reviewer", probe=False,
                                               allow_policies=[route_policy.user_policy(cfg)])
        cand = next((c for c in built if hydrogen_trials.is_hydrogen(c)), None)
        if cand and probe:
            _probe_pass(con, cand)
        seeded = {**cfg, "roles": {"plan_reviewer": {"preferred_seed": [SEED]}}}
        return cand, candidates.route_role(con, seeded, {"gear": "S", "risk": {"size_class": "S"}},
                                           "plan_reviewer", probe=False, observe=True)


def test_plan_reviewer_routes_to_the_allowed_hydrogen_after_a_probe(env):
    cand, decision = _route(env, _config())
    assert cand and (cand["effort"], cand["harness"]) == ("high", "agy")
    assert adapters.profile_for(adapters.load_all()["agy"], "reviewer", cand["invocation_model_id"], "high")
    assert decision["selected"] == routing.candidate_id(cand), decision


@pytest.mark.parametrize("name,cfg,probe", [
    ("no-probe", _config(), False),
    ("code-reviewer-allow-only", _config(role="code_reviewer"), True),
    ("medium-allow", _config(route="agy@1/gemini-4-hydrogen@medium"), True),
    ("all-role-allow", _config(role="*"), True),
])
def test_plan_reviewer_hydrogen_still_needs_the_exact_allow_and_probe(env, name, cfg, probe):
    _cand, decision = _route(env, cfg, probe=probe)
    selected = decision.get("selected") or ""
    assert "gemini-4-hydrogen" not in selected, (name, decision)


def test_refusals_open_plan_review_only_under_the_exact_allow():
    r = hydrogen_trials.route_refusal
    assert r(CAND, role="plan_reviewer", size=None, manual=False, user_allowed=True) is None
    assert "exact allow" in r(CAND, role="plan_reviewer", size=None, manual=True)
    assert "exact agy/gemini-4-hydrogen@high" in r({**CAND, "effort": "medium"}, role="plan_reviewer",
                                                   size=None, manual=False, user_allowed=True)


@pytest.mark.parametrize("role", ["visual_reviewer", "integration_reviewer", "planner", "worker"])
def test_other_roles_stay_vetoed(role):
    why = hydrogen_trials.route_refusal(CAND, role=role, size=None, manual=True, user_allowed=True)
    assert "code or plan reviewer only" in why, role


def test_visual_reviewer_never_builds_hydrogen_even_with_the_exact_allow(env):
    cfg = _config(role="visual_reviewer")
    with db.connect(env / "runs.db") as con:
        built, _ = candidates.build_candidates(con, "visual_reviewer", probe=False,
                                               allow_policies=[route_policy.user_policy(cfg)])
    assert not [c for c in built if hydrogen_trials.is_hydrogen(c)]


def test_a_pinned_hydrogen_plan_reviewer_needs_only_the_run(env, monkeypatch):
    from office.state import Refused
    db.connect().close()
    with pytest.raises(Refused, match="exact allow"):
        candidates.declared_decision(HYDROGEN, flag="--as", role="plan_reviewer", config={}, run_id="r1")
    _live(monkeypatch, _config())
    with pytest.raises(Refused, match="conformance receipt"):
        candidates.declared_decision(HYDROGEN, flag="--as", role="plan_reviewer", config=_config(), run_id="r1")


def test_seed_accepts_hydrogen_behind_luna_for_plan_review_with_the_allow():
    luna = {"model_id": "luna", "harness": "codex", "effort": "high"}
    assert configcmd.validate_seed([luna, SEED], role="plan_reviewer", config=_config()) == []
    assert any("not dispatchable" in p for p in configcmd.validate_seed([luna, SEED], role="plan_reviewer",
                                                                         config=_config(role="code_reviewer")))


def test_route_policy_names_the_voting_review_role():
    row = {"invocation_harness": "agy", "model_id": "gemini-4-hydrogen", "effort": "high"}
    state = {"state": "allow", "source": "user", "rule": ALLOW}
    ok, why = routecmd._dispatch_view(row, state, True, True, candidate=dict(CAND), role="plan_reviewer",
                                      probe={"result": "pass"})
    assert ok is True and why.startswith("user allow; exact probe passed; a voting plan reviewer"), why
    assert "reviews code and plans only" in why
    ok, why = routecmd._dispatch_view(row, state, True, True, candidate=dict(CAND), role="plan_reviewer")
    assert ok is False and why.startswith("user allow as a plan reviewer")
    ok, why = routecmd._dispatch_view(row, state, True, True, candidate=dict(CAND), role="visual_reviewer",
                                      probe={"result": "pass"})
    assert ok is not True and "code or plan reviewer only" in why, why


def test_policy_header_names_the_hydrogen_gates():
    """Luna recheck on H5: the #845 header must not say an allowed Hydrogen route dispatches without proof."""
    text = "\n".join(routecmd.policy(role="visual_reviewer").lines)
    assert "except Hydrogen, which also needs a fresh exact probe" in text


def test_an_all_role_allow_is_not_the_exact_allow():
    """Luna on H5: `role: "*"` matches every role in effective_role_state, but it does not name the role."""
    cand = {"harness": "agy", "harness_version": "1.2.3", "model_id": hydrogen_trials.MODEL,
            "invocation_model_id": "gemini-4-hydrogen-high", "effort": "high"}
    for cfg, want in ((_config(role="*"), False), (_config(), True)):
        state = route_policy.effective_role_state(cand, "plan_reviewer", [route_policy.user_policy(cfg)])
        assert state["state"] == "allow" and hydrogen_trials.user_allowed(state) is want, (cfg, state)
