"""#1083: qualified route identities must give policy inspection and routing one verdict."""
import subprocess

import pytest
import yaml

from office import adapters, candidates, config, route_policy, routecmd, routing, state


VIEW_ROUTE = "claude/sonnet@high"
MODEL = "sonnet"
INVOCATION_MODEL = "claude-sonnet-5-5"
INSTALLED_CLAUDE = "2.1.311"


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
    monkeypatch.setattr(adapters, "installed", lambda _adapter: True)
    monkeypatch.setattr(adapters, "profile", lambda _adapter, _kind: {"command": ["fake"]})
    return target, repo


def write_rule(target, route, action):
    target.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": route, "state": action},
    ]}}}))


def view_row():
    rows = routecmd.policy(role="executor", effective=True).data["effective"]
    return next(row for row in rows if row["route"] == VIEW_ROUTE)


def routing_candidate():
    # The policy view and named-route resolver know this alias target, while a routed
    # candidate may carry only the canonical invocation id. Both identify the same route.
    return {
        "harness": "claude", "harness_version": INSTALLED_CLAUDE, "model_id": MODEL,
        "invocation_model_id": INVOCATION_MODEL, "effort": "high", "capabilities": ["review"],
        "quota": {"status": "ok", "tightest_remaining_percent": 80},
        "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1},
        "route_status": "available",
    }


def route_decision(policy):
    return routing.route({"role": "executor", "candidates": [routing_candidate()],
                          "policy": {"floor": None, "required_capabilities": []},
                          "user_policies": [policy], "runs_db": None})


@pytest.mark.parametrize("route", [
    "claude@2/claude-sonnet-5-5@high",
    "claude/claude-sonnet-5-5@high",
])
def test_qualified_and_unqualified_denies_match_view_dispatch_and_reroute_eligibility(isolated, route):
    target, repo = isolated
    write_rule(target, route, "deny")
    view = view_row()
    assert view["state"] == "deny" and not view["dispatches"], view

    live, _ = config.resolve(repo)
    for spelling in ("claude@2/claude-sonnet-5-5@high", "claude/claude-sonnet-5-5@high"):
        with pytest.raises(state.Refused) as refusal:
            candidates.declared_decision(spelling, flag="--as", role="executor",
                                         config=live, repo_root=str(repo))
        assert refusal.value.category == "route-denied", spelling

    policy = route_policy.user_policy(live)
    # `declared_decision` admits named dispatch/amend-route choices; rerun requalification
    # routes recorded candidates through the same `routing.route` policy check.
    decision = route_decision(policy)
    assert decision["status"] == "no_qualifying_candidate", decision


@pytest.mark.parametrize("route", [
    "claude@2/claude-sonnet-5-5@high",
    "claude/claude-sonnet-5-5@high",
])
def test_qualified_and_unqualified_allows_match_view_and_dispatch_eligibility(isolated, route):
    target, repo = isolated
    write_rule(target, route, "allow")
    view = view_row()
    assert view["state"] == "allow" and view["dispatches"], view

    live, _ = config.resolve(repo)
    policy = route_policy.user_policy(live)
    assert route_policy.user_allows(routing_candidate(), "executor", [policy]) is not None
    for spelling in ("claude@2/claude-sonnet-5-5@high", "claude/claude-sonnet-5-5@high"):
        assert candidates.declared_decision(spelling, flag="--as", role="executor",
                                            config=live, repo_root=str(repo))["status"] == "selected"
    decision = route_decision(policy)
    assert decision["status"] == "selected", decision
