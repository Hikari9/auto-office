import json

from office.web import repos


def repo(name="o/a", perm="push", has_issues=True, archived=False, access="visible"):
    return {"full_name": name, "permission": perm, "has_issues": has_issues,
            "archived": archived, "access": access}


def fake_git(origins):
    """path-string -> origin url (None means a non-git dir)."""
    def git(path, args):
        url = origins.get(str(path), "missing")
        if url is None:
            return None
        if args[0] == "rev-parse":
            return ".git"
        return None if url == "missing" else url
    return git


def test_remote_parsing():
    for url in ("git@github.com:O/A.git", "https://github.com/o/a", "ssh://git@github.com/o/a.git",
                "https://github.com/o/a.git/"):
        assert repos.remote_full_name(url) == "o/a"
    assert repos.remote_full_name("https://gitlab.com/o/a") is None


def test_ready_from_run_root(tmp_path):
    d = tmp_path / "work" / "a"
    d.mkdir(parents=True)
    r = repos.assess(repo(), run_roots=[d], git=fake_git({str(d): "git@github.com:o/a.git"}))
    assert r["ready"] and r["attached"] and r["failing"] == []
    assert r["checkout"] == "…/work/a"
    assert str(tmp_path) not in json.dumps(r)


def test_configured_checkout_and_explicit_failures(tmp_path):
    other, mine = tmp_path / "other", tmp_path / "mine"
    other.mkdir(); mine.mkdir()
    git = fake_git({str(other): "git@github.com:o/zzz.git", str(mine): "https://github.com/o/a"})
    [r] = repos.assess_all([repo(perm="pull", has_issues=False, archived=True)], run_roots=[other],
                           checkout_paths={"O/A": [mine]}, git=git)
    assert r["attached"] and not r["ready"]
    assert r["failing"] == ["push_permission", "issues_enabled", "not_archived"]


def test_visible_only_never_ready(tmp_path):
    for perm in ("pull", "triage", None):
        r = repos.assess(repo(perm=perm), run_roots=[tmp_path], git=fake_git({str(tmp_path): "git@github.com:o/a"}))
        assert not r["ready"] and "push_permission" in r["failing"]
    r = repos.assess(repo(access="revoked"), run_roots=[tmp_path], git=fake_git({str(tmp_path): "git@github.com:o/a"}))
    assert not r["ready"]


def test_missing_checkout_not_git_and_wrong_origin(tmp_path):
    r = repos.assess(repo(), run_roots=[tmp_path / "nope"], git=fake_git({}))
    assert r["failing"][:3] == ["checkout_exists", "git_repository", "origin_matches"]
    r = repos.assess(repo(), git=fake_git({}))
    assert r["checkout"] is None and not r["ready"]
    r = repos.assess(repo(), run_roots=[tmp_path], git=fake_git({str(tmp_path): None}))
    assert r["failing"][:2] == ["git_repository", "origin_matches"]
    r = repos.assess(repo(), run_roots=[tmp_path], git=fake_git({str(tmp_path): "git@github.com:x/y"}))
    assert r["failing"] == ["origin_matches"] and not r["attached"]
    assert "x/y" in r["prerequisites"][2]["detail"]


def test_real_git(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:o/a.git"], check=True)
    assert repos.assess(repo(), run_roots=[tmp_path])["ready"]
