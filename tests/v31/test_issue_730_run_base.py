"""A local run can deliberately start on an existing PR branch."""

from test_issue_678_unsafe_run_base import feature_commit, remote_main


def test_no_prs_pins_feature_head(env):
    feature = feature_commit(env, remote_main(env))
    code, out = env.office("start", "local goal", "--no-prs", "--planner", "inline")
    assert code == 0, out
    assert env.con().execute("SELECT base_sha FROM runs").fetchone()[0] == feature


def test_no_prs_explicit_pr_branch_pins_that_commit(env):
    base = remote_main(env)
    feature = feature_commit(env, base)
    env.git("switch", "-q", "--detach", base)
    code, out = env.office("start", "stacked goal", "--no-prs", "--base", "feature-foreign",
                           "--planner", "inline")
    assert code == 0, out
    assert env.con().execute("SELECT base_sha FROM runs").fetchone()[0] == feature


def test_start_help_lists_base(env):
    code, out = env.office("start", "--help")
    assert code == 0, out
    assert "--base" in out
