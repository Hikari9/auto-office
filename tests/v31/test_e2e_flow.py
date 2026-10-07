"""End-to-end lifecycle through the real CLI with scripted fake harnesses."""
from __future__ import annotations

import pytest

from conftest import BAD_ADD, GOOD_ADD, approved_run, start_inline


def _approve(env):
    code, out = env.office("approve", "plan", "--quote", "yes, go ahead")
    assert code == 0, out
    return out


def test_normal_code_task_to_close(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    out = start_inline(env)
    assert "plan p1 submitted" in out
    code, out = env.office("dispatch", "T1")
    assert code == 4 and "authorization" in out, out
    _approve(env)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    assert "T1 -> D" in out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    assert "land it" in data["next"] or "office close" in data["next"], data["next"]
    code, out = env.office("close")
    assert code == 4 and "landing not recorded" in out, out
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code == 0, out
    assert "closed" in out


@pytest.mark.approved
def test_code_review_failure_then_fix(env):
    approved_run(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True},
                           {"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    # The first submission failed its deterministic check. Findings wait for the
    # orchestrator (R8): nothing relaunches until it picks resume or fresh.
    assert data["data"]["tasks"]["T1"] == "changes_required", data
    assert "office rerun T1 --resume | --fresh" in data["next"], data
    assert [c["role"] for c in env.calls()].count("executor") == 1
    code, out = env.office("rerun", "T1", "--fresh")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    roles = [c["role"] for c in env.calls()]
    assert roles.count("executor") == 2 and roles.count("convergence_reviewer") == 1, roles


@pytest.mark.approved
def test_tool_cache_outside_scope_is_left_out_of_the_revision(env):
    # A harness hook (graft) drops a session cache into the worktree; it is not the task's work.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD, "graft/.cache/session/s.json": "{}"}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    con = env.con()
    commit = con.execute("SELECT commit_sha FROM revisions WHERE task_id='T1'").fetchone()[0]
    import subprocess
    files = subprocess.run(["git", "-C", str(env.repo), "show", "--name-only", "--format=", commit],
                           capture_output=True, text=True).stdout.split()
    assert files == ["calc.py"], files


def _submitted_files(env):
    import subprocess
    commit = env.con().execute("SELECT commit_sha FROM revisions WHERE task_id='T1'").fetchone()[0]
    return subprocess.run(["git", "-C", str(env.repo), "show", "--name-only", "--format=", commit],
                          capture_output=True, text=True).stdout.split()


def test_untracked_file_outside_scope_is_left_out_with_a_warning(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD, "notes.txt": "x"}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env)
    _approve(env)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    assert _submitted_files(env) == ["calc.py"]


def test_harness_config_edit_outside_scope_is_left_out_with_a_warning(env):
    env.git("config", "commit.gpgsign", "false")
    (env.repo / ".claude").mkdir()
    (env.repo / ".claude" / "settings.json").write_text("{}\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "harness config")
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD, ".claude/settings.json": '{"hooks": {}}\n'}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env)
    _approve(env)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    assert _submitted_files(env) == ["calc.py"]


@pytest.mark.approved
def test_refused_submit_blocks_instead_of_relaunching(env):
    # A tracked source edit outside scope is refused; a fresh session would hit the same
    # refusal, so the task blocks with the reason instead of relaunching.
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD, "README.md": "changed\n"}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1")
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "blocked", data
    roles = [c["role"] for c in env.calls()]
    assert roles.count("executor") == 1, roles
    con = env.con()
    reason = con.execute("SELECT pause_reason FROM tasks WHERE id='T1'").fetchone()[0]
    assert "outside its scope" in reason and "README.md" in reason, reason


def test_dispatch_returns_while_the_worker_is_still_running(env):
    # #125: completion delivery must not block the orchestrator. With the real
    # process launcher, dispatch returns at once and the result arrives later.
    import time
    approved_run(env, executor=[{"sleep": 6, "write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    t = time.time()
    code, out = env.office("dispatch", "T1", env={"OFFICE_LAUNCHER": "process"})
    elapsed = time.time() - t
    assert code == 0, out
    code, data = env.ojson("status")
    assert elapsed < 5 and data["data"]["tasks"]["T1"] in ("launching", "running"), (elapsed, data)
    deadline = time.time() + 60
    while time.time() < deadline:
        code, data = env.ojson("status")
        if data["data"]["tasks"]["T1"] == "accepted":
            break
        time.sleep(1)
    assert data["data"]["tasks"]["T1"] == "accepted", data


def test_user_authorization_next_names_native_question_tool(env):
    start_inline(env)
    code, data = env.ojson("status")
    assert "ask the user (native question tool)" in data["next"], data["next"]
    assert "office approve plan --quote" in data["next"], data["next"]
    code, out = env.office("dispatch", "T1")
    assert code == 4 and "native question tool" in out, out
