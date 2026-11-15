"""H3 (#1168): a user-allowed, catalog-disabled code reviewer route may be ordered in the seed."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import configcmd, routecmd  # noqa: E402

H = {"model_id": "gemini-4-hydrogen", "harness": "agy", "effort": "high"}


def _cfg(*rules):
    return {"routing": {"user_policy": {"role_rules": list(rules)}}}


def _rule(route="agy/gemini-4-hydrogen@high", role="code_reviewer"):
    return {"role": role, "route": route, "state": "allow"}


def test_exact_user_allow_lets_the_seed_order_a_catalog_disabled_reviewer():
    assert configcmd.validate_seed([H], role="code_reviewer", config=_cfg(_rule())) == []


@pytest.mark.parametrize("name,config,entry,role", [
    ("no-allow", _cfg(), H, "code_reviewer"),
    ("other-role-allow", _cfg(_rule(role="executor")), H, "code_reviewer"),
    ("wildcard-allow", _cfg(_rule(route="agy/gemini-4-hydrogen")), H, "code_reviewer"),
    ("other-effort-allow", _cfg(_rule(route="agy/gemini-4-hydrogen@medium")), H, "code_reviewer"),
    ("no-effort-entry", _cfg(_rule()), {k: v for k, v in H.items() if k != "effort"}, "code_reviewer"),
    ("no-role", _cfg(_rule()), H, None),
])
def test_the_seed_still_refuses_without_an_exact_allow(name, config, entry, role):
    problems = configcmd.validate_seed([entry], role=role, config=config)
    assert any("not dispatchable" in p for p in problems), (name, problems)


def test_route_policy_shows_the_allowed_reviewer_as_a_voting_code_reviewer():
    row = {"model_id": "gemini-4-hydrogen", "invocation_harness": "agy", "effort": "high", "dispatchable": False}
    state = {"state": "allow", "source": "user", "rule": "agy/gemini-4-hydrogen@high"}
    ok, why = routecmd._dispatch_view(row, state, candidate=H, role="code_reviewer", probe={"result": "pass"})
    assert ok is True and why.startswith("user allow; exact probe passed; a voting code reviewer") and "dispatches" not in why.split(";")[0]
    assert "voting code reviewer" in why and "canary trial" not in why.split(";")[0]
    ok, why = routecmd._dispatch_view(row, state, candidate=H, role="code_reviewer", probe=None)
    assert ok is False and why.startswith("user allow as a code reviewer")
