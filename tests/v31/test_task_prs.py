"""3.2 per-task PRs: pushed task branches, GitHub-stacked draft PRs, verdict comments."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, start_inline

FAKE_GH = Path(__file__).resolve().parent / "fake_gh.py"
PLAN_STACKED = PLAN_TWO.replace("### T2: Implement mul\nscope: mul.py\ndepends: none",
                                "### T2: Implement mul\nscope: mul.py\ndepends: T1")


def github(env, monkeypatch, **state) -> Path:
    """A bare `origin` and a fake `gh` whose PR state lives in a JSON file."""
    bare = env.tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    env.git("remote", "add", "origin", str(bare))
    env.git("push", "-q", "origin", "main")
    gh_state = env.tmp / "gh.json"
    monkeypatch.setenv("FAKE_GH_STATE", str(gh_state))
    if state:
        from fake_gh import load
        gh_state.write_text(json.dumps({**load(), **state}))
    (env.bin / "gh").write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE_GH)!r}, run_name='__main__')\n")
    (env.bin / "gh").chmod(0o755)
    return bare


def gh(env) -> dict:
    return json.loads((env.tmp / "gh.json").read_text())


def remote_head(bare: Path, branch: str) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
                          capture_output=True, text=True).stdout.strip()


def _accepted_commit(env, tid: str) -> str:
    con = env.con()
    try:
        return con.execute("SELECT r.commit_sha FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id "
                           "WHERE t.id=?", (tid,)).fetchone()[0]
    finally:
        con.close()


def test_stacked_tasks_get_stacked_draft_prs_that_leave_draft_on_accept(env, monkeypatch):
    bare = github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                         {"write": {"mul.py": GOOD_MUL}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}, {"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, plan=PLAN_STACKED, extra=("--issue", "42"))
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", "T2")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    state = gh(env)
    prs = {p["title"].split(":")[0]: p for p in state["prs"]}
    assert set(prs) == {"T1", "T2"}, state["prs"]
    t1, t2 = prs["T1"], prs["T2"]
    assert t1["base"] == "main" and t2["base"] == t1["head"], (t1, t2)
    assert not t1["draft"] and not t2["draft"]
    assert "Part of #42" in t1["body"] and "Route: `" in t1["body"] and "stacked on T1 (#1)" in t2["body"]
    # #337: one lane review per task lane, posted to the task PR (no per-task code review).
    assert any(c.startswith("office: lane L-T1 convergence review APPROVED") for c in t1["comments"]), t1
    assert any("accepted" in c for c in t1["comments"])
    # The PR head is exactly the revision the gates accepted.
    assert remote_head(bare, t1["head"]) == _accepted_commit(env, "T1")
    assert remote_head(bare, t2["head"]) == _accepted_commit(env, "T2")


def test_executor_brief_carries_push_and_pr_commands(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    brief = next(env.state.rglob("brief.md")).read_text()
    assert "GIT commit and push your work to this branch as you go: git push -u origin HEAD:refs/heads/office/" in brief
    assert "gh pr create --draft --base main --head office/" in brief
    assert "do not merge, push," not in brief


def test_local_blast_radius_keeps_the_31_flow(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, plan=PLAN_TWO.replace("blast_radius: repo", "blast_radius: local"))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    assert not (env.tmp / "gh.json").exists() or not gh(env)["prs"]
    code, out = env.office("inspect", "run")
    assert code == 0


def test_github_failure_is_a_notice_not_a_failure(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    subprocess.run(["git", "-C", str(env.repo), "remote", "set-url", "--push", "origin", str(env.tmp / "missing.git")],
                   check=True)
    env.office("dispatch", "T1", check=0)
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    code, out = env.office("inspect", "events")
    assert "PR revision: push of office/" in out, out
