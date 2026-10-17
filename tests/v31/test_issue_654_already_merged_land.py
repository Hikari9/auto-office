"""#654: landing adopts PRs already merged outside Office without restack loops."""
import json

from test_land import _integration_tree, _main_tree, _plan, _run
from test_task_prs import gh
from fake_gh import main as gh_main


def test_external_parent_merge_is_adopted_without_merging_twice(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    first = gh(env)["prs"][0]
    with monkeypatch.context() as ctx:
        ctx.chdir(env.repo)
        assert gh_main(["pr", "merge", str(first["number"]), "--merge"]) == 0
    assert gh(env)["prs"][0]["state"] == "merged"
    code, out = env.office("land", "--merge", "--quote", "merge accepted tasks")
    assert code == 0 and "already merged" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["merged", "merged"]
    assert _main_tree(env, bare) == _integration_tree(env)
    saved = json.loads(env.con().execute("SELECT pr_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert saved["merged"] is True and saved["merged_externally"] is True


def test_external_squash_merged_parent_is_adopted(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    first = gh(env)["prs"][0]
    with monkeypatch.context() as ctx:
        ctx.chdir(env.repo)
        assert gh_main(["pr", "merge", str(first["number"]), "--squash"]) == 0
    assert gh(env)["prs"][0]["state"] == "merged"
    code, out = env.office("land", "--merge", "--quote", "merge accepted tasks")
    assert code == 0 and "already merged externally; adopted" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["merged", "merged"]
    assert _main_tree(env, bare) == _integration_tree(env)
    saved = json.loads(env.con().execute("SELECT pr_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert saved["merged"] is True and saved["merged_externally"] is True


def test_external_parent_and_child_merges_are_adopted(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    with monkeypatch.context() as ctx:
        ctx.chdir(env.repo)
        assert gh_main(["pr", "merge", "1", "--merge"]) == 0
        assert gh_main(["pr", "edit", "2", "--base", "main"]) == 0
        assert gh_main(["pr", "merge", "2", "--merge"]) == 0
    code, out = env.office("land", "--merge", "--quote", "merge accepted tasks")
    assert code == 0 and out.count("already merged externally") == 2, out
    assert _main_tree(env, bare) == _integration_tree(env)
    con = env.con()
    assert all(json.loads(row[0])["merged"] for row in con.execute("SELECT pr_json FROM tasks"))


def test_external_merge_into_parent_is_not_claimed_as_landed_on_main(env, monkeypatch):
    _run(env, monkeypatch, _plan("merge"))
    with monkeypatch.context() as ctx:
        ctx.chdir(env.repo)
        assert gh_main(["pr", "merge", "2", "--merge"]) == 0
    assert gh(env)["prs"][1]["state"] == "merged"
    code, out = env.office("land", "--merge", "--quote", "merge accepted tasks")
    assert code != 0 and "merged-to-other-base" in out, out
    assert "not main" in out and "cannot be adopted" in out
    saved = json.loads(env.con().execute("SELECT pr_json FROM tasks WHERE id='T2'").fetchone()[0])
    assert not saved.get("merged")
