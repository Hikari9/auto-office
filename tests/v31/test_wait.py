"""office wait: an orchestrator's watcher keys on the exit code, not status text.

Run b90bbb5b: watch loops grepped `office status` for a few phrases and stayed
silent through an amendment deadlock and a stalled gate that printed none of them.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from conftest import GOOD_ADD, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _go(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("status", check=0)  # consume the dispatch events


@pytest.mark.approved
def test_wait_times_out_with_124_when_nothing_changes(env):
    _go(env)
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code == 124 and "nothing new" in out, out


@pytest.mark.approved
def test_wait_reports_a_gate_nothing_can_advance_as_a_stall(env):
    _go(env)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, created_at) "
                "SELECT 'Gstuck', run_id, 'task', 'T1', 'checks', 'stuck', 'running', '2026-01-01' FROM tasks WHERE id='T1'")
    con.execute("UPDATE outbox SET status='done' WHERE status IN ('queued','claimed')")
    con.commit()
    code, out = env.office("wait", "--timeout", "5", "--poll", "0.2", env=EXTERNAL)
    assert code == 3 and "stall:" in out and "Gstuck" in out, out


@pytest.mark.approved
def test_wait_returns_0_at_once_when_a_task_already_needs_the_orchestrator(env):
    _go(env)
    # A blocker already present when wait starts ends it at once.
    env.office("revoke", "T1", check=0)
    code, out = env.office("wait", "--timeout", "5", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "blocker:" in out, out


# ---- an executor whose agent stopped without submitting

FAKE_HERDR = r'''#!{python}
import json, os, sys
d = os.environ["FAKE_HERDR_DIR"]
def get(name, default=""):
    p = os.path.join(d, name)
    return open(p).read().strip() if os.path.exists(p) else default
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    mode = get("mode", "ok")
    if mode == "gone":
        print(json.dumps({{"error": {{"code": "not_found"}}}}))
        sys.exit(1)
    if mode == "unreachable":
        sys.stderr.write("connection refused")
        sys.exit(1)
    print(json.dumps({{"result": {{"agent": {{"status": get("status", "idle")}}}}}}))
elif args[:2] == ["agent", "read"]:
    if get("mode", "ok") == "unreachable":
        sys.exit(1)
    if get("changing"):
        n = int(get("counter", "0")) + 1
        open(os.path.join(d, "counter"), "w").write(str(n))
        print(get("pane", "") + " tick %d" % n)
    else:
        print(get("pane", ""))
else:
    print("{{}}")
'''


def _herdr(env, **files) -> dict:
    """A fake herdr driven by files in a directory; `files` sets the agent's state."""
    import sys
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = env.tmp / "herdr-fake"
    state.mkdir(exist_ok=True)
    for name, text in files.items():
        (state / name).write_text(text)
    return {**EXTERNAL, "FAKE_HERDR_DIR": str(state), "OFFICE_EXECUTOR_IDLE_STALL_S": "0"}


def _as_herdr(env) -> dict:
    """Make T1's dispatch a running pane-hosted one, as if herdr had launched it."""
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p1', status='running' WHERE task_id='T1' AND role='executor'")
    con.commit()
    return dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone())


def _wait(env, e, timeout="1"):
    return env.office("wait", "--timeout", timeout, "--poll", "0.2", env=e)


def test_a_working_agent_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="working", pane="thinking")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_an_agent_idle_past_the_threshold_without_submitting_is_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> waiting for input")
    d = _as_herdr(env)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "stall:" in out and d["id"] in out and "idle" in out, out
    assert "office prompt D" in out and "office rerun T1" in out and "office revoke T1" in out, out
    tail = Path(paths_run_dir(env)) / "dispatches" / d["id"] / "pane-tail.txt"
    assert "waiting for input" in tail.read_text()


def paths_run_dir(env) -> str:
    from office import paths
    con = env.con()
    return str(paths.run_dir(con.execute("SELECT id FROM runs").fetchone()[0]))


def test_the_idle_threshold_holds_across_wait_invocations(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> waiting")
    e["OFFICE_EXECUTOR_IDLE_STALL_S"] = "1800"
    d = _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124, out
    con = env.con()
    assert con.execute("SELECT idle_since FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0]
    con.execute("UPDATE dispatches SET idle_since='2020-01-01T00:00:00.000000+00:00' WHERE id=?", (d["id"],))
    con.commit()
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and d["id"] in out, out


def test_an_agent_idle_after_an_accepted_submit_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> done")
    d = _as_herdr(env)
    (Path(d["worktree"]) / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=d["worktree"], env={**e, "OFFICE_DISPATCH_ID": d["id"], "OFFICE_ROLE": "executor"})
    assert code == 0, out
    code, out = _wait(env, e)
    assert "stall" not in out, out


def test_agy_reporting_idle_while_its_pane_changes_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="esc to cancel", changing="1")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_herdr_unreachable_is_never_a_stall(env):
    _go(env)
    e = _herdr(env, mode="unreachable")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_a_dead_headless_process_is_a_stall(env):
    import subprocess
    import sys
    _go(env)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='process', pid=?, status='running' WHERE task_id='T1' AND role='executor'",
                (gone.pid,))
    con.commit()
    from office import guide, state
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0]
    lines = guide.stalls(con, state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0]))
    assert len(lines) == 1 and did in lines[0] and "gone" in lines[0], lines


def test_the_stall_line_cites_a_refused_submit(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> stuck")
    d = _as_herdr(env)
    # Submitting from outside the task worktree is refused.
    code, out = env.office("submit", cwd=env.repo, env={**e, "OFFICE_DISPATCH_ID": d["id"], "OFFICE_ROLE": "executor"})
    assert code == 4 and "wrong-worktree" in out, out
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "last submit was refused" in out and "wrong-worktree" in out, out


def test_the_idle_threshold_defaults_to_60_seconds(monkeypatch):
    from office import guide
    monkeypatch.delenv("OFFICE_EXECUTOR_IDLE_STALL_S", raising=False)
    assert guide.idle_stall_s() == 60
    monkeypatch.setenv("OFFICE_EXECUTOR_IDLE_STALL_S", "5")
    assert guide.idle_stall_s() == 5


# ---- a Claude usage-limit stop (#253)

SESSION_LIMIT = "esc to interrupt\n✽ Pondering… (1m 2s)\n⚠ Usage limit reached · limit resets 9:30pm\n"
MANILA_LIMIT = "esc to interrupt\nYou've hit your session limit · resets 9:30pm (Asia/Manila)\n"


def _utc(*a):
    from datetime import datetime, timezone
    return datetime(*a, tzinfo=timezone.utc)


def test_usage_limit_parses_time_forms():
    from office import dispatch
    now = _utc(2026, 10, 2, 10, 0)
    got = dispatch._usage_limit("hit your session limit · resets 9:30pm (Asia/Manila)", now)
    assert got["resets_at"] == _utc(2026, 10, 2, 13, 30) and got["tz"] == "Asia/Manila" and got["local"] == "21:30"
    got = dispatch._usage_limit("hit your session limit · resets 9pm (Asia/Manila)", now)
    assert got["resets_at"] == _utc(2026, 10, 2, 13, 0)
    got = dispatch._usage_limit("hit your session limit · resets 12am (UTC)", now)
    assert got["resets_at"] == _utc(2026, 10, 3, 0, 0)


def test_usage_limit_without_a_zone_uses_the_local_zone(monkeypatch):
    import time
    from office import dispatch
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        now = _utc(2026, 10, 2, 10, 0)
        assert dispatch._usage_limit("Usage limit reached · resets 9:30pm", now)["resets_at"] == _utc(2026, 10, 2, 21, 30)
        assert dispatch._usage_limit("hit your session limit · resets 9pm", now)["resets_at"] == _utc(2026, 10, 2, 21, 0)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_usage_limit_past_reset_rolls_to_the_next_day():
    from office import dispatch
    now = _utc(2026, 10, 2, 14, 0)  # 22:00 in Manila, after a 9:30pm reset
    got = dispatch._usage_limit("hit your session limit · resets 9:30pm (Asia/Manila)", now)
    assert got["resets_at"] == _utc(2026, 10, 3, 13, 30)


def test_a_weekly_limit_warning_is_not_a_usage_limit():
    from office import dispatch
    assert dispatch._usage_limit("You've used 95% of your weekly limit · resets Oct 5") is None
    assert dispatch._usage_limit("working…") is None and dispatch._usage_limit(None) is None


def test_only_the_last_pane_lines_count():
    from office import dispatch
    old = "You've hit your session limit · resets 9pm\n" + "\n".join(f"line {i}" for i in range(60))
    assert dispatch._usage_limit(old) is None


def test_a_session_limit_is_a_usage_limit_stall_despite_busy_markers(env):
    _go(env)
    e = _herdr(env, status="working", pane=MANILA_LIMIT)
    e["OFFICE_EXECUTOR_IDLE_STALL_S"] = "1800"
    d = _as_herdr(env)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out and f"office prompt {d['id']} -- continue" in out, out
    assert "(Asia/Manila 21:30)" in out and "resets 20" in out, out
    row = env.con().execute("SELECT stall_kind, resets_at FROM dispatches WHERE id=?", (d["id"],)).fetchone()
    assert row[0] == "usage_limit" and row[1], tuple(row)
    assert "session limit" in (Path(paths_run_dir(env)) / "dispatches" / d["id"] / "pane-tail.txt").read_text()


def test_a_weekly_warning_pane_is_not_a_usage_limit_stall(env):
    _go(env)
    e = _herdr(env, status="working", pane="You've used 95% of your weekly limit")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def _limit_run(env, monkeypatch, *, auto):
    """A limit pane under guide.stalls with `submit_prompt` recorded, not sent."""
    import os
    from office import dispatch, guide, state
    _go(env)
    e = _herdr(env, status="idle", pane=SESSION_LIMIT)
    d = _as_herdr(env)
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", f"{env.bin}:{os.environ['PATH']}")
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, text, pane=None: sent.append((name, text)) or "landed")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    if auto:
        run["policy"] = {**(run.get("policy") or {}), "executor_usage_limit": {"auto_continue": True,
                                                                             "continue_grace_seconds": 60}}
    return con, run, d, sent, guide


def _set_reset(con, d, at):
    con.execute("UPDATE dispatches SET resets_at=? WHERE id=?", (at, d["id"]))
    con.commit()


def test_auto_continue_is_off_by_default(env, monkeypatch):
    con, run, d, sent, guide = _limit_run(env, monkeypatch, auto=False)
    assert len(guide.stalls(con, run)) == 1
    _set_reset(con, d, "2020-01-01T00:00:00+00:00")
    out = guide.stalls(con, run)
    assert len(out) == 1 and "usage_limit" in out[0] and sent == []


def test_auto_continue_sends_one_continue_after_the_reset(env, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from office import rerun
    con, run, d, sent, guide = _limit_run(env, monkeypatch, auto=True)
    first = guide.stalls(con, run)
    assert len(first) == 1 and sent == []  # before the reset: a stall, nothing sent
    _set_reset(con, d, (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())
    assert len(guide.stalls(con, run)) == 1 and sent == []  # inside the grace window
    _set_reset(con, d, "2020-01-01T00:00:00+00:00")
    assert guide.stalls(con, run) == [] and sent == [("w1:p1", "continue")]
    events = con.execute("SELECT 1 FROM events WHERE kind='usage_limit.continue' AND dispatch_id=?", (d["id"],)).fetchall()
    assert len(events) == 1
    for _ in range(3):  # the limit screen still showing never triggers a second send
        guide.stalls(con, run)
    assert len(sent) == 1
    # Pane unchanged on the limit screen past the settle time: reported again, not retried.
    row = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (d["id"],)).fetchone())
    con.execute("UPDATE dispatches SET idle_since='2020-01-01T00:00:00+00:00', idle_hash=? WHERE id=?",
                (rerun.agent_activity(row)["hash"], d["id"]))
    con.commit()
    again = guide.stalls(con, run)
    assert len(again) == 1 and "already sent continue" in again[0] and len(sent) == 1, again


@pytest.mark.parametrize("result,word", [("", "not confirmed"), ("held", "not confirmed"),
                                         (RuntimeError("herdr down"), "send failed")])
def test_an_unconfirmed_auto_continue_is_not_recorded_as_sent(env, monkeypatch, result, word):
    from office import dispatch
    con, run, d, sent, guide = _limit_run(env, monkeypatch, auto=True)
    guide.stalls(con, run)  # the episode is recorded on first sight
    _set_reset(con, d, "2020-01-01T00:00:00+00:00")

    def fake(name, text, pane=None):
        sent.append((name, text))
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(dispatch, "submit_prompt", fake)
    out = guide.stalls(con, run)
    assert len(out) == 1 and word in out[0] and f"office prompt {d['id']} -- continue" in out[0], out
    kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE dispatch_id=? AND kind LIKE 'usage_limit.continue%'",
                                       (d["id"],))]
    assert len(kinds) == 1 and kinds[0] != "usage_limit.continue", kinds
    again = guide.stalls(con, run)  # still reported at once, and never re-sent
    assert len(again) == 1 and word in again[0] and len(sent) == 1, again
