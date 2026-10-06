"""Tasks with `scope: none` (a comment or issue edit): accepted on gates, no PR, skipped by land."""
from __future__ import annotations

from conftest import GOOD_ADD, PLAN_ONE, start_inline
from test_task_prs import github, gh

PLAN_COMMENT = PLAN_ONE.replace("visual: none\n", """visual: none

### T2: Post the summary on the issue
scope: none
depends: T1
checks: none
accept:
- the summary comment is posted
visual: none
""")


def _run(env, monkeypatch):
    bare = github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}, {"reply": "VERDICT: APPROVED\nNEXT proceed"}],
               )
    start_inline(env, plan=PLAN_COMMENT, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = env.office("dispatch", "T1", "T2")
    assert code == 0, out
    return bare


def test_scope_none_parses():
    from office import planfile
    plan = planfile.parse(PLAN_COMMENT)
    assert not plan.errors, plan.errors
    assert plan.tasks[1]["scope"] == []


def test_missing_scope_is_still_an_error():
    from office import planfile
    plan = planfile.parse(PLAN_COMMENT.replace("scope: none\n", ""))
    assert any("missing `scope:`" in e for e in plan.errors), plan.errors


def test_scope_none_task_is_accepted_without_a_pr_and_land_skips_it(env, monkeypatch):
    _run(env, monkeypatch)
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    assert [p["title"].split(":")[0] for p in gh(env)["prs"]] == ["T1"]
    code, out = env.office("inspect", "events")
    assert "pr.error" not in out, out
    code, out = env.office("land", "--merge", "--quote", "merge it")
    assert "no-pr" not in out and "T2 has no file scope and no PR; skipped" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["merged"]


def test_scope_none_evidence_reaches_the_lane_reviewer(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                         {"write": {"OFFICE_EVIDENCE.md": "comment https://github.com/o/r/issues/7#c1: shipped"},
                          "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}, {"reply": "VERDICT: APPROVED\nNEXT proceed"}],
               integration_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env, plan=PLAN_COMMENT, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", "T2", check=0)
    briefs = [p.read_text() for p in env.state.rglob("*") if p.is_file() and p.suffix in (".md", ".txt")
              and "EXECUTOR EVIDENCE" in p.read_text(errors="replace")]
    assert briefs and all("comment https://github.com/o/r/issues/7#c1: shipped" in b for b in briefs), briefs
    assert any("SCOPE none" in p.read_text(errors="replace") for p in env.state.rglob("brief.md"))


PLAN_ONLY_COMMENT = PLAN_ONE.split("## Tasks")[0] + """## Tasks
### T1: Post the summary on the issue
scope: none
depends: none
checks: none
accept:
- the summary comment is posted
visual: none
"""


def test_land_with_prs_off_is_a_no_op_when_no_task_needs_a_pr(env):
    env.trust()
    env.script(executor=[{"submit": True}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}],
               integration_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env, plan=PLAN_ONLY_COMMENT, extra=("--no-prs",))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    code, out = env.office("land", "--merge", "--quote", "merge it")
    assert "prs-off" not in out and "nothing to merge" in out, out


def test_land_with_prs_off_still_refuses_when_a_scoped_task_needs_merging(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}, {"reply": "VERDICT: APPROVED\nNEXT proceed"}],
               integration_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env, plan=PLAN_COMMENT, extra=("--no-prs",))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", "T2", check=0)
    code, out = env.office("land", "--merge", "--quote", "merge it")
    assert "prs-off" in out, out


TO_NONE = PLAN_COMMENT.replace("scope: calc.py\n", "scope: none\n", 1)


def _amend_to_none(env):
    env.write_plan(TO_NONE)
    return env.office("amend", "T1", "--contract", "--", "T1 only comments now")


def test_scope_change_to_none_is_refused_for_a_task_with_a_recorded_pr(env, monkeypatch):
    """A PR-free task is never synced or landed, so a task with a PR (open or closed) keeps its scope."""
    import json
    from conftest import task_row
    _run(env, monkeypatch)
    assert json.loads(task_row(env, "T1")["pr_json"])["number"] == 1
    state = gh(env)
    state["prs"][0]["state"] = "closed"  # closed or not, the record decides; Office asks GitHub nothing
    (env.tmp / "gh.json").write_text(json.dumps(state))
    calls = len(gh(env)["calls"])
    code, out = _amend_to_none(env)
    assert code != 0 and "scope-has-pr" in out and "#1" in out and "new task" in out, out
    assert len(gh(env)["calls"]) == calls
    assert json.loads(task_row(env, "T1")["scope_json"]) == ["calc.py"]  # plan unchanged


def test_scope_change_to_none_is_refused_while_a_pr_job_is_pending(env, monkeypatch):
    """A queued, claimed or failed PR job may have opened a PR that Office never recorded."""
    import json
    from conftest import task_row
    _run(env, monkeypatch)
    con = env.con()
    con.execute("UPDATE tasks SET pr_json=NULL WHERE id='T1'")
    con.execute("UPDATE outbox SET status='failed' WHERE kind='pr_sync' AND payload_json LIKE '%\"T1\"%'")
    con.commit()
    code, out = _amend_to_none(env)
    assert code != 0 and "scope-has-pr" in out and "PR job" in out and "failed" in out, out
    assert json.loads(task_row(env, "T1")["scope_json"]) == ["calc.py"]


def test_a_task_that_never_had_a_pr_can_become_scope_none(env, monkeypatch):
    import json
    from conftest import task_row
    github(env, monkeypatch)
    env.trust()
    start_inline(env, plan=PLAN_COMMENT, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go", check=0)
    code, out = _amend_to_none(env)
    assert code == 0, out
    assert json.loads(task_row(env, "T1")["scope_json"]) == []


def test_has_pr_follows_scope_or_any_recorded_pr():
    from office import prs
    assert not prs.has_pr({"scope": []})
    assert prs.has_pr({"scope": ["a.py"]})
    assert prs.has_pr({"scope": [], "pr": {"number": 3}})
