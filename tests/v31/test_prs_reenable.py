"""`office pr on`: task PRs come back after revisions exist (#479 pins them off once one is submitted)."""
from __future__ import annotations

import json

from conftest import GOOD_ADD, GOOD_MUL, start_inline
from test_land import SCRIPT
from test_task_prs import PLAN_STACKED, github, gh, remote_head


def _off_run(env, monkeypatch, **gh_state):
    """A stacked run whose tasks were accepted while GitHub did not answer: no branch pushed, no PR."""
    bare = github(env, monkeypatch, **gh_state)
    env.trust()
    env.script(**SCRIPT)
    start_inline(env, plan=PLAN_STACKED)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", "T2", check=0)
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    assert gh(env)["prs"] == []
    return bare


def _prs_setting(env) -> dict:
    con = env.con()
    try:
        return json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])["prs"]
    finally:
        con.close()


def _accepted_commit(env, tid):
    con = env.con()
    try:
        return con.execute("SELECT r.commit_sha FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id "
                           "WHERE t.id=?", (tid,)).fetchone()[0]
    finally:
        con.close()


def test_prs_on_pushes_accepted_branches_and_opens_the_stacked_prs(env, monkeypatch):
    bare = _off_run(env, monkeypatch, repo_view_failures=2)
    assert _prs_setting(env)["enabled"] is False
    code, out = env.office("pr", "on")
    assert code == 0, out
    assert "T1 #1 pushed, ready for review" in out and "T2 #2 pushed, ready for review" in out, out
    prs = {p["title"].split(":")[0]: p for p in gh(env)["prs"]}
    assert set(prs) == {"T1", "T2"}
    assert prs["T1"]["base"] == "main" and prs["T2"]["base"] == prs["T1"]["head"], prs
    assert not prs["T1"]["draft"] and not prs["T2"]["draft"]
    assert "stacked on T1 (#1)" in prs["T2"]["body"]
    assert remote_head(bare, prs["T1"]["head"]) == _accepted_commit(env, "T1")
    assert remote_head(bare, prs["T2"]["head"]) == _accepted_commit(env, "T2")
    setting = _prs_setting(env)
    assert setting["enabled"] is True and setting["base_branch"] == "main" and "transient" not in setting, setting
    code, events = env.office("inspect", "events")
    assert "task PRs on by office pr on" in events, events
    # The change holds: land sees PRs and merges what was opened.
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "T2 #2 merged" in out, out


def test_prs_on_is_safe_to_repeat(env, monkeypatch):
    _off_run(env, monkeypatch, repo_view_failures=2)
    assert env.office("pr", "on")[0] == 0
    code, out = env.office("pr", "on")
    assert code == 0 and "T1 #1 pushed" in out, out
    assert len(gh(env)["prs"]) == 2


def test_prs_on_refuses_with_the_reason_when_github_is_unavailable(env, monkeypatch):
    bare = _off_run(env, monkeypatch, repo_view_failures=9)
    code, out = env.office("pr", "on")
    assert code == 4 and "prs-unavailable" in out and "task PRs stay off: gh repo view failed" in out, out
    assert "TLS handshake timeout" in out, out
    assert gh(env)["prs"] == []
    assert _prs_setting(env)["enabled"] is False
    assert env.git("--git-dir", str(bare), "branch", "--list", "office/*").strip() == ""  # nothing pushed


def test_prs_on_reports_a_task_that_did_not_sync_and_finishes_on_the_repeat(env, monkeypatch):
    _off_run(env, monkeypatch, repo_view_failures=2)
    state = gh(env)
    state["fail"] = {"pr create": 1}
    (env.tmp / "gh.json").write_text(json.dumps(state))
    code, out = env.office("pr", "on")
    assert code == 4 and "prs-sync-failed" in out and "T1 not synced: gh pr create failed" in out, out
    assert "T2 waits for T1" in out and gh(env)["prs"] == [], (out, gh(env)["prs"])  # no PR on the wrong base
    assert _prs_setting(env)["enabled"] is True  # the change is recorded; only the sync is repeated
    code, out = env.office("pr", "on")
    assert code == 0 and "T2 #2 pushed" in out, out
    assert [p["title"].split(":")[0] for p in gh(env)["prs"]] == ["T1", "T2"]


def test_prs_on_refuses_for_a_local_run(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(**SCRIPT)
    start_inline(env, plan=PLAN_STACKED.replace("blast_radius: repo", "blast_radius: local"))
    code, out = env.office("pr", "on")
    assert code == 4 and "blast radius is local" in out, out


def test_pr_status_shows_the_setting_and_each_tasks_pr(env, monkeypatch):
    _off_run(env, monkeypatch, repo_view_failures=2)
    code, out = env.office("pr", "status")
    assert code == 0 and "task PRs off: gh repo view failed" in out and "will be probed again" in out, out
    assert "T1 (accepted) no PR" in out and "next: office pr on" in out, out
    env.office("pr", "on", check=0)
    code, out = env.office("pr", "status")
    assert "task PRs on (base main, merge merge)" in out and "T1 (accepted) #1 open" in out and "T2 (accepted) #2 open" in out, out


def test_prs_on_after_the_run_ended_pushes_nothing(env, monkeypatch):
    bare = _off_run(env, monkeypatch, repo_view_failures=2)
    env.office("close", "--abandon", "not needed", check=0)
    code, out = env.office("pr", "on")
    assert code != 0 and "no-active-run" in out, out
    assert gh(env)["prs"] == [] and _prs_setting(env)["enabled"] is False
    assert env.git("--git-dir", str(bare), "branch", "--list", "office/*").strip() == ""
