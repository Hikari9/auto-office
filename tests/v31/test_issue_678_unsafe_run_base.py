"""#678: a feature-only starting commit must never silently enter a task PR."""
from office import lifecycle
from office.state import Usage


def remote_main(env):
    base = env.git("rev-parse", "HEAD").strip()
    env.git("update-ref", "refs/remotes/origin/main", base)
    env.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return base


def feature_commit(env, base):
    env.git("switch", "-qc", "feature-foreign", base)
    (env.repo / "foreign.py").write_text("UNRELATED_FEATURE = True\n")
    env.git("add", "foreign.py")
    env.git("commit", "-qm", "foreign unmerged feature")
    return env.git("rev-parse", "HEAD").strip()


def test_feature_head_is_refused_before_a_run_is_created(env):
    base = remote_main(env)
    feature = feature_commit(env, base)
    code, out = env.office("start", "docs-only change", "--planner", "inline")
    assert code != 0 and "unsafe-run-base" in out, out
    assert "unrelated feature commits" in out
    assert "origin/main" in out
    assert not env.con().execute("SELECT id FROM runs").fetchone()
    assert env.git("rev-parse", "HEAD").strip() == feature


def test_explicit_tracked_default_base_succeeds_from_feature_checkout(env):
    base = remote_main(env)
    feature_commit(env, base)
    code, out = env.office("start", "docs-only change", "--planner", "inline",
                           "--base", "origin/main")
    assert code == 0, out
    got = env.con().execute("SELECT base_sha FROM runs").fetchone()[0]
    assert got == base, (got, base)


def test_explicit_feature_base_is_also_refused(env):
    base = remote_main(env)
    feature = feature_commit(env, base)
    code, out = env.office("start", "docs-only change", "--base", feature,
                           "--planner", "inline")
    assert code != 0 and "unsafe-run-base" in out, out


def test_offline_repo_without_remote_remains_valid(env):
    code, out = env.office("start", "local-only goal", "--planner", "inline")
    assert code == 0, out


def test_origin_main_without_origin_head_still_protects_run(env):
    base = remote_main(env)
    env.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    feature_commit(env, base)
    code, out = env.office("start", "docs-only change", "--planner", "inline")
    assert code != 0 and "unsafe-run-base" in out, out


def test_commits_already_contained_in_remote_main_are_allowed(env):
    base = remote_main(env)
    feature = feature_commit(env, base)
    env.git("update-ref", "refs/remotes/origin/main", feature)
    code, out = env.office("start", "on merged main", "--planner", "inline")
    assert code == 0, out
    assert env.con().execute("SELECT base_sha FROM runs").fetchone()[0] == feature
