"""#850 sections 1-3: the committed role policy is size-scoped allow / neutral / deny, from one data file."""
import pytest
import yaml
from office import route_policy

MAJOR = {"claude": "2.1.311", "agy": "1.0.0", "codex": "0.157.1", "opencode": None}


def state(role, harness, model, effort, size=None, policies=()):
    cand = {"harness": harness, "harness_version": MAJOR[harness], "model_id": model,
            "invocation_model_id": model, "effort": effort}
    return route_policy.effective_role_state(cand, role, list(policies), size)["state"]


def user(*rules):
    return {"role_rules": list(rules), "denied": [], "overkill": [], "ceilings": {}, "sources": {"role_rules": "user"}}


def test_user_rules_outrank_shipped_and_the_scope_precedence_holds():
    cand = ("claude", "claude-opus-5-5", "medium")
    pol = user({"role": "executor", "route": "claude@2/claude-opus-5-5@medium", "state": "allow"},
               {"role": "executor", "route": "claude@2/claude-opus-5-5@medium", "state": "deny", "sizes": ["XL"]})
    assert state("executor", *cand, "L", [pol]) == "allow"  # a user's pin beats the shipped L deny
    assert state("executor", *cand, "XL", [pol]) == "deny"  # a size-scoped user rule beats the unsized one
    neutral = user({"role": "executor", "route": "claude@2/claude-opus-5-5@medium", "state": "neutral", "sizes": ["L"]})
    assert state("executor", *cand, "L", [neutral]) == "neutral"  # clears one size


def _config(*rules):
    return {"routing": {"user_policy": {"role_rules": list(rules)}}}


def test_validate_user_policy_accepts_sizes_and_rejects_bad_ones():
    base = {"role": "executor", "route": "claude@2/claude-opus-5-5@medium", "state": "allow"}
    assert route_policy.validate_user_policy(_config({**base, "sizes": ["XL"]})) == []
    for sizes in ([], ["XXL"], "XL"):
        assert any("sizes" in p for p in route_policy.validate_user_policy(_config({**base, "sizes": sizes}))), sizes
    # The same route may carry different rules for different size scopes.
    assert route_policy.validate_user_policy(_config({**base, "sizes": ["XL"]}, {**base, "state": "deny", "sizes": ["L"]})) == []
    assert route_policy.validate_user_policy(_config({**base, "sizes": ["XL"]}, {**base, "sizes": ["XL"]}))


def test_policy_data_has_no_duplicate_rules_and_only_real_efforts():
    keys = [(r["role"], r["route"], tuple(r.get("sizes") or ()), r["state"]) for r in route_policy.SEED_ROLE_RULES]
    assert len(keys) == len(set(keys))
    for rule in route_policy.SEED_ROLE_RULES:
        assert rule["route"].rsplit("@", 1)[1] in route_policy.EFFORTS, rule


# --- CLI: size-scoped user rules (#850) -----------------------------------------------------------------------
def _isolated(tmp_path, monkeypatch):
    import subprocess
    target = tmp_path / "my-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(target))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return target


def test_route_change_writes_and_replaces_size_scoped_rules(tmp_path, monkeypatch):
    from office import routecmd
    target = _isolated(tmp_path, monkeypatch)
    routecmd.change("allow", role="executor", route="claude/claude-sonnet-5-5@high", sizes=["L", "XL"])
    routecmd.change("deny", role="executor", route="claude/claude-sonnet-5-5@high", sizes=["XS"])
    rules = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"]
    assert [(r["state"], r["sizes"]) for r in rules] == [("allow", ["L", "XL"]), ("deny", ["XS"])]
    # The same scope replaces its rule; clearing one scope leaves the other.
    routecmd.change("deny", role="executor", route="claude/claude-sonnet-5-5@high", sizes=["XL", "L"])
    routecmd.change("clear", role="executor", route="claude/claude-sonnet-5-5@high", sizes=["XS"])
    rules = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"]
    assert [(r["state"], r["sizes"]) for r in rules] == [("deny", ["L", "XL"])]
    with pytest.raises(Exception):
        routecmd.change("allow", role="executor", route="claude/claude-sonnet-5-5@high", sizes=["XXL"])


def test_route_policy_inspection_by_size(tmp_path, monkeypatch):
    from office import routecmd
    _isolated(tmp_path, monkeypatch)
    rows = {(r["route"], r["state"]) for r in routecmd.policy(role="executor", effective=True, size="XL").data["effective"]}
    assert ("claude/claude-opus-5-5@medium", "allow") in rows
    rows = {(r["route"], r["state"]) for r in routecmd.policy(role="executor", effective=True, size="L").data["effective"]}
    assert ("claude/claude-opus-5-5@medium", "deny") in rows
    with pytest.raises(Exception):
        routecmd.policy(role="executor", effective=True, size="XXL")


@pytest.mark.parametrize("sizes", [[""], ["", ""], ["XXL"], ["xs"], []])
def test_route_change_rejects_malformed_sizes_instead_of_widening_the_rule(tmp_path, monkeypatch, sizes):
    from office import routecmd
    from office.state import OfficeError
    target = _isolated(tmp_path, monkeypatch)
    with pytest.raises(OfficeError):
        routecmd.change("allow", role="executor", route="claude/claude-sonnet-5-5@high", sizes=sizes)
    assert not target.exists()


# --- Integration: routing.route and candidate building honour the shipped policy (#850) -------------------------
def _cand(harness, model, effort):
    return {"harness": harness, "harness_version": MAJOR[harness], "model_id": model, "invocation_model_id": model,
            "effort": effort, "capabilities": ["builder", "review"], "route_status": "available",
            "quota": {"status": "ok", "tightest_remaining_percent": 80},
            "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1}}


def _route(role, rows, size=None):
    from office import routing
    request = {"role": role, "candidates": rows, "preferred_seed": None, "user_policies": [], "runs_db": None,
               "policy": {"floor": None, "strict_seed": False, "required_capabilities": []}}
    if size:
        request.update({"run_gear": size, "size_class": size})
    return routing.route(request)


def test_a_size_scoped_allow_that_lifted_a_catalog_disable_does_not_cover_other_sizes():
    cand = {**_cand("claude", "claude-sonnet-5-5", "medium"), "user_allowed": True, "user_allow_sizes": ["M"]}
    for size in ("S", "L", None):
        got = _route("executor", [cand], size)
        assert got["status"] == "no_qualifying_candidate", size
        assert "covers task sizes M" in str(got.get("rejected")), size
    got = _route("executor", [cand], "M")
    assert not any("covers task sizes" in r.get("reason", "") for r in got.get("rejected", [])), got


def test_user_allows_reports_which_sizes_a_scoped_allow_covers():
    cand = {"harness": "codex", "harness_version": MAJOR["codex"], "model_id": "gpt-6-luna",
            "invocation_model_id": "gpt-6-luna", "effort": "low"}
    scoped = user({"role": "executor", "route": "codex@0/gpt-6-luna@low", "state": "allow", "sizes": ["XL", "L"]})
    assert route_policy.user_allows(cand, "executor", [scoped])["sizes"] == ["L", "XL"] or \
        sorted(route_policy.user_allows(cand, "executor", [scoped])["sizes"]) == ["L", "XL"]
    unsized = user({"role": "executor", "route": "codex@0/gpt-6-luna@low", "state": "allow"})
    assert route_policy.user_allows(cand, "executor", [unsized])["sizes"] is None
    assert route_policy.user_allows(cand, "executor", [user()]) is None


def test_a_shipped_deny_still_denies_when_the_harness_major_is_unknown():
    cand = {"harness": "codex", "harness_version": None, "model_id": "gpt-6-astra",
            "invocation_model_id": "gpt-6-astra", "effort": "medium"}
    assert route_policy.effective_role_state(cand, "executor", [], "M")["state"] == "deny"
    # An allow still needs the exact major, so an unknown one never grants.
    luna = {"harness": "codex", "harness_version": None, "model_id": "gpt-6-luna",
            "invocation_model_id": "gpt-6-luna", "effort": "high"}
    assert route_policy.effective_role_state(luna, "code_reviewer", [])["state"] == "neutral"
