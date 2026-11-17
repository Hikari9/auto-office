"""#1120: `office route clear` reaches a run that pinned the rule at start.

Routing reads the user and repo policy live; the run's pinned `routing.user_policy` is never merged in
beside it. A rule cleared after `office start` therefore stops applying at the run's next decision,
whether the rule was an allow or a deny. Unit tier: the World fixture's isolated home, a real runs.db."""
import copy

from test_route_discovery_consumer_contract import World, world  # noqa: F401 (world is a fixture)

from office import candidates, config as cfg, route_policy, route_probe, routecmd

SOL_HIGH = "codex/gpt-6.1-sol@high"  # a shipped executor deny: only a user allow makes it dispatchable
SOL_MEDIUM = "codex/gpt-6.1-sol@medium"  # neutral: only a user deny blocks it


def _pin_with(world_, action, route=SOL_HIGH):
    """A run pinned while the user's `action` rule for `route` was set, then the user clears it."""
    routecmd.change(action, role="executor", route=route)
    pinned = copy.deepcopy(world_.config)
    live = cfg.resolve(world_.tmp / "repo")[0]
    pinned["routing"]["user_policy"] = copy.deepcopy(live["routing"]["user_policy"])
    assert pinned["routing"]["user_policy"]["role_rules"], "the pin carries the rule"
    routecmd.change("clear", role="executor", route=route)
    assert cfg.resolve(world_.tmp / "repo")[0]["routing"]["user_policy"]["role_rules"] == []
    return pinned


def _cand(spec):
    return route_probe.candidate_from_spec(spec)


def _role_denied(decision):
    return {r["candidate"] for r in decision["rejected"] if r.get("category") == "role-deny"}


def test_a_cleared_allow_no_longer_makes_a_route_dispatchable_for_a_pinned_run(world):
    routecmd.change("allow", role="executor", route=SOL_HIGH)
    assert world.decide(exact=world.sol["high"])["selected"] == world.sol["high"]  # the standing allow selects it
    pinned = _pin_with(world, "allow")
    decision = world.decide(exact=world.sol["high"], config=pinned)
    assert decision["selected"] != world.sol["high"], decision


def test_a_cleared_deny_no_longer_blocks_a_route_for_a_pinned_run(world):
    pinned = _pin_with(world, "deny", SOL_MEDIUM)
    assert world.sol["medium"] not in _role_denied(world.decide(exact=world.sol["medium"], config=pinned))


def test_a_standing_deny_still_blocks_a_route_for_a_pinned_run(world):
    routecmd.change("deny", role="executor", route=SOL_MEDIUM)
    assert world.sol["medium"] in _role_denied(world.decide(exact=world.sol["medium"], config=world.config))


def test_a_cleared_deny_does_not_refuse_the_probe_of_a_pinned_run(world):
    pinned = _pin_with(world, "deny", SOL_MEDIUM)
    assert route_probe._denial(_cand(SOL_MEDIUM), world.run, pinned) is None


def test_a_standing_deny_still_refuses_the_probe_of_a_pinned_run(world):
    routecmd.change("deny", role="executor", route=SOL_MEDIUM)
    assert route_probe._denial(_cand(SOL_MEDIUM), world.run, world.config)[0] == "route-denied"
