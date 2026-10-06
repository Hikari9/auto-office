"""Stable identities stay distinct where the records are distinct."""
from __future__ import annotations

from office import db
from office.web import identity, synthetic


def test_identity_formats():
    assert identity.host("h1") == "host:h1" and identity.host(None) is None
    assert identity.run("R") == "run:R"
    assert identity.task("R", "T1") == "task:R/T1"
    assert identity.dispatch("D1") == "dispatch:D1"
    assert identity.session("R", "claude", "S") == "session:R/claude/S"
    assert identity.runtime("3.3.3") == "runtime:3.3.3" and identity.runtime(None) is None
    assert identity.repo_key("Acme/Alpha") == "repo:github.com/acme/alpha"
    local = identity.repo_key(None, "/src/alpha/.git")
    assert local.startswith("repo-local:") and local == identity.local_repo_key("/src/alpha/.git")
    assert local != identity.local_repo_key("/src/beta/.git")
    assert identity.issue_ref("repo:github.com/acme/alpha", 5) == "issue:repo:github.com/acme/alpha#5"


def test_host_id_is_created_only_by_explicit_startup(tmp_path):
    home = tmp_path / "state"
    assert identity.read_host_id(home) is None
    assert not home.exists()
    created = identity.ensure_host_id(home)
    assert identity.read_host_id(home) == created
    assert identity.ensure_host_id(home) == created


def test_parse_issue_forms():
    key = "repo:github.com/acme/alpha"
    assert identity.parse_issue(12, key)["ref"] == f"issue:{key}#12"
    assert identity.parse_issue("#12", key)["number"] == 12
    url = identity.parse_issue("https://github.com/Other/Beta/issues/12", key)
    assert url["ref"] == "issue:repo:github.com/other/beta#12" and url["repo"] == "repo:github.com/other/beta"
    assert identity.parse_issue(None, key) is None and identity.parse_issue("", key) is None
    assert identity.parse_issue("not an issue", key)["ref"] is None


def test_same_issue_number_runs_and_prs_stay_distinct(writer, make_observer):
    path, con = writer
    with db.transaction(con):
        synthetic.insert_run(con, "A1", git_common_dir="/src/a/.git", landing={"issue": 3})
        synthetic.insert_run(con, "B1", git_common_dir="/src/b/.git", landing={"issue": 3})
        synthetic.insert_run(con, "A2", git_common_dir="/src/a/.git", landing={"issue": "#3"})
        for task, number in (("T1", 10), ("T2", 11)):
            synthetic.insert_task(con, "A1", task, status="accepted",
                                  pr={"number": number, "url": f"https://github.com/acme/a/pull/{number}"})
    slugs = {"/src/a/.git": "acme/a", "/src/b/.git": "acme/b"}
    for ctx in ({"repo_slugs": slugs}, {}):  # with and without known GitHub slugs
        runs = {r["run_id"]: r for r in make_observer(path, **ctx).workspace()["runs"]}
        a1, b1, a2 = runs["A1"], runs["B1"], runs["A2"]
        assert a1["issue"]["number"] == b1["issue"]["number"] == 3
        assert a1["issue"]["ref"] != b1["issue"]["ref"]  # two repos, same number
        assert a1["issue"]["ref"] == a2["issue"]["ref"]  # two runs, one issue
        assert a1["id"] != a2["id"]
        refs = [p["ref"] for p in a1["prs"]]
        assert len(refs) == 2 and len(set(refs)) == 2 and all(refs)
    assert a1["repo"]["key"].startswith("repo-local:")
