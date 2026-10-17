"""#659: per-task pull request titles carry their run slug, including after sync."""
import json
import re

import pytest

from conftest import GOOD_ADD, start_inline
from office import prs, state
from test_task_prs import gh, github


def _accepted_run(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, extra=("--issue", "42"))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    assert len(gh(env)["prs"]) == 1
    return gh(env)["prs"][0]


def _resync(env):
    con = env.con()
    try:
        rid = con.execute("SELECT id FROM runs").fetchone()[0]
        run = state.get_run(con, rid)
        task = state.get_task(con, rid, "T1")
        dispatch = state.get_dispatch(con, task["current_dispatch_id"])
        prs.ensure_pr(con, run, task, dispatch)
    finally:
        con.close()


def _edit_fake_pr(env, **changes):
    store = gh(env)
    store["prs"][0].update(changes)
    (env.tmp / "gh.json").write_text(json.dumps(store))


def test_task_pr_created_with_readable_unique_run_slug(env, monkeypatch):
    pr = _accepted_run(env, monkeypatch)
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8} T1: Implement add", pr["title"])


def test_generic_title_is_migrated_once_on_sync(env, monkeypatch):
    created = _accepted_run(env, monkeypatch)
    expected = created["title"]
    _edit_fake_pr(env, title="T1: Implement add")
    _resync(env)
    assert gh(env)["prs"][0]["title"] == expected
    _resync(env)
    assert gh(env)["prs"][0]["title"] == expected
    edits = [c for c in gh(env)["calls"] if c[:2] == ["pr", "edit"] and "--title" in c]
    assert len(edits) == 1, edits


def test_manually_owned_pr_title_not_normalized(env, monkeypatch):
    _accepted_run(env, monkeypatch)
    _edit_fake_pr(env, title="Hand-authored PR", body="Human-written PR without Office marker")
    with pytest.raises(RuntimeError, match="not Office-managed"):
        _resync(env)
    assert gh(env)["prs"][0]["title"] == "Hand-authored PR"
    assert gh(env)["prs"][0]["body"] == "Human-written PR without Office marker"


def test_same_goal_with_two_run_ids_produces_distinct_titles():
    task = {"id": "T1", "title": "Implement add"}
    a = prs.task_pr_title({"id": "aa" * 16, "goal": "Make It Good"}, task)
    b = prs.task_pr_title({"id": "bb" * 16, "goal": "Make It Good"}, task)
    assert a != b
    assert a.startswith("make-it-good-aaaaaaaa T1:")
    assert b.startswith("make-it-good-bbbbbbbb T1:")


def test_executor_pr_creation_command_uses_same_title(monkeypatch):
    monkeypatch.setattr(prs, "pr_base", lambda *args: "main")
    run = {"id": "c" * 32, "goal": "Fix the Printer"}
    task = {"id": "T7", "title": "Retry labels"}
    d = {"branch": "office/cccccccc/T7"}
    cmd = prs.create_command(None, run, task, d, "/tmp/brief.md")
    assert '--title "fix-the-printer-cccccccc T7: Retry labels"' in cmd


def test_duplicate_prefix_in_existing_managed_title_is_replaced(env, monkeypatch):
    created = _accepted_run(env, monkeypatch)
    intended = created["title"]
    prefix = intended.split(" T1:", 1)[0]
    _edit_fake_pr(env, title=f"{prefix} {intended}")
    _resync(env)
    assert gh(env)["prs"][0]["title"] == intended
    _resync(env)
    assert gh(env)["prs"][0]["title"] == intended
