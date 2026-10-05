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


# ---- a worker stopped on something only the orchestrator resolves

def _executor(env):
    from conftest import task_row
    d = dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (task_row(env)["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"]), d


def _signals(env):
    return [dict(r) for r in env.con().execute("SELECT * FROM events WHERE kind='worker.signal' ORDER BY seq")]


@pytest.mark.approved
def test_a_preflight_stop_wakes_wait_as_a_stall_at_once(env):
    import time
    _go(env)
    wenv, wt, d = _executor(env)
    env.office("revoke", "T1", check=0)
    env.office("status", check=0)  # consume the revoke events: only the worker's own signal is left
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: lease-lost" in out, out
    started = time.time()
    code, out = env.office("wait", "--timeout", "30", "--poll", "0.2", env=EXTERNAL)
    assert time.time() - started < 10, "wait must not sit out the 60s idle stall"
    assert code == 3 and "stall:" in out, out
    line = next(l for l in out.splitlines() if l.startswith("stall:"))
    assert "T1" in line and d["id"] in line and "lease-lost" in line and "next: office status; then office rerun T1" in line, line
    # Reported once: the revoked task still needs the orchestrator (exit 0), but the signal is not a stall again.
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code != 3 and "stall:" not in out, out


@pytest.mark.approved
def test_repeating_the_same_preflight_stop_records_one_event(env):
    _go(env)
    wenv, wt, d = _executor(env)
    env.office("revoke", "T1", check=0)
    for _ in range(3):
        env.office("preflight", cwd=wt, env=wenv, check=4)
    events = _signals(env)
    assert events and all(e["audience"] == "orchestrator" and e["task_id"] == "T1" and e["dispatch_id"] == d["id"]
                          for e in events), events
    assert len({e["summary"] for e in events}) == len(events), "one event per distinct reason"
    assert any("lease-lost" in e["summary"] for e in events), events
    # A different reason is a different signal, recorded once however often it repeats.
    con = env.con()
    con.execute("UPDATE tasks SET status='blocked', pause_reason='worker ended (crash) without submitting' WHERE id='T1'")
    con.commit()
    env.office("preflight", cwd=wt, env=wenv, check=4)
    env.office("preflight", cwd=wt, env=wenv, check=4)
    after = _signals(env)
    assert len(after) == len(events) + 1 and "worker ended (crash)" in after[-1]["summary"], [e["summary"] for e in after]


@pytest.mark.approved
def test_the_same_reason_is_news_again_once_read_and_old_enough(env):
    _go(env)
    wenv, wt, d = _executor(env)
    env.office("revoke", "T1", check=0)
    env.office("preflight", cwd=wt, env=wenv, check=4)
    n = len(_signals(env))
    env.office("status", check=0)  # the orchestrator reads them
    env.office("preflight", cwd=wt, env=wenv, check=4)
    assert len(_signals(env)) == n, "read but recent: still the same news"
    con = env.con()
    con.execute("UPDATE events SET created_at='2000-01-01T00:00:00+00:00' WHERE kind='worker.signal'")
    con.commit()
    env.office("preflight", cwd=wt, env=wenv, check=4)
    assert len(_signals(env)) == 2 * n, [e["summary"] for e in _signals(env)]


@pytest.mark.approved
def test_a_new_distinct_reason_is_recorded_however_many_came_before(env):
    from office import state
    _go(env)
    wenv, wt, d = _executor(env)
    con = env.con()
    run = state.get_run(con, d["run_id"])
    for i in range(30):
        with con:
            state.signal_orchestrator(con, run, source="preflight stop", task_id="T1", dispatch_id=d["id"],
                                      reason=f"reason {i}", next_step="office status")
    with con:
        state.signal_orchestrator(con, run, source="preflight stop", task_id="T1", dispatch_id=d["id"],
                                  reason="reason 0", next_step="office status")  # a repeat: not recorded
    assert len(_signals(env)) == 30 and "reason 29" in _signals(env)[-1]["summary"]
    with con:
        state.signal_orchestrator(con, run, source="submit refused", task_id="T1", dispatch_id=d["id"],
                                  reason="a later, different refusal", next_step="office status")
    assert len(_signals(env)) == 31 and "a later, different refusal" in _signals(env)[-1]["summary"]


@pytest.mark.approved
@pytest.mark.parametrize("first", ["status", "wait"])
def test_a_signal_behind_a_full_window_of_other_events_still_wakes_wait(env, first):
    from office import state
    _go(env)
    wenv, wt, d = _executor(env)
    con = env.con()
    run = state.get_run(con, d["run_id"])
    with con:
        for i in range(20):  # far more than the 4 a command and the 6 a status show
            state.emit(con, run, "note", f"earlier orchestrator event {i}", task_id="T1")
    env.office("revoke", "T1", check=0)
    env.office("preflight", cwd=wt, env=wenv, check=4)
    if first == "status":
        env.office("status", check=0)  # consumes the first six unread events only
    code, out = env.office("wait", "--timeout", "30", "--poll", "0.2", env=EXTERNAL)
    assert code == 3 and "stall: T1" in out and "preflight stop:" in out and "next: office status; then office rerun T1" in out, out
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert "stall: T1" not in out, out  # reported once


@pytest.mark.approved
def test_signal_text_is_one_printable_line_and_bounded(env):
    from office import state
    _go(env)
    wenv, wt, d = _executor(env)
    con = env.con()
    with con:
        state.signal_orchestrator(con, state.get_run(con, d["run_id"]), source="submit refused", task_id="T1",
                                  dispatch_id=d["id"], reason="bad\x1b[31m\nnext: office revoke T2 " + "x" * 900, next_step="n")
    summary = _signals(env)[0]["summary"]
    assert "\n" not in summary and "\x1b" not in summary and len(summary) < 400, summary


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


def test_other_claude_limit_wordings_are_usage_limits():
    from office import dispatch
    now = _utc(2026, 10, 2, 10, 0)
    got = dispatch._usage_limit("5-hour limit reached ∙ resets 9pm (Asia/Manila)", now)
    assert got["resets_at"] == _utc(2026, 10, 2, 13, 0) and got["label"] == "9pm (Asia/Manila)", got
    got = dispatch._usage_limit("Claude usage limit reached. Your limit will reset at 9pm (Asia/Manila).", now)
    assert got["resets_at"] == _utc(2026, 10, 2, 13, 0) and got["label"] == "9pm (Asia/Manila)", got
    assert dispatch._usage_limit("● The API said: rate limit reached, retrying") is None


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


REAL_LIMIT = """\
● Edited src/office/guide.py (+12 -3)

⚠ Usage limit reached · limit resets 9:30pm
  /upgrade to keep using Claude Code

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""


def test_a_limit_followed_by_a_continue_and_activity_is_resolved():
    from office import dispatch
    assert dispatch._usage_limit(REAL_LIMIT)["label"] == "9:30pm"  # limit only: a stall
    for later in ("> continue\n", "> continue\n✽ Pondering… (3s)\n", "esc to interrupt\n",
                  "> continue\n\n● Picking the task back up\n", "❯ continue\n"):
        assert dispatch._usage_limit(REAL_LIMIT + later) is None, later
    # The old limit stays in scrollback while the agent works: not a stall.
    assert dispatch._usage_limit(REAL_LIMIT + "> continue\n● Reading files\n" + "  ⎿ Read 40 lines\n") is None


def test_a_new_limit_after_a_continue_is_a_stall_with_the_new_reset():
    from office import dispatch
    again = "> continue\n● Working\n⚠ Usage limit reached · limit resets 2:15am\n"
    got = dispatch._usage_limit(REAL_LIMIT + again)
    assert got is not None and got["label"] == "2:15am"


def test_the_empty_composer_and_a_weekly_warning_are_not_activity_or_limits():
    from office import dispatch
    # Placeholder text inside the composer box starts with a border, not a prompt echo.
    boxed = REAL_LIMIT.replace("│ >                  ", "│ > Try \"fix the bug\"  ")
    assert dispatch._usage_limit(boxed) is not None
    assert dispatch._usage_limit("● Done\nYou've used 95% of your weekly limit\n> ") is None


def test_an_unresolvable_explicit_zone_gives_no_guessed_time():
    from office import dispatch
    got = dispatch._usage_limit("hit your session limit · resets 9:30pm (Mars/Olympus)", _utc(2026, 10, 2, 10, 0))
    assert got["resets_at"] is None and got["label"] == "9:30pm (Mars/Olympus)" and got["tz"] is None, got


def test_a_limit_stall_with_an_unresolvable_zone_shows_the_raw_label(env):
    _go(env)
    e = _herdr(env, status="idle", pane="Usage limit reached · resets 9:30pm (Mars/Olympus)")
    d = _as_herdr(env)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "9:30pm (Mars/Olympus)" in out and "time not resolved" in out, out
    assert f"office prompt {d['id']} -- continue" in out, out


def test_a_stale_limit_line_with_later_activity_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane=REAL_LIMIT + "> continue\n● Picking the task back up\n")
    e["OFFICE_EXECUTOR_IDLE_STALL_S"] = "1800"
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "usage_limit" not in out, out


# ---- one limit episode is one fingerprint of the screen above the limit line

def test_the_same_label_after_new_context_is_a_new_episode():
    from office import dispatch
    now = _utc(2026, 10, 2, 10, 0)
    first = dispatch._usage_limit(MANILA_LIMIT, now)
    again = dispatch._usage_limit("esc to interrupt\n> continue\n● Picked it back up\n"
                                  "You've hit your session limit · resets 9:30pm (Asia/Manila)\n", now)
    assert first["label"] == again["label"] and first["fingerprint"] != again["fingerprint"]


def test_ticking_spinners_and_timers_keep_the_fingerprint():
    from office import dispatch
    a = dispatch._usage_limit("● Edited calc.py\n✽ Pondering… (1m 2s · ↓ 1.2k tokens)\n" + REAL_LIMIT)
    b = dispatch._usage_limit("● Edited calc.py   \n\n✻ Pondering… (4m 40s · ↓ 3.4k tokens)\n" + REAL_LIMIT)
    c = dispatch._usage_limit("● Edited other.py\n✻ Pondering… (4m 40s)\n" + REAL_LIMIT)
    assert a["fingerprint"] == b["fingerprint"] != c["fingerprint"]


def test_only_the_15_lines_above_the_limit_identify_it():
    from office import dispatch
    context = "\n".join(f"● step {i}" for i in range(15)) + "\n"
    a = dispatch._usage_limit("● older output A\n" + context + REAL_LIMIT)
    b = dispatch._usage_limit("● older output B\n" + context + REAL_LIMIT)
    assert a["fingerprint"] == b["fingerprint"]


def _limit_row(env, d):
    return dict(env.con().execute("SELECT stall_kind, resets_at, limit_label, limit_fingerprint FROM dispatches "
                                  "WHERE id=?", (d["id"],)).fetchone())


def _store_episode(env, d, **cols):
    con = env.con()
    con.execute("UPDATE dispatches SET stall_kind='usage_limit', resets_at=?, limit_label=?, limit_fingerprint=? "
                "WHERE id=?", (cols["resets_at"], cols["limit_label"], cols["limit_fingerprint"], d["id"]))
    con.commit()


def test_a_repeated_limit_screen_after_its_reset_shows_the_new_reset(env):
    from office import dispatch
    _go(env)
    e = _herdr(env, status="idle", pane=MANILA_LIMIT)
    d = _as_herdr(env)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out and "now past" not in out, out
    first = _limit_row(env, d)
    assert first["limit_fingerprint"] == dispatch._usage_limit(MANILA_LIMIT)["fingerprint"], first
    # A later `office wait` process sees the same screen and label after the
    # stored reset passed: either the old limit is still shown or the same work
    # hit the same limit again. The newly parsed reset is stored and shown, and
    # the passed one is named as a possibility, never as the reset.
    _store_episode(env, d, **{**first, "resets_at": "2026-01-01T13:30:00+00:00"})
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "resets 2026-01-01" not in out and "(Asia/Manila 21:30)" in out, out
    assert "stored with reset 2026-01-01T13:30Z, now past" in out and "-- continue" in out, out
    row = _limit_row(env, d)
    assert row["resets_at"] != "2026-01-01T13:30:00+00:00" and row["resets_at"] > "2026-01-02", row


def test_a_new_limit_with_the_same_label_stores_its_own_reset(env):
    _go(env)
    pane = REAL_LIMIT.replace("limit resets 9:30pm", "resets 9:30pm (Asia/Manila)")
    e = _herdr(env, status="idle", pane=pane)
    d = _as_herdr(env)
    # An earlier wait stored the previous episode; the operator sent `continue`
    # and the agent hit a new limit with the same displayed reset.
    _store_episode(env, d, resets_at="2026-01-01T13:30:00+00:00", limit_label="9:30pm (Asia/Manila)",
                   limit_fingerprint="0" * 64)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "usage_limit" in out and "2026-01-01" not in out, out
    row = _limit_row(env, d)
    assert row["resets_at"] != "2026-01-01T13:30:00+00:00" and row["limit_fingerprint"] != "0" * 64, row


def test_a_resolved_limit_clears_the_stored_episode(env):
    _go(env)
    e = _herdr(env, status="idle", pane=REAL_LIMIT + "> continue\n● Picking the task back up\n")
    e["OFFICE_EXECUTOR_IDLE_STALL_S"] = "1800"
    d = _as_herdr(env)
    _store_episode(env, d, resets_at="2026-01-01T13:30:00+00:00", limit_label="9:30pm", limit_fingerprint="0" * 64)
    code, out = _wait(env, e)
    assert code == 124 and "usage_limit" not in out, out
    assert _limit_row(env, d) == {"stall_kind": None, "resets_at": None, "limit_label": None, "limit_fingerprint": None}


# ---- a reset rolled to tomorrow follows tomorrow's DST offset

@pytest.fixture
def new_york(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_local_reset_rolled_across_spring_forward_uses_the_new_offset(new_york):
    from office import dispatch
    now = _utc(2026, 3, 8, 6, 0)  # 01:00 EST; 00:30 tomorrow is EDT
    got = dispatch._usage_limit("hit your session limit · resets 12:30am", now)
    assert got["resets_at"] == _utc(2026, 3, 9, 4, 30) and got["local"] == "00:30" and got["tz"] == "EDT", got
    got = dispatch._usage_limit("hit your session limit · resets 12:30am (America/New_York)", now)
    assert got["resets_at"] == _utc(2026, 3, 9, 4, 30) and got["local"] == "00:30", got


def test_a_local_reset_rolled_across_fall_back_uses_the_new_offset(new_york):
    from office import dispatch
    now = _utc(2026, 11, 1, 5, 0)  # 01:00 EDT; 00:30 tomorrow is EST
    got = dispatch._usage_limit("hit your session limit · resets 12:30am", now)
    assert got["resets_at"] == _utc(2026, 11, 2, 5, 30) and got["tz"] == "EST", got


def test_a_local_reset_later_today_is_today(new_york):
    from office import dispatch
    got = dispatch._usage_limit("Usage limit reached · resets 9:30pm", _utc(2026, 7, 1, 12, 0))
    assert got["resets_at"] == _utc(2026, 7, 2, 1, 30) and got["local"] == "21:30" and got["tz"] == "EDT", got
