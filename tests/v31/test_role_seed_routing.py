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


# (harness, model, effort, size, expected): the section 1 table.
EXECUTOR = [
    ("claude", "claude-haiku-5-5", "medium", "XS", "allow"), ("claude", "claude-haiku-5-5", "high", "S", "allow"),
    ("claude", "claude-haiku-5-5", "high", "XS", "deny"), ("claude", "claude-haiku-5-5", "xhigh", "M", "deny"),
    ("claude", "claude-haiku-5-5", "max", "XL", "deny"), ("claude", "claude-haiku-5-5", "low", "XS", "neutral"),
    ("claude", "claude-haiku-5-5", "medium", "M", "neutral"),
    ("agy", "gemini-3.8-flash", "low", "XS", "allow"), ("agy", "gemini-3.8-flash", "medium", "S", "allow"),
    ("agy", "gemini-3.8-flash", "high", "XS", "deny"), ("agy", "gemini-3.8-flash", "high", "S", "deny"),
    ("agy", "gemini-3.8-flash", "high", "M", "neutral"), ("agy", "gemini-3.8-flash", "max", "L", "deny"),
    ("codex", "gpt-6.1-terra", "low", "XS", "allow"), ("codex", "gpt-6.1-terra", "medium", "S", "allow"),
    ("codex", "gpt-6.1-terra", "medium", "M", "allow"), ("codex", "gpt-6.1-terra", "high", "L", "allow"),
    ("codex", "gpt-6.1-terra", "high", "M", "deny"), ("codex", "gpt-6.1-terra", "xhigh", "L", "deny"),
    ("codex", "gpt-6.1-terra", "medium", "L", "neutral"),
    ("claude", "claude-sonnet-5-5", "medium", "M", "allow"), ("agy", "claude-sonnet-5-5", "medium", "M", "allow"),
    ("claude", "claude-sonnet-5-5", "high", "L", "allow"), ("agy", "claude-sonnet-5-5", "high", "L", "allow"),
    ("claude", "claude-sonnet-5-5", "high", "S", "deny"), ("agy", "claude-sonnet-5-5", "high", "XS", "deny"),
    ("claude", "claude-sonnet-5-5", "xhigh", "XL", "deny"), ("claude", "claude-sonnet-5-5", "low", "S", "neutral"),
    ("claude", "claude-sonnet-5-5", "high", "XL", "neutral"),
    ("opencode", "glm-5.3-flash", "max", "M", "allow"), ("opencode", "glm-5.3-flash", "max", "L", "neutral"),
    ("claude", "claude-opus-5-5", "medium", "XL", "allow"), ("claude", "claude-opus-5-5", "medium", "L", "deny"),
    ("claude", "claude-opus-5-5", "medium", "XS", "deny"), ("claude", "claude-opus-5-5", "high", "XL", "deny"),
    ("claude", "claude-opus-5-5", "low", "XL", "neutral"), ("agy", "claude-opus-5-5", "medium", "XL", "deny"),
    ("codex", "gpt-6.1-sol", "medium", "XL", "allow"), ("codex", "gpt-6.1-sol", "high", "XL", "deny"),
    ("codex", "gpt-6.1-sol", "low", "XL", "neutral"), ("codex", "gpt-6.1-sol", "medium", "L", "neutral"),
    ("agy", "gemini-4-argon", "medium", "L", "allow"), ("agy", "gemini-4-argon", "high", "XL", "allow"),
    ("agy", "gemini-4-argon", "high", "L", "deny"), ("agy", "gemini-4-argon", "low", "L", "neutral"),
]


@pytest.mark.parametrize("harness,model,effort,size,expected", EXECUTOR)
def test_executor_matrix_is_size_scoped(harness, model, effort, size, expected):
    assert state("executor", harness, model, effort, size) == expected


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"])
@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("size", [None, "XS", "M", "XL"])
def test_astra_and_5_6_are_never_executors_at_any_size(model, effort, size):
    assert state("executor", "codex", model, effort, size) == "deny"


def test_astra_denial_does_not_spill_into_planning():
    assert state("planner", "codex", "gpt-6-astra", "medium", "L") == "neutral"


def test_size_scoped_rules_do_not_apply_to_an_unknown_size():
    assert state("executor", "claude", "claude-sonnet-5-5", "high", None) == "neutral"
    assert state("executor", "claude", "claude-opus-5-5", "medium", None) == "neutral"
    # An unsized rule still applies.
    assert state("executor", "claude", "claude-sonnet-5-5", "xhigh", None) == "deny"


def test_sonnet_high_is_denied_for_s_and_only_a_users_size_scoped_rule_re_allows_it():
    assert state("executor", "claude", "claude-sonnet-5-5", "high", "S") == "deny"
    pol = user({"role": "executor", "route": "claude@2/claude-sonnet-5-5@high", "state": "allow", "sizes": ["S"]})
    assert state("executor", "claude", "claude-sonnet-5-5", "high", "S", [pol]) == "allow"
    assert state("executor", "claude", "claude-sonnet-5-5", "high", "XS", [pol]) == "deny"


@pytest.mark.parametrize("role", ["code_reviewer", "plan_reviewer"])
def test_reviewers_section_2(role):
    assert state(role, "codex", "gpt-6-luna", "high") == "allow"
    assert state(role, "codex", "gpt-6-luna", "low") == "neutral"
    assert state(role, "codex", "gpt-6-luna", "medium") == "neutral"
    assert state(role, "codex", "gpt-6-luna", "xhigh") == "deny"
    assert state(role, "codex", "gpt-6-luna", "max") == "deny"
    assert state(role, "opencode", "deepseek-v4.1-flash-free", "max") == "allow"
    for harness in ("claude", "agy"):
        assert state(role, harness, "claude-opus-5-5", "low") == "allow"
        for effort in ("medium", "high", "xhigh", "max"):
            assert state(role, harness, "claude-opus-5-5", effort) == "deny"
        for effort in ("medium", "high"):
            assert state(role, harness, "claude-sonnet-5-5", effort) == "deny"
        # Sonnet reviewer denials are not widened to unapproved efforts.
        for effort in ("low", "xhigh", "max"):
            assert state(role, harness, "claude-sonnet-5-5", effort) == "neutral"


def test_visual_reviewer_section_3():
    for effort in ("low", "medium"):
        assert state("visual_reviewer", "agy", "gemini-3.8-flash", effort) == "allow"
    assert state("visual_reviewer", "codex", "gpt-6.1-terra", "low") == "allow"
    assert state("visual_reviewer", "codex", "gpt-5.6-terra", "low") == "deny"
    assert state("visual_reviewer", "codex", "gpt-5.6-terra", "high") == "neutral"
    assert state("visual_reviewer", "claude", "claude-sonnet-5-5", "high") == "neutral"
    assert state("visual_reviewer", "codex", "gpt-6-luna", "high") == "neutral"


def test_the_5_6_executor_deny_is_not_a_cross_role_ban():
    for role in ("code_reviewer", "plan_reviewer", "planner"):
        assert state(role, "codex", "gpt-5.6-terra", "high") == "neutral"


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


def test_default_config_reviewer_and_visual_seed_order():
    roles = yaml.safe_load(open("config/config.default.yaml"))["roles"]
    order = [(e["model_id"], e["harness"], e["effort"]) for e in roles["code_reviewer"]["preferred_seed"]]
    assert order == [("luna", "codex", "high"), ("deepseek-v4.1-flash-free", "opencode", "max"),
                     ("opus", "claude", "low"), ("claude-opus-5-5", "agy", "low")]
    assert roles["plan_reviewer"]["preferred_seed"] == roles["code_reviewer"]["preferred_seed"]
    visual = [(e["model_id"], e["harness"], e["effort"]) for e in roles["visual_reviewer"]["preferred_seed"]]
    assert visual == [("gemini-3.8-flash", "agy", "low"), ("gpt-6.1-terra", "codex", "low"),
                      ("deepseek-v4.1-flash-free", "opencode", "max"), ("claude-sonnet-5-5", "agy", "medium")]


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


@pytest.mark.parametrize("model,effort", [("gpt-6-astra", "medium"), ("gpt-5.6-sol", "high"), ("gpt-5.6-terra", "low")])
def test_routing_rejects_a_shipped_executor_deny_at_every_size(model, effort):
    for size in (None, "XS", "M", "XL"):
        got = _route("executor", [_cand("codex", model, effort)], size)
        assert got["status"] == "no_qualifying_candidate", (model, effort, size)


def test_routing_applies_the_size_scope_of_a_shipped_deny():
    sonnet = _cand("claude", "claude-sonnet-5-5", "high")
    assert _route("executor", [sonnet], "S")["status"] == "no_qualifying_candidate"
    # Neutral at XL: whatever else rejects it (the XL benchmark floor on this bare candidate), it is not a role deny.
    got = _route("executor", [sonnet], "XL")
    assert not any("policy deny" in r.get("reason", "") for r in got.get("rejected", [])), got
    opus = _cand("claude", "claude-opus-5-5", "medium")
    assert _route("executor", [opus], "L")["status"] == "no_qualifying_candidate"


def test_routing_rejects_a_shipped_reviewer_deny():
    for model, effort in (("claude-sonnet-5-5", "high"), ("gpt-6-luna", "xhigh")):
        harness = "codex" if "luna" in model else "claude"
        got = _route("code_reviewer", [_cand(harness, model, effort)])
        assert got["status"] == "no_qualifying_candidate", (model, effort)


def test_candidate_building_excludes_shipped_denied_executor_routes(tmp_path, monkeypatch):
    from office import candidates, db, routing
    _isolated(tmp_path, monkeypatch)
    con = db.connect()
    try:
        built, skipped = candidates.build_candidates(con, "executor", probe=False, user_policies=[user()])
    finally:
        con.close()
    ids = {routing.candidate_id(c) for c in built}
    assert not [i for i in ids if "gpt-6-astra" in i or "gpt-5.6-sol" in i or "gpt-5.6-terra" in i], ids


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
