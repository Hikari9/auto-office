"""Office commits carry the operator's git identity (favorchurch/rock-mcp PR #220).

A commit authored as `Auto Office <office@localhost>` has no GitHub account, and
Vercel blocks the preview deployment for it ("GitHub couldn't verify an account
for the commit"). Provenance moves to an `Office-Run:` trailer.
"""
from __future__ import annotations

import pytest

from conftest import GOOD_ADD, start_inline


@pytest.fixture(autouse=True)
def _no_global_git_identity(monkeypatch):
    # The operator's real ~/.gitconfig must not decide these tests.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _go(env, **script):
    env.trust()
    env.script(**script)
    start_inline(env, gear="direct+review")
    env.office("approve", "plan", "--quote", "approved", check=0)


def test_identity_comes_from_the_repo_config_then_env_then_fallback(env, monkeypatch):
    from office import paths
    assert paths.commit_identity_env(env.repo)["GIT_AUTHOR_EMAIL"] == "office@localhost"
    env.git("config", "user.name", "Rico T")
    env.git("config", "user.email", "rico@example.org")
    got = paths.commit_identity_env(env.repo)
    assert got == {"GIT_AUTHOR_NAME": "Rico T", "GIT_AUTHOR_EMAIL": "rico@example.org",
                   "GIT_COMMITTER_NAME": "Rico T", "GIT_COMMITTER_EMAIL": "rico@example.org"}
    monkeypatch.setenv("OFFICE_GIT_NAME", "Ops")
    monkeypatch.setenv("OFFICE_GIT_EMAIL", "ops@example.org")
    assert paths.commit_identity_env(env.repo)["GIT_COMMITTER_EMAIL"] == "ops@example.org"


def test_submission_commit_uses_the_operator_identity(env):
    env.git("config", "user.name", "Rico T")
    env.git("config", "user.email", "rico@example.org")
    _go(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    sha = con.execute("SELECT commit_sha FROM revisions").fetchone()[0]
    fmt = env.git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%B", sha)
    assert fmt.startswith("Rico T <rico@example.org>|Rico T <rico@example.org>|"), fmt
    assert f"Office-Run: {run_id}" in fmt
    assert "office@localhost" not in env.git("log", "--all", "--format=%ae %ce")
