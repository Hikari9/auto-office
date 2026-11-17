"""#1116: `office config quota.reserve_percent` prints each role family's effective protected reserve
and the layer it came from, for the live config and, with `--run`, for what a run pinned."""
import yaml

from office import configcmd, config as cfg, paths, state
from conftest import start_inline


def _write(tier_path, quota):
    tier_path.parent.mkdir(parents=True, exist_ok=True)
    tier_path.write_text(yaml.safe_dump({"quota": quota}))


def test_the_shipped_default_names_each_family_and_its_roles(env):
    res = configcmd.config(key="quota.reserve_percent", value=None, tier=None)
    text = "\n".join(res.lines)
    assert "executor: 20%  (shipped default; roles executor, planner, worker)" in text
    assert "reviewer: 5%  (shipped default; roles code_reviewer, plan_reviewer)" in text
    assert res.data["origins"] == {"executor": "shipped default", "reviewer": "shipped default"}


def test_a_user_scalar_sets_both_families_and_a_user_by_role_value_wins_for_its_own(env):
    _write(paths.user_config_path(), {"reserve_percent": 9, "reserve_percent_by_role": {"executor": 30}})
    res = configcmd.config(key="quota.reserve_percent", value=None, tier=None)
    assert res.data["by_role"] == {"executor": 30.0, "reviewer": 9.0}
    assert res.data["origins"] == {"executor": "user quota.reserve_percent_by_role.executor",
                                   "reviewer": "user quota.reserve_percent"}
    assert any(line.startswith("executor: 30%  (user quota.reserve_percent_by_role.executor") for line in res.lines)


def test_the_repo_layer_outranks_the_user_layer_for_the_same_key():
    files = {"user": yaml.safe_dump({"quota": {"reserve_percent": 9}}),
             "repo": yaml.safe_dump({"quota": {"reserve_percent": 2}})}
    assert cfg.reserve_origins(files) == {"executor": "repo quota.reserve_percent", "reviewer": "repo quota.reserve_percent"}


def test_a_run_shows_what_it_pinned_and_notes_a_later_live_change(env):
    start_inline(env)
    run = state.get_run(env.con(), env.con().execute("SELECT id FROM runs").fetchone()[0])
    _write(paths.user_config_path(), {"reserve_percent_by_role": {"executor": 1}})
    res = configcmd.run_get(env.con(), run["id"][:8], "quota.reserve_percent", False, paths.user_config_path().parent)
    assert res.data["by_role"]["executor"] == 20.0 and f"run {run['id'][:8]} pinned" in res.lines[0]
    assert any("executor 20% -> 1% (user quota.reserve_percent_by_role.executor)" in n for n in res.notices), res.notices
    assert res.data["live"]["by_role"]["executor"] == 1.0
