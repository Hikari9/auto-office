"""#899: `office route policy --effective` and `office dispatch --as` give one allow/deny answer for every route.

Unit tier. An isolated user config and a temp repository, the real catalog and candidate code, and a pinned
installed Claude major. No model is called.
"""
import subprocess

import pytest
import yaml

from office import adapters, candidates, config, model_lineages, route_policy, routecmd, scoring, state

INSTALLED_CLAUDE = "2.1.311"

SEED_SONNET = [{"harness": "claude", "model_id": "sonnet", "effort": "high"}]

# Every scenario here is one the view and `--as` must answer the same way.
SCENARIOS = {
    "shipped-only": {},
    "exact-deny": {"role_rules": [{"role": "executor", "route": "claude/claude-opus-5-5@low", "state": "deny"}]},
    "alias-deny": {"role_rules": [{"role": "executor", "route": "claude/sonnet@high", "state": "deny"}]},
    "instance-deny": {"role_rules": [{"role": "executor", "route": "claude@2/sonnet@high", "state": "deny"}]},
    "instance-allow": {"role_rules": [{"role": "executor", "route": "claude@2/sonnet@high", "state": "allow"}]},
    "harness-deny": {"role_rules": [{"role": "executor", "route": "harness:claude", "state": "deny", "wildcard": True}]},
    "legacy-deny": {"denied": ["claude/sonnet@high"]},
    "seed-under-legacy-deny": {"denied": ["claude/sonnet@high"], "seed": SEED_SONNET},
    "seed-under-role-deny": {"role_rules": [{"role": "executor", "route": "claude/sonnet@high", "state": "deny"}],
                             "seed": SEED_SONNET},
    "allow-canonical-over-alias-deny": {"role_rules": [
        {"role": "executor", "route": "claude/sonnet@high", "state": "deny"},
        {"role": "executor", "route": "claude/claude-sonnet-5-5@high", "state": "allow"}]},
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    target = tmp_path / "my-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(target))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    real = adapters.route_version

    def route_version(harness, adapter, sources=None):
        return INSTALLED_CLAUDE if harness == "claude" else real(harness, adapter, sources)

    monkeypatch.setattr(adapters, "route_version", route_version)
    return target, repo


def write_policy(target, role_rules=(), denied=(), seed=None):
    doc = {"routing": {"user_policy": {"role_rules": list(role_rules), "denied_models": list(denied)}}}
    if seed is not None:
        doc["roles"] = {"executor": {"preferred_seed": seed}}
    target.write_text(yaml.safe_dump(doc))


def view_routes():
    return routecmd.policy(role="executor", effective=True).data["effective"]


def view_state(route):
    [row] = [r for r in view_routes() if r["route"] == route]
    return row


def dispatch_refusal(text, repo):
    """The refusal `office dispatch --as <text>` gives the executor, or None when it is accepted.

    This is the declared-route gate `named_executor_route` runs for an orchestrator choice, with the
    live config the view reads."""
    live, _ = config.resolve(repo)
    try:
        candidates.declared_decision(text, flag="--as", role="executor", config=live, repo_root=str(repo))
    except state.OfficeError as exc:
        return exc
    return None


def spellings(route):
    """The spellings of one view route that `--as` accepts: the catalog's own, and the pinned harness@major form."""
    harness, rest = route.split("/", 1)
    major = scoring.harness_major(adapters.route_version(harness, adapters.load_all()[harness]))
    return [route] + ([f"{harness}@{major}/{rest}"] if major.isdigit() else [])


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_route_and_spelling_gets_the_same_answer_from_the_view_and_dispatch(isolated, name):
    target, repo = isolated
    write_policy(target, **SCENARIOS[name])
    routes = view_routes()
    claude_routes = [r for r in routes if r["route"].startswith("claude/")]
    assert routes and claude_routes
    assert all(len(spellings(r["route"])) == 2 for r in claude_routes)  # every claude route has its pinned spelling
    disagreements = []
    for row in routes:
        for text in spellings(row["route"]):
            refusal = dispatch_refusal(text, repo)
            category = refusal.category if refusal else "accept"
            if row["state"] == "allow":
                expected = category == "accept"
            elif row["state"] == "deny":
                expected = category == "route-denied"
            else:
                expected = category != "route-denied"
            if not expected:
                disagreements.append((text, row["state"], row["source"], category))
    assert disagreements == []


def test_a_preferred_seed_cannot_override_a_legacy_deny_in_dispatch(isolated):
    """Production routing refuses a seeded route on denied_models; `--as` must refuse it too."""
    target, repo = isolated
    write_policy(target, denied=["claude/sonnet@high"], seed=SEED_SONNET)
    refusal = dispatch_refusal("claude/sonnet@high", repo)
    assert refusal is not None and refusal.category == "route-denied"
    assert "office route policy --effective --role executor" in refusal.next_step


def test_the_effective_view_shows_a_seeded_legacy_deny_as_denied(isolated):
    target, _ = isolated
    write_policy(target, denied=["claude/sonnet@high"], seed=SEED_SONNET)
    assert view_state("claude/sonnet@high")["state"] == "deny"


@pytest.mark.parametrize("pin", ["claude@3", "claude@", "claude@unknown", "claude@2@3"])
def test_a_pin_that_is_not_the_installed_major_is_refused_with_a_command_that_shows_the_verdict(isolated, pin):
    _, repo = isolated
    refusal = dispatch_refusal(f"{pin}/sonnet@high", repo)
    assert refusal is not None and refusal.category == "harness-major-mismatch"
    assert "the installed claude major is 2" in refusal.message
    assert "office route policy --effective --role executor" in refusal.next_step


@pytest.mark.parametrize("pin", ["claude@2", "claude@02"])
def test_a_pin_names_the_installed_major(isolated, pin):
    _, repo = isolated
    assert dispatch_refusal(f"{pin}/sonnet@high", repo) is None


@pytest.mark.parametrize("pin", ["claude@2", "claude@unknown"])
def test_an_unknown_installed_major_cannot_satisfy_a_pin(isolated, monkeypatch, pin):
    _, repo = isolated
    monkeypatch.setattr(adapters, "route_version", lambda harness, adapter, sources=None: "unknown")
    refusal = dispatch_refusal(f"{pin}/sonnet@high", repo)
    assert refusal is not None and refusal.category == "harness-major-mismatch"
    assert "the installed claude major is unknown" in refusal.message


def test_a_denied_route_refusal_names_the_command_that_shows_the_verdict(isolated):
    target, repo = isolated
    write_policy(target, **SCENARIOS["alias-deny"])
    refusal = dispatch_refusal("claude/sonnet@high", repo)
    assert refusal is not None and refusal.category == "route-denied"
    assert "office route policy --effective --role executor" in refusal.next_step


def test_dispatch_applies_the_role_capability_gate_from_the_live_config(isolated):
    """The sweep above passes the live config, so a role's required capability is checked the way dispatch checks it."""
    target, repo = isolated
    target.write_text(yaml.safe_dump({"roles": {"executor": {"required_capabilities": ["teleport"]}}}))
    refusal = dispatch_refusal("claude/sonnet@high", repo)
    assert refusal is not None and refusal.category == "capability-missing"


def test_a_pinned_instance_allow_is_accepted_and_shown_as_allowed(isolated):
    target, repo = isolated
    write_policy(target, **SCENARIOS["instance-allow"])
    assert view_state("claude/sonnet@high")["state"] == "allow"
    assert dispatch_refusal("claude@2/sonnet@high", repo) is None


def test_a_preferred_seed_cannot_lift_a_version_ceiling_in_the_effective_view():
    """A version ceiling denies a route dispatch refuses, so the seeded view must deny it too (#899)."""
    cand = {"harness": "claude", "harness_version": INSTALLED_CLAUDE, "model_id": "sonnet",
            "invocation_model_id": "claude-sonnet-5-5", "alias_resolved_to": "claude-sonnet-5-5", "effort": "high"}
    identity = model_lineages.identify("sonnet", aliases=cand)
    ceiling = {"denied": [], "role_rules": [], "sources": {"version_ceilings": "user"},
               "ceilings": {identity["lineage_key"]: identity["generation_version"]}}
    seed = route_policy.preferred_seed_policy("executor", SEED_SONNET)
    assert route_policy.effective_role_state(cand, "executor", [seed, ceiling])["state"] == "deny"
