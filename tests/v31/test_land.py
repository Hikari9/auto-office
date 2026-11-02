"""3.2 `office land`: the user's end state after the task PRs."""
from __future__ import annotations

import json

from conftest import GOOD_ADD, GOOD_MUL, start_inline
from test_task_prs import PLAN_STACKED, gh, github, remote_head

SCRIPT = dict(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"write": {"mul.py": GOOD_MUL}, "submit": True}],
              convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}, {"reply": "VERDICT: APPROVED\nNEXT proceed"}],
              integration_reviewer=[{"reply": "VERDICT: PASS"}])


def _plan(end_state: str, **deploy) -> str:
    extra = "".join(f"deploy_{k}: {v}\n" for k, v in deploy.items())
    return PLAN_STACKED.replace("blast_radius: repo\n", f"blast_radius: repo\nend_state: {end_state}\n{extra}")


def _run(env, monkeypatch, plan: str, **gh_state):
    bare = github(env, monkeypatch, **gh_state)
    env.trust()
    env.script(**SCRIPT)
    out = start_inline(env, plan=plan, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go end to end", check=0)
    env.office("dispatch", "T1", "T2", check=0)
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data
    return bare, out, data


def _main_tree(env, bare) -> str:
    return env.git("--git-dir", str(bare), "rev-parse", "main^{tree}").strip()


def _integration_tree(env) -> str:
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    finally:
        con.close()
    return env.git("rev-parse", f"{landing['integration']['commit']}^{{tree}}").strip()


def test_e2e_merges_bottom_up_deploys_verifies_and_closes(env, monkeypatch):
    marker = env.tmp / "deployed"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','w').write('ok')\"", verify=f"test -f {marker}")
    bare, out, data = _run(env, monkeypatch, plan)
    assert "merge PRs bottom-up into main (merge) -> deploy prod `python3" in out and "-> verify `test -f" in out, out
    assert data["next"] == "office land (end state: e2e)", data
    code, out = env.office("land")
    assert code == 0, out
    assert "T1 #1 merged (merge)" in out and "T2 #2 merged (merge)" in out, out
    assert "matches the reviewed integration tree" in out and "prod deploy ok" in out and "prod verify ok" in out, out
    assert marker.read_text() == "ok"
    assert _main_tree(env, bare) == _integration_tree(env)
    state = gh(env)
    assert [p["state"] for p in state["prs"]] == ["merged", "merged"] and state["closed_issues"] == ["7"]
    code, out = env.office("close")
    assert code == 0 and "closed" in out, out


def test_ask_mode_lists_prs_then_merges_on_the_users_words(env, monkeypatch):
    bare, out, data = _run(env, monkeypatch, PLAN_STACKED)
    assert "PRs ready -> ask: merge / preview / e2e / stop" in out, out
    code, out = env.office("land")
    assert code == 0 and "https://github.com/o/r/pull/1" in out and "office land --merge" in out, out
    code, out = env.office("land", "--merge")
    assert code == 2 and "user-quote-required" in out, out
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "T2 #2 merged" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    env.office("close", check=0)


def test_squash_only_repo_restacks_the_child(env, monkeypatch):
    repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": False,
            "squashMergeAllowed": True, "rebaseMergeAllowed": False}
    bare, out, _ = _run(env, monkeypatch, _plan("merge"), repo=repo)
    assert "(squash)" in out, out
    code, out = env.office("land")
    assert code == 0 and "T1 #1 merged (squash)" in out and "T2 #2 merged (squash)" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    assert gh(env)["prs"][1]["base"] == "main"


def test_failing_required_checks_merge_nothing(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), checks_exit=1)
    before = remote_head(bare, "main")
    code, out = env.office("land")
    assert code == 4 and "checks-failed" in out, out
    assert remote_head(bare, "main") == before
    code, out = env.office("close")
    assert code == 4 and "landing not recorded" in out, out


def test_preview_deploys_the_integration_and_leaves_prs_open(env, monkeypatch):
    marker = env.tmp / "preview"
    bare, _, _ = _run(env, monkeypatch, _plan("preview", preview=f"cp calc.py {marker}"))
    code, out = env.office("land")
    assert code == 0 and "preview deploy ok" in out, out
    assert "return a + b" in marker.read_text()
    assert [p["state"] for p in gh(env)["prs"]] == ["open", "open"]
    env.office("close", check=0)


def test_detect_proposes_vercel_commands_before_any_run(env):
    (env.repo / "vercel.json").write_text("{}")
    code, out = env.office("land", "--detect")
    assert code == 0 and "deploy_prod: vercel deploy --prod" in out and "deploy_preview: vercel deploy" in out, out


def test_e2e_without_a_prod_command_is_a_plan_error(env):
    code, out = env.office("start", "g", "--planner", "inline")
    env.write_plan(_plan("e2e"))
    code, out = env.office("submit")
    assert code == 4 and "end_state e2e needs `deploy_prod: <command>`" in out, out


def test_intake_flags_reach_the_diagram_when_the_plan_omits_them(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    code, out = env.office("start", "g", "--gear", "direct+review", "--planner", "inline", "--end-state", "e2e",
                           "--deploy-prod", "make ship", "--deploy-verify", "make smoke")
    assert code == 0, out
    env.write_plan(PLAN_STACKED)
    code, out = env.office("submit")
    assert code == 0, out
    assert "deploy prod `make ship` -> verify `make smoke` -> closeout" in out and "end state: e2e" in out, out


SQUASH_ONLY = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": False,
               "squashMergeAllowed": True, "rebaseMergeAllowed": False}


def _task_branch(env, bare, tid: str) -> str:
    refs = env.git("--git-dir", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads/office/").split()
    return next(r for r in refs if r.endswith(f"/{tid}"))


def test_restack_leases_to_an_ancestor_left_by_a_failed_accepted_push(env, monkeypatch):
    """#538: the accepted revision's push never reached the remote, so the branch
    holds an ancestor of it. The restack must lease to that head, not refuse as stale."""
    from test_task_prs import _accepted_commit
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    branch = _task_branch(env, bare, "T2")
    env.git("--git-dir", str(bare), "update-ref", f"refs/heads/{branch}", _accepted_commit(env, "T1"))
    code, out = env.office("land")
    assert code == 0 and "T2 #2 merged (squash)" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)


def test_restack_refuses_a_foreign_remote_head_naming_both_shas(env, monkeypatch):
    from test_task_prs import _accepted_commit
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    branch = _task_branch(env, bare, "T2")
    foreign = env.git("--git-dir", str(bare), "commit-tree", "-p", _accepted_commit(env, "T2"),
                      "-m", "someone else", f"{_accepted_commit(env, 'T2')}^{{tree}}").strip()
    env.git("--git-dir", str(bare), "update-ref", f"refs/heads/{branch}", foreign)
    code, out = env.office("land")
    assert code == 4 and "restack-push-failed" in out, out
    assert foreign[:12] in out and _accepted_commit(env, "T2")[:12] in out, out
    assert remote_head(bare, branch) == foreign


def test_restack_adopts_a_hand_resolved_head_with_the_reviewed_tree(env, monkeypatch):
    """#676: after a restack-conflict, the user pushes a head that contains main and
    carries the reviewed tree. Land must adopt it, not replay the recorded revision."""
    from office import land
    from test_task_prs import _accepted_commit
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    branch = _task_branch(env, bare, "T2")
    real = land._restack

    def hand_resolved_first(con, run, task, up, base):
        if task["id"] == "T2":
            env.git("fetch", "-q", "origin", "main")
            head = env.git("commit-tree", "-p", "origin/main", "-m", "resolved by hand",
                           f"{_accepted_commit(env, 'T2')}^{{tree}}").strip()
            env.git("push", "-q", "-f", "origin", f"{head}:refs/heads/{branch}")
        return real(con, run, task, up, base)

    monkeypatch.setattr(land, "_restack", hand_resolved_first)
    code, out = env.office("land")
    assert code == 0 and "T2 #2 merged (squash)" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    con = env.con()
    try:
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='pr.restack_adopted'").fetchone()[0] == 1
    finally:
        con.close()


def test_restack_never_adopts_a_head_that_drops_main_changes(env, monkeypatch):
    """A pushed head with only the accepted tree would revert what main gained since; refuse it."""
    from office import land
    from test_landing_gaps import _advance_main
    from test_task_prs import _accepted_commit
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    branch = _task_branch(env, bare, "T2")
    real = land._restack

    def drops_main(con, run, task, up, base):
        if task["id"] == "T2":
            _advance_main(env, bare, "NOTES.md", "unrelated\n")
            env.git("fetch", "-q", "origin", "main")
            head = env.git("commit-tree", "-p", "origin/main", "-m", "drops NOTES.md",
                           f"{_accepted_commit(env, 'T2')}^{{tree}}").strip()
            env.git("push", "-q", "-f", "origin", f"{head}:refs/heads/{branch}")
        return real(con, run, task, up, base)

    monkeypatch.setattr(land, "_restack", drops_main)
    code, out = env.office("land")
    assert code == 4 and "restack-push-failed" in out and "will not overwrite" in out, out
