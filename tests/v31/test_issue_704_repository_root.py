"""#704: an Office run outlives the caller's disposable linked checkout."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, start_inline, task_row
from office import db, state
from test_task_prs import gh, github, remote_head


def _run(env):
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        return state.get_run(con, run_id)
    finally:
        con.close()


def _linked(env):
    caller = env.repo / ".claude" / "worktrees" / "agent-704"
    env.git("worktree", "add", "-q", "-b", "agent-704", str(caller), "HEAD")
    return caller


def _start(env, caller, *extra):
    code, result = env.ojson("start", "survive the caller checkout", "--gear", "direct+review",
                             "--planner", "inline", *extra, cwd=caller)
    assert code == 0, result
    return _run(env), result


@pytest.mark.parametrize("explicit", [False, True], ids=["caller-head", "caller-relative-base"])
def test_linked_start_anchors_repository_but_preserves_caller_base_and_config(env, explicit):
    main_head = env.git("rev-parse", "HEAD").strip()
    caller = _linked(env)
    config = caller / ".auto-office" / "config.yaml"
    config.parent.mkdir()
    config.write_text("quota:\n  reserve_percent: 7\n")
    env.git("add", ".auto-office/config.yaml", cwd=caller)
    env.git("commit", "-qm", "caller-only configuration", cwd=caller)
    caller_head = env.git("rev-parse", "HEAD", cwd=caller).strip()

    run, result = _start(env, caller, *(('--base', 'HEAD~1') if explicit else ()))
    assert run["base_sha"] == (main_head if explicit else caller_head)
    assert run["policy"]["quota"]["reserve_percent"] == 7
    assert run["repo_root"] == str(env.repo)
    assert run["git_common_dir"] == str(env.repo / ".git")
    draft = env.repo / ".office" / "plans" / run["id"][:8] / "PLAN.md"
    assert str(draft) in result["next"], result
    assert draft.parent.is_dir() and not draft.exists()
    code, result = env.ojson("status", cwd=caller)
    assert code == 0 and str(draft) in result["next"], result
    assert "config differs from files pinned at run start" in "\n".join(result["lines"]), result
    assert "edited since run start" not in "\n".join(result["lines"]), result


def test_inline_submit_and_contract_amendment_use_durable_draft_from_linked_caller(env):
    env.trust()
    caller = _linked(env)
    run, _ = _start(env, caller)
    env.write_plan(PLAN_ONE, run_id=run["id"])
    env.office("submit", cwd=caller, check=0)
    env.office("approve", "plan", "--quote", "go", cwd=caller, check=0)
    env.write_plan(PLAN_ONE.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"), run_id=run["id"])
    env.office("amend", "T1", "--contract", "--", "separate the helper implementation", cwd=caller, check=0)
    assert json.loads(task_row(env)["scope_json"]) == ["calc.py", "calc_helpers.py"]


@pytest.mark.parametrize("command", ["submit", "amend"])
def test_default_inline_draft_cannot_be_redirected_by_an_unrelated_repository(env, command):
    env.trust()
    run, _ = _start(env, env.repo)
    env.write_plan(PLAN_ONE, run_id=run["id"])
    if command == "amend":
        env.office("submit", check=0)
        env.office("approve", "plan", "--quote", "go", check=0)
    before = _run(env)
    original_draft = env.repo / ".office" / "plans" / run["id"][:8] / "PLAN.md"
    original_text = original_draft.read_text()
    other = env.tmp / "unrelated"
    env.git("init", "-q", "-b", "main", str(other))
    env.write_plan(PLAN_ONE.replace("scope: calc.py", "scope: calc.py, decoy.py"),
                   where=other, run_id=run["id"])
    args = (("T1", "--contract", "--run", run["id"], "--", "read the unrelated draft")
            if command == "amend" else ("--run", run["id"]))
    code, output = env.office(command, "--json", *args, cwd=other)
    result = json.loads(output)
    assert code == 2 and result["error"]["category"] in ("wrong-repository", "no-plan-file"), result
    assert _run(env)["plan_version"] == before["plan_version"]
    assert original_draft.read_text() == original_text
    if command == "amend":
        assert json.loads(task_row(env)["scope_json"]) == ["calc.py"]


def test_explicit_plan_file_remains_an_intentional_selection_outside_the_repository(env):
    env.trust()
    run, _ = _start(env, env.repo)
    selected = env.tmp / "selected-plan.md"
    selected.write_text(PLAN_ONE)
    env.office("submit", "--run", run["id"], "--plan", str(selected), cwd=env.tmp, check=0)
    assert _run(env)["plan_version"] == 1


@pytest.mark.parametrize("separate_common", [False, True], ids=["standard-git-dir", "separate-git-dir"])
def test_removed_caller_does_not_break_task_pr_or_lane_worktree(env, monkeypatch, separate_common):
    common = env.tmp / "git-metadata" if separate_common else env.repo / ".git"
    if separate_common:
        env.git("init", "-q", "--separate-git-dir", str(common))
    bare = github(env, monkeypatch)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    caller = _linked(env)
    run, _ = _start(env, caller)
    # Set up the plan in the root this runtime actually recorded, so the old
    # implementation reaches the removal failure instead of failing setup.
    env.write_plan(PLAN_ONE, where=Path(run["repo_root"]), run_id=run["id"])
    env.office("submit", "--plan", str(Path(run["repo_root"]) / ".office" / "plans" / run["id"][:8] / "PLAN.md"),
               cwd=caller, check=0)
    env.office("approve", "plan", "--quote", "go", cwd=caller, check=0)
    env.git("worktree", "remove", "--force", str(caller))
    assert not caller.exists()

    env.office("dispatch", "T1", check=0)
    code, result = env.ojson("status")
    assert code == 0 and result["data"]["tasks"] == {"T1": "accepted"}, result
    assert _run(env)["repo_root"] == str(common if separate_common else env.repo)
    prs = gh(env)["prs"]
    assert len(prs) == 1 and not prs[0]["draft"], prs
    assert any(c.startswith("office: lane L-T1 convergence review APPROVED") for c in prs[0]["comments"]), prs
    con = env.con()
    try:
        accepted = con.execute("SELECT r.commit_sha FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id "
                               "WHERE t.run_id=? AND t.id='T1'", (run["id"],)).fetchone()[0]
        assert remote_head(bare, prs[0]["head"]) == accepted
        assert not con.execute("SELECT 1 FROM events WHERE run_id=? AND kind IN ('pr.error','convergence.blocked')",
                               (run["id"],)).fetchone()
    finally:
        con.close()


def test_missing_legacy_root_reanchors_same_repository_once_without_repinning(env):
    env.trust()
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    original = _run(env)
    caller = _linked(env)
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, original["id"], repo_root=str(caller))
    finally:
        con.close()
    env.git("worktree", "remove", "--force", str(caller))

    env.office("resume", original["id"], check=0)
    env.office("resume", original["id"], check=0)
    recovered = _run(env)
    assert recovered["repo_root"] == str(env.repo)
    assert recovered["base_sha"] == original["base_sha"]
    assert recovered["policy"] == original["policy"]
    assert recovered["gates"] == original["gates"]
    con = env.con()
    try:
        events = con.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='run.repository_reanchored'",
                             (original["id"],)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0]) == {"before": str(caller), "after": str(env.repo),
                                          "git_common_dir": original["git_common_dir"]}
    finally:
        con.close()


@pytest.mark.parametrize("recoverable", [True, False])
def test_wait_resolves_missing_root_before_restarting_its_wake_supervisor(env, monkeypatch, recoverable):
    from office import root_wake

    run, _ = _start(env, env.repo)
    caller = _linked(env)
    common = run["git_common_dir"] if recoverable else str(env.tmp / "gone-main" / ".git")
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, run["id"], repo_root=str(caller), git_common_dir=common)
    finally:
        con.close()
    env.git("worktree", "remove", "--force", str(caller))
    wakes = []

    def ensure(current):
        # A real supervisor launch needs a live cwd. It must never receive
        # the stale pre-reconciliation run row on the wait path.
        assert Path(current["repo_root"]).is_dir()
        wakes.append(current["repo_root"])

    monkeypatch.setattr(root_wake, "ensure", ensure)
    code, result = env.ojson("wait", "--timeout", "0", "--run", run["id"])
    if recoverable:
        assert code in (0, 3), result
        assert wakes == [str(env.repo)]
        assert _run(env)["repo_root"] == str(env.repo)
    else:
        assert code == 4 and result["error"]["category"] == "repository-unavailable", result
        assert wakes == []


@pytest.mark.parametrize("command", ["status", "wait", "resume"])
def test_unrecoverable_missing_root_names_restoration_and_resume(env, command):
    run, _ = _start(env, env.repo)
    missing = env.tmp / "removed-repository"
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, run["id"], repo_root=str(missing), git_common_dir=str(missing / ".git"))
    finally:
        con.close()
    args = ("--timeout", "0") if command == "wait" else ()
    code, result = env.ojson(command, *args, "--run", run["id"])
    assert code == 4 and result["error"]["category"] == "repository-unavailable", result
    assert str(missing) in result["error"]["message"], result
    assert "restore" in result["error"]["next"].lower() and "office resume" in result["error"]["next"], result
    assert "no action" not in result["error"]["next"].lower(), result
    assert _run(env)["repo_root"] == str(missing)


def test_recovery_refuses_candidate_with_a_different_common_git_directory(env):
    run, _ = _start(env, env.repo)
    caller = _linked(env)
    # A path ending in .git is not evidence of repository identity: its parent
    # is a linked checkout belonging to env.repo, not to caller/.git.
    impostor_common = caller / ".git"
    missing = env.tmp / "gone-agent"
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, run["id"], repo_root=str(missing), git_common_dir=str(impostor_common))
    finally:
        con.close()
    code, result = env.ojson("status", "--run", run["id"])
    assert code == 4 and result["error"]["category"] == "repository-unavailable", result
    assert _run(env)["repo_root"] == str(missing)


def test_an_index_directory_does_not_make_a_deleted_checkout_usable(env):
    run, _ = _start(env, env.repo)
    caller = _linked(env)
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, run["id"], repo_root=str(caller))
    finally:
        con.close()
    env.git("worktree", "remove", "--force", str(caller))
    (caller / ".office" / "sessions").mkdir(parents=True)
    env.office("resume", run["id"], check=0)
    assert _run(env)["repo_root"] == str(env.repo)


def test_primary_checkout_with_separate_git_directory_keeps_its_working_root(env):
    common = env.tmp / "git-metadata"
    env.git("init", "-q", "--separate-git-dir", str(common))
    run, _ = _start(env, env.repo)
    assert run["repo_root"] == str(env.repo)
    assert run["git_common_dir"] == str(common)


def test_missing_browser_capture_names_install_and_resume_without_spending_a_review(env, monkeypatch):
    from office import doctor, visual
    from test_issue_651_playwright_doctor import fake_playwright, headless_shell_only

    headless_shell_only(monkeypatch)
    fake_playwright(monkeypatch, failure="BrowserType.launch: Executable doesn't exist at "
                    "/cache/chromium_headless_shell-1248/chrome-headless-shell")
    monkeypatch.setattr(visual, "_shared_browser", None)
    monkeypatch.setattr(visual, "_wait_url", lambda *args, **kwargs: None)
    # #651's doctor already knows the remediation. The same launch failure
    # during the real capture path must leave an actionable evidence blocker.
    message, problem = doctor._visual_capture_status()
    assert problem and "playwright install chromium-headless-shell" in message
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, plan=PLAN_ONE.replace("visual: none", "visual:\n  url: http://127.0.0.1:3999/\n  viewports: desktop"))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        gate = dict(con.execute("SELECT * FROM gates WHERE kind='visual'").fetchone())
        assert gate["review_status"] == "EVIDENCE_BLOCKED" and gate["evidence_status"] == "CAPTURE_BLOCKED", gate
        assert gate["verdict"] is None
        assert not con.execute("SELECT 1 FROM dispatches WHERE role='visual_reviewer'").fetchone()
    finally:
        con.close()
    run = _run(env)
    assert run["landing"]["convergence"]["L-T1"]["status"] == "evidence_blocked"
    assert run["landing"].get("integration", {}).get("status") != "accepted"
    assert task_row(env)["status"] == "accepted"
    code, result = env.ojson("status")
    assert code == 0 and "playwright install chromium-headless-shell" in result["next"], result
    assert "office resume" in result["next"] and "no action" not in result["next"], result
    assert "office amend" not in result["next"], result


def test_unexpected_browser_error_is_not_relabelled_as_missing_installation(env, monkeypatch):
    from office import visual

    run, _ = _start(env, env.repo)
    monkeypatch.setattr(visual, "_wait_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)

    def fail(*args):
        raise RuntimeError("unexpected codec failure")

    monkeypatch.setattr(visual, "_playwright_capture", fail)
    con = env.con()
    try:
        with pytest.raises(RuntimeError, match="unexpected codec failure"):
            visual.capture_all(con, run, {"id": "T1", "visual": {"url": "http://127.0.0.1:3999/"}},
                               {"id": "R1", "commit_sha": run["base_sha"]}, {"id": "G1", "recaptures": 0}, env.repo)
    finally:
        con.close()
