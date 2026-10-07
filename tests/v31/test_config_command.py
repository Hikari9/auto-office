"""`office config` (git-config style) and `office setup` (interactive)."""
from __future__ import annotations

import subprocess

import pytest
import yaml

from office import configcmd
from office.state import OfficeError


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return user, repo / ".auto-office" / "config.yaml"


def cfg(**kw):
    return configcmd.config(**{"key": None, "value": None, "tier": None, **kw})


def test_set_get_and_list_round_trip(home):
    user, _ = home
    cfg(key="roles.code_reviewer.preferred_seed", value="claude/sonnet@high,codex/luna@xhigh")
    assert yaml.safe_load(user.read_text())["roles"]["code_reviewer"]["preferred_seed"] == [
        {"model_id": "sonnet", "harness": "claude", "effort": "high"},
        {"model_id": "luna", "harness": "codex", "effort": "xhigh"}]
    assert cfg(key="roles.code_reviewer.preferred_seed").lines == ["claude/sonnet@high,codex/luna@xhigh"]
    assert cfg(list_=True, origin=True).lines == ["user\troles.code_reviewer.preferred_seed=claude/sonnet@high,codex/luna@xhigh"]


def test_only_what_was_set_is_written_and_unset_prunes(home):
    user, _ = home
    cfg(key="cost_policy.default", value="quota_saver")
    assert yaml.safe_load(user.read_text()) == {"cost_policy": {"default": "quota_saver"}}
    cfg(key="cost_policy.default", unset=True)
    assert yaml.safe_load(user.read_text()) == {}
    with pytest.raises(OfficeError) as err:
        cfg(key="cost_policy.default", unset=True)
    assert err.value.category == "not-set"


def test_get_reads_the_effective_value_and_its_origin(home):
    assert cfg(key="cost_policy.default", origin=True).lines == ["default\tbalanced"]
    cfg(key="cost_policy.default", value="quota_saver")
    cfg(key="cost_policy.default", value="money_saver", tier="repo")
    assert cfg(key="cost_policy.default", origin=True).lines == ["repo\tmoney_saver"]  # repo beats user
    assert cfg(key="cost_policy.default", tier="user").lines == ["quota_saver"]


@pytest.mark.parametrize("key,value,fragment", [
    ("cost_policy.defualt", "x", "did you mean default"),
    ("cost_policy.default", "bogus", "must be one of"),
    ("roles.code_reviewer.preferred_seed", "claude/sonet@high", "did you mean sonnet"),
    ("roles.code_reviewer.preferred_seed", "claude/sonnet@turbo", "no turbo effort"),
    ("roles.code_reviewer.preferred_seed", "codex/sonnet", "runs on claude"),
    ("routing.adaptive.weights.balanced.preference", "0.9", "must be <= 0.25"),
    ("schema_version", "9", "not configurable"),
    ("nonsense", "1", "unknown config key"),
])
def test_bad_values_are_refused_and_nothing_is_written(home, key, value, fragment):
    user, _ = home
    with pytest.raises(OfficeError) as err:
        cfg(key=key, value=value)
    assert fragment in err.value.message and not user.exists()


def test_force_sets_a_key_the_defaults_do_not_define(home):
    cfg(key="cost_policy.experimental", value="1", force=True)
    assert cfg(key="cost_policy.experimental").lines == ["1"]


def test_any_role_may_carry_a_preference(home):
    cfg(key="roles.executor.preferred_seed", value="codex/gpt-6-luna@high")
    assert cfg(key="roles.executor.preferred_seed").lines == ["codex/gpt-6-luna@high"]


def test_a_repo_may_not_move_the_runs_database(home):
    with pytest.raises(OfficeError) as err:
        cfg(key="paths.runs_db", value="/x", tier="repo")
    assert "machine-level" in err.value.message


def test_comments_are_backed_up_before_a_rewrite(home):
    user, _ = home
    user.write_text("# mine\ncost_policy:\n  default: quota_saver\n")
    res = cfg(key="cost_policy.default", value="balanced")
    assert (user.parent / "user.yaml.bak").read_text().startswith("# mine")
    assert any("not kept" in n for n in res.notices)


def test_malformed_file_is_reported_not_overwritten(home):
    user, _ = home
    user.write_text("roles: [unclosed")
    with pytest.raises(OfficeError) as err:
        cfg(key="cost_policy.default", value="balanced")
    assert err.value.category == "config-unreadable" and user.read_text() == "roles: [unclosed"


def test_repo_file_lives_in_the_repository(home):
    _, repo_file = home
    cfg(key="cost_policy.default", value="money_saver", tier="repo")
    assert repo_file.is_file()
    assert cfg(path=True, tier="repo").lines == [str(repo_file.resolve())]


def answers(*lines):
    it = iter(lines)
    return lambda prompt: next(it)


def run_setup(home, *lines, **kw):
    quiet = []
    res = configcmd.setup(tier=kw.pop("tier", "user"), input_fn=answers(*lines), out=quiet.append, interactive=True, **kw)
    return res, quiet


def test_setup_writes_the_answers(home):
    user, _ = home
    # planner, plan_reviewer, executor, code_reviewer, visual_reviewer, cost policy, confirm
    res, _ = run_setup(home, "claude/opus@high", "", "", "claude/sonnet@high,codex/luna@xhigh", "", "quota_saver", "y")
    data = yaml.safe_load(user.read_text())
    assert data["cost_policy"] == {"default": "quota_saver"}
    assert [e["model_id"] for e in data["roles"]["code_reviewer"]["preferred_seed"]] == ["sonnet", "luna"]
    assert data["roles"]["planner"]["preferred_seed"] == [{"model_id": "opus", "harness": "claude", "effort": "high"}]
    assert set(res.data["changed"]) == {"roles.planner.preferred_seed", "roles.code_reviewer.preferred_seed",
                                        "cost_policy.default"}


def test_setup_reprompts_on_a_bad_route_and_lists_routes_on_question_mark(home):
    user, _ = home
    _, out = run_setup(home, "?", "claude/sonet@high", "claude/sonnet@high", "", "", "", "", "", "y")
    text = "\n".join(out)
    assert "claude/claude-sonnet-5-5@" in text and "did you mean sonnet" in text
    assert yaml.safe_load(user.read_text())["roles"]["planner"]["preferred_seed"][0]["model_id"] == "sonnet"


def test_setup_dash_resets_a_role_to_the_default(home):
    user, _ = home
    cfg(key="roles.planner.preferred_seed", value="claude/sonnet@high")
    run_setup(home, "-", "", "", "", "", "", "y")
    assert yaml.safe_load(user.read_text()) == {}


def test_setup_declined_or_unchanged_writes_nothing(home):
    user, _ = home
    assert run_setup(home, "", "", "", "", "", "")[0].lines == ["no changes"]
    assert run_setup(home, "claude/sonnet@high", "", "", "", "", "", "n")[0].lines == ["nothing written"]
    assert not user.exists()


def test_setup_needs_a_terminal(home):
    with pytest.raises(OfficeError) as err:
        configcmd.setup(tier="user", interactive=False)
    assert err.value.exit_code == 2


def test_setup_eof_cancels_without_writing(home):
    user, _ = home

    def eof(prompt):
        raise EOFError

    with pytest.raises(OfficeError) as err:
        configcmd.setup(tier="user", input_fn=eof, out=lambda s: None, interactive=True)
    assert err.value.category == "cancelled" and not user.exists()
