"""Role policy CLI edits remain user-owned, version/alias-aware and inspectable."""
import subprocess
import pytest
import yaml

from office import config, routecmd, route_policy
from office.state import OfficeError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    target = tmp_path / "my-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(target))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return target, repo


def test_allow_deny_neutral_clear_roundtrip_preserves_other_roles(isolated):
    target, _ = isolated
    assert routecmd.change("deny", role="executor", route="claude/opus@low").data["changed"]
    assert routecmd.change("allow", role="code_reviewer", route="claude/opus@low").data["changed"]
    policies = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"]
    assert [p["state"] for p in policies] == ["deny", "allow"]
    assert all(p["route"].endswith("/claude-opus-5-5@low") for p in policies)
    assert routecmd.change("neutral", role="executor", route="claude/opus@low").data["changed"]
    assert routecmd.change("clear", role="executor", route="claude/opus@low").data["changed"]
    policies = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"]
    assert len(policies) == 1 and policies[0]["role"] == "code_reviewer"


def test_cli_never_writes_invalid_unknown_major(isolated):
    target, _ = isolated
    routecmd.change("deny", role="code_reviewer", route="claude/opus@low")
    entry = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"][0]
    assert "@unknown" not in entry["route"]
    assert route_policy._valid_spec(entry["route"]) or route_policy._canonical_spec(entry["route"])


def test_bad_edit_not_written_and_wildcard_requires_flag(isolated):
    target, _ = isolated
    with pytest.raises(OfficeError):
        routecmd.change("deny", role="code_reviewer", route="claude/opus")
    with pytest.raises(OfficeError):
        routecmd.change("deny", role="root", route="claude/opus@low")
    assert not target.exists()
    routecmd.change("deny", role="code_reviewer", route="claude/opus", wildcard=True)
    entry = yaml.safe_load(target.read_text())["routing"]["user_policy"]["role_rules"][0]
    assert entry["wildcard"] is True


def test_effective_view_shows_provenance_and_technical_status(isolated):
    result = routecmd.policy(role="code_reviewer")
    assert result.data["effective"]
    assert all(set(r) == {"role", "route", "state", "source", "technical_dispatchable", "dispatches", "dispatch_reason"}
               for r in result.data["effective"])
    assert any("does not dispatch" in l for l in result.lines)  # #845: said plainly, with the way to make it dispatch


def test_raw_config_supports_role_rules_at_user_tier(isolated):
    target, _ = isolated
    routecmd.change("deny", role="code_reviewer", route="claude/opus@low")
    cfg, _ = config.resolve(None)
    assert route_policy.user_policy(cfg)["role_rules"][0]["state"] == "deny"
