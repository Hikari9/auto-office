"""#1163: an exact user allow makes Hydrogen a normal executor route behind an exact probe pass."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import db, dispatch, hydrogen_trials as h, route_probe, routing, state  # noqa: E402
from office.util import now_iso  # noqa: E402

SPEC = "agy/gemini-4-hydrogen@medium"


def _cand(effort: str = "medium") -> dict:
    spec = f"agy/gemini-4-hydrogen@{effort}"
    return {**route_probe.candidate_from_spec(spec), "harness_version": "1.2.3", "capabilities": ["code"],
            "dispatchable": True, "route_status": "available", "quota": {"status": "ok"}}


def _policy(route: str = SPEC, *, role: str = "executor", source: str = "user", **rule) -> dict:
    return {"denied": [], "role_rules": [{"role": role, "route": route, "state": "allow", **rule}],
            "strict_roles": [], "ceilings": {}, "ceiling_sources": {}, "overkill": [],
            "sources": {"role_rules": source}}


def _seed(con, cand: dict, result: str = "pass") -> None:
    adapter = route_probe._adapter(cand)
    fp = route_probe.fingerprint(cand, adapter)
    con.execute(
        "INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
        "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (route_probe.key(cand, adapter), fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
         fp["invocation_model_id"], fp["effort"], result, None if result == "pass" else "conformance-failed",
         f"seeded {result}", now_iso(), "run-1163", None, "att-1163"))
    con.commit()


def _route(tmp_path, cand, policy, *, size="S", role="executor", probe="pass"):
    db_path = tmp_path / "runs.db"
    with db.connect(db_path) as con:
        if probe:
            _seed(con, cand, probe)
    return routing.route({"role": role, "run_gear": size, "size_class": size, "runs_db": str(db_path),
                          "candidates": [cand], "user_policies": [policy], "allow_free_models": True}), db_path


def _hydrogen_rejections(decision):
    return [r for r in decision.get("rejected", []) if r.get("category") == "hydrogen-safety"]


def test_exact_user_allow_with_pass_selects_hydrogen_without_a_trial(tmp_path):
    cand = _cand()
    decision, db_path = _route(tmp_path, cand, _policy())
    assert decision["status"] == "selected", decision
    assert decision["selected"] == routing.candidate_id(cand)
    assert decision["candidate"]["hydrogen_user_allow"] is True
    assert not decision.get("trial") and not _hydrogen_rejections(decision)
    with db.connect(db_path) as con:  # cold start in effect, nothing booked by routing
        assert con.execute("SELECT COUNT(*) FROM hydrogen_trial_opportunities").fetchone()[0] == 0
        assert h.snapshot(con)["cold_start"] is True


@pytest.mark.parametrize("probe", [None, "fail"])
def test_user_allow_without_a_probe_pass_is_rejected(tmp_path, probe):
    decision, _ = _route(tmp_path, _cand(), _policy(), probe=probe)
    assert decision["selected"] is None
    assert any("conformance receipt" in r["reason"] for r in _hydrogen_rejections(decision)), decision


@pytest.mark.parametrize("name,policy,size,effort,role", [
    ("wildcard-route", _policy("agy/gemini-4-hydrogen"), "S", "medium", "executor"),
    ("wildcard-harness", _policy("harness:agy"), "S", "medium", "executor"),
    ("non-user-source", _policy(source="preferred_seed"), "S", "medium", "executor"),
    ("wrong-effort", _policy("agy/gemini-4-hydrogen@high"), "S", "medium", "executor"),
    ("effort-not-size-exact", _policy("agy/gemini-4-hydrogen@high"), "S", "high", "executor"),
    ("size-out-of-scope", _policy(sizes=["M"]), "S", "medium", "executor"),
    ("xl", _policy(), "XL", "medium", "executor"),
    ("reviewer", _policy(role="code_reviewer"), "S", "medium", "code_reviewer"),
])
def test_user_allow_does_not_widen_other_cases(tmp_path, name, policy, size, effort, role):
    decision, _ = _route(tmp_path, _cand(effort), policy, size=size, role=role)
    assert decision.get("selected") is None, (name, decision)


def test_user_allowed_helper_requires_exact_user_allow():
    ok = {"state": "allow", "source": "user", "rule": SPEC}
    assert h.user_allowed(ok)
    for bad in (None, {}, {**ok, "state": "neutral"}, {**ok, "source": "shipped"}, {**ok, "source": "default"},
                {**ok, "rule": "agy/gemini-4-hydrogen"}, {**ok, "rule": "harness:agy"}, {**ok, "rule": None},
                {**ok, "source": "user override receipt r1"}):
        assert not h.user_allowed(bad), bad


def test_without_allow_canary_refusals_are_unchanged():
    c = {"model_id": h.MODEL, "harness": "agy", "effort": "medium"}
    kw = {"role": "executor", "size": "S", "manual": False}
    assert h.route_refusal(c, **kw) == "automatic Hydrogen canary is disabled pending exact qualification"
    assert h.route_refusal(c, **kw, user_allowed=False, config={"routing": {"hydrogen_trials": {"enabled": True}}}
                           ) == "automatic Hydrogen requires a bounded, reversible, low-risk S/M task"
    # a user allow skips only the canary checks, never the exact/role/size vetoes
    assert h.route_refusal(c, **kw, user_allowed=True) is None
    assert h.route_refusal(c, role="code_reviewer", size="S", manual=False, user_allowed=True)
    assert h.route_refusal(c, role="executor", size="XL", manual=False, user_allowed=True)
    assert h.route_refusal({**c, "effort": "high"}, **kw, user_allowed=True)


def test_without_allow_routing_never_selects_hydrogen(tmp_path):
    decision, _ = _route(tmp_path, _cand(), {**_policy(), "role_rules": []})
    assert decision.get("selected") is None
    assert not any(r.get("candidate") == routing.candidate_id(_cand()) and "user allow" in str(r) for r in decision["rejected"])


def test_settle_discovery_launches_user_allowed_hydrogen_without_a_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "pinned_config", lambda run: {})
    run, task = {"id": "R1"}, {"id": "T1", "descriptor": {}}
    base = {"status": "selected", "selected": "x", "candidate": _cand()}
    with pytest.raises(dispatch.Refused) as refused:
        dispatch.settle_discovery(None, run, task, base, launching=True)
    assert refused.value.category == "hydrogen-trial-required"
    allowed = {**base, "candidate": {**_cand(), "hydrogen_user_allow": True}}
    settled = dispatch.settle_discovery(None, run, task, allowed, launching=True)
    assert settled["selected"] == "x" and "hydrogen_counter" not in settled


# H3 (#1168): Rico promoted Hydrogen to a voting code reviewer at the planted-bug effort (@high).
REVIEW_SPEC = "agy/gemini-4-hydrogen@high"


def test_exact_user_allow_makes_hydrogen_high_a_code_reviewer(tmp_path):
    cand = _cand("high")
    decision, _ = _route(tmp_path, cand, _policy(REVIEW_SPEC, role="code_reviewer"), role="code_reviewer")
    assert decision["status"] == "selected", decision
    assert decision["selected"] == routing.candidate_id(cand)
    assert decision["candidate"]["hydrogen_user_allow"] is True


@pytest.mark.parametrize("probe", [None, "fail"])
def test_code_reviewer_hydrogen_still_needs_a_fresh_probe_pass(tmp_path, probe):
    decision, _ = _route(tmp_path, _cand("high"), _policy(REVIEW_SPEC, role="code_reviewer"),
                         role="code_reviewer", probe=probe)
    assert decision["selected"] is None
    assert any("conformance receipt" in r["reason"] for r in _hydrogen_rejections(decision)), decision


@pytest.mark.parametrize("name,policy,effort,role", [
    ("no-allow", {**_policy(REVIEW_SPEC, role="code_reviewer"), "role_rules": []}, "high", "code_reviewer"),
    ("wildcard", _policy("agy/gemini-4-hydrogen", role="code_reviewer"), "high", "code_reviewer"),
    ("seed-source", _policy(REVIEW_SPEC, role="code_reviewer", source="preferred_seed"), "high", "code_reviewer"),
    ("medium", _policy(SPEC, role="code_reviewer"), "medium", "code_reviewer"),
    ("plan-reviewer", _policy(REVIEW_SPEC, role="plan_reviewer"), "high", "plan_reviewer"),
    ("visual-reviewer", _policy(REVIEW_SPEC, role="visual_reviewer"), "high", "visual_reviewer"),
    ("planner", _policy(REVIEW_SPEC, role="planner"), "high", "planner"),
])
def test_reviewer_promotion_is_exact_and_code_review_only(tmp_path, name, policy, effort, role):
    decision, _ = _route(tmp_path, _cand(effort), policy, role=role)
    assert decision.get("selected") is None, (name, decision)


def test_reviewer_refusal_reasons():
    c = {"model_id": h.MODEL, "harness": "agy", "effort": "high"}
    assert h.route_refusal(c, role="code_reviewer", size=None, manual=False, user_allowed=True) is None
    # H4 (#1178): a manual --review-as is not the user's exact allow.
    assert "exact allow" in h.route_refusal(c, role="code_reviewer", size=None, manual=True)
    assert h.route_refusal(c, role="code_reviewer", size=None, manual=True, user_allowed=True) is None
    assert "exact allow" in h.route_refusal(c, role="code_reviewer", size=None, manual=False)
    assert "exact agy/gemini-4-hydrogen@high" in h.route_refusal({**c, "effort": "medium"}, role="code_reviewer",
                                                                  size=None, manual=False, user_allowed=True)
    assert "never a worker, planner or other reviewer" in h.route_refusal(c, role="plan_reviewer", size=None,
                                                                           manual=True)
