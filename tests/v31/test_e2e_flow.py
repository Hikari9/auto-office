"""End-to-end lifecycle through the real CLI with scripted fake harnesses."""
from __future__ import annotations

from conftest import BAD_ADD, GOOD_ADD, GOOD_MUL, PLAN_ONE, PLAN_TWO, start_inline


def _approve(env):
    code, out = env.office("approve", "plan", "--quote", "yes, go ahead")
    assert code == 0, out
    return out


def test_normal_code_task_to_close(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               code_reviewer=[{"reply": "VERDICT: PASS"}])
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


def test_code_review_failure_then_fix(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": BAD_ADD}, "submit": True},
                         {"write": {"calc.py": GOOD_ADD}, "submit": True}],
               code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env)
    _approve(env)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    roles = [c["role"] for c in env.calls()]
    # First submission failed its deterministic check; the runtime relaunched
    # the executor with the finding (no orchestrator turn), then review ran once.
    assert roles.count("executor") == 2 and roles.count("code_reviewer") == 1, roles


def test_tool_cache_outside_scope_is_left_out_of_the_revision(env):
    # A harness hook (graft) drops a session cache into the worktree; it is not the task's work.
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD, "graft/.cache/session/s.json": "{}"}, "submit": True}],
               code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env)
    _approve(env)
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


def test_refused_submit_blocks_instead_of_relaunching(env):
    # A real out-of-scope file is refused; a fresh session would hit the same
    # refusal, so the task blocks with the reason instead of relaunching.
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD, "notes.txt": "x"}, "submit": True}],
               code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env)
    _approve(env)
    env.office("dispatch", "T1")
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "blocked", data
    roles = [c["role"] for c in env.calls()]
    assert roles.count("executor") == 1, roles
    con = env.con()
    reason = con.execute("SELECT pause_reason FROM tasks WHERE id='T1'").fetchone()[0]
    assert "outside its scope" in reason and "notes.txt" in reason, reason


def test_dispatch_returns_while_the_worker_is_still_running(env):
    # #125: completion delivery must not block the orchestrator. With the real
    # process launcher, dispatch returns at once and the result arrives later.
    import time
    env.trust()
    env.script(executor=[{"sleep": 6, "write": {"calc.py": GOOD_ADD}, "submit": True}],
               code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env)
    _approve(env)
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
