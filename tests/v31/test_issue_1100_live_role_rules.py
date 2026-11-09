"""#1100: a user role-rule written after a run pinned its policy reaches that run's next routing decision.

Unit tier. The `world` fixture's isolated Office home, scripted fake `codex` and real runs.db, with a fake
`claude` (pinned to major 2) so the shipped claude-sonnet executor deny is live. The run's pinned config has
no role rules: it predates the user rule written below. No model is called.
"""
import yaml

from test_route_discovery_consumer_contract import World, world  # noqa: F401 (world is a fixture)

from office import adapters, routing

ROUTE = "claude@2/claude-sonnet-5-5@high"
CLAUDE_VERSION = "2.1.311"


def _write_role_rule(world_, state):
    world_.user_config.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": ROUTE, "state": state}]}}}))


def _role_denied(decision):
    return [e for e in decision["rejected"] if e.get("category") == "role-deny" and e["candidate"] == ROUTE]


def _claude_fake_at_size_m(world_, monkeypatch):
    """A task of size M on a run whose pinned policy predates the user rule (`world.config` has no role rules)."""
    monkeypatch.setattr(adapters, "installed", lambda adapter: adapters.executable(adapter) in ("codex", "claude"))
    real = adapters.route_version
    monkeypatch.setattr(adapters, "route_version",
                        lambda harness, adapter, sources=None: CLAUDE_VERSION if harness == "claude"
                        else real(harness, adapter, sources))
    run = world_.make_run(size_class="M")
    run["gear"] = "M"  # the run gear names the size slate, so the executor's task size M is what the policy reads
    return run


def test_a_user_allow_written_after_the_pin_reaches_the_run_at_its_next_routing_decision(world, monkeypatch):
    run = _claude_fake_at_size_m(world, monkeypatch)
    assert _role_denied(world.decide(run=run)), "the shipped size-M deny must apply before the user rule exists"
    _write_role_rule(world, "allow")
    decision = world.decide(run=run)
    assert not _role_denied(decision)
    assert decision["selected"] == ROUTE
    assert any(routing.candidate_id(c) == ROUTE for c in decision["request"]["candidates"])


def test_a_user_deny_written_after_the_pin_rejects_the_route_at_the_next_routing_decision(world, monkeypatch):
    run = _claude_fake_at_size_m(world, monkeypatch)
    _write_role_rule(world, "deny")
    decision = world.decide(run=run)
    [entry] = _role_denied(decision)
    assert entry["policy"]["source"] == "user" and entry["policy"]["rule"] == ROUTE
    assert decision["selected"] != ROUTE
