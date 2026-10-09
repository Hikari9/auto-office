"""office rerun --resume|--fresh and office dismiss (R5-R8, issue #200).

Findings never start an executor on their own; after a worker ends, the
orchestrator picks: resume that session, or start fresh with the findings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run
from test_adaptive_dispatch import FB1, PLANNED, _approve, _quota

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

FAKE_HERDR = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_AGENT", "gone")
if args[:2] == ["agent", "get"]:
    if mode == "alive":
        print(json.dumps({{"result": {{"agent": {{"name": args[2], "agent_status": "working"}}}}}})); sys.exit(0)
    if mode == "gone":
        sys.stderr.write(json.dumps({{"error": {{"code": "agent_not_found"}}}})); sys.exit(1)
    sys.stderr.write("server unavailable"); sys.exit(1)
print(json.dumps({{"result": {{}}}}))
'''


def _setup(env, monkeypatch, *, session="sess-123", launcher="herdr"):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}] * 3)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    for k, v in EXTERNAL.items():
        monkeypatch.setenv(k, v)
    con = env.con()
    have = {r[1] for r in con.execute("PRAGMA table_info(dispatches)")}
    for col in ("session_id", "resumed_from"):
        if col not in have:  # added by the pane-lifecycle change; tolerate either order
            con.execute(f"ALTER TABLE dispatches ADD COLUMN {col} TEXT")
    did = con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0]
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=started_at, "
                "launcher=?, pane_id='w1:p5', session_id=?, adapter_id='claude', model='m' WHERE id=?",
                (launcher, session, did))
    con.commit()
    return did


def _run(con):
    from office import state
    return state.get_run(con, con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0])


RESUMABLE = {"id": "claude", "invocation": {"executable": "claude"}, "effort_mapping": {"high": "high"},
             "office_profiles": {"worker": {"herdr_kind": "claude", "interactive": {
                 "argv": ["--model", "{model}"], "resume_argv": ["--resume", "{session_id}"]}}}}
NO_RESUME = {**RESUMABLE, "office_profiles": {"worker": {"herdr_kind": "claude", "interactive": {"argv": ["--model", "{model}"]}}}}


def test_resume_argv_appends_the_session_or_says_none():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import adapters
    args, kind = adapters.resume_argv(RESUMABLE, "worker", session_id="S1", model="m", effort="high", cwd=Path("."))
    assert args == ["--model", "m", "--resume", "S1"] and kind == "claude"
    assert adapters.resume_argv(NO_RESUME, "worker", session_id="S1", model="m", effort="high", cwd=Path(".")) is None


@pytest.mark.approved
def test_rerun_needs_exactly_one_mode(env, monkeypatch):
    _setup(env, monkeypatch)
    code, out = env.office("rerun", "T1", env=EXTERNAL)
    assert code == 2 and "--resume" in out and "--fresh" in out, out
    code, out = env.office("rerun", "T1", "--resume", "--fresh", env=EXTERNAL)
    assert code == 2, out


@pytest.mark.approved
def test_fresh_starts_a_new_executor_on_the_same_worktree(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and "fresh session" in out, out
    con = env.con()
    rows = con.execute("SELECT id, worktree FROM dispatches WHERE task_id='T1' AND role='executor' ORDER BY started_at").fetchall()
    assert len(rows) == 2 and rows[0]["worktree"] == rows[1]["worktree"] and rows[1]["id"] != parent


@pytest.mark.approved
def test_resume_refuses_without_a_session_id(env, monkeypatch):
    _setup(env, monkeypatch, session=None)
    code, out = env.office("rerun", "T1", "--resume", env=EXTERNAL)
    assert code == 4 and "no stored harness session id" in out and "office rerun T1 --fresh" in out, out


@pytest.mark.approved
@pytest.mark.parametrize("mode,reason", [("alive", "still running"), ("down", "herdr unreachable")])
def test_resume_refuses_a_live_or_unknown_parent(env, monkeypatch, mode, reason):
    _setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", mode)
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": RESUMABLE})
    con = env.con()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert reason in err.value.message and err.value.next_step == "office rerun T1 --fresh"


@pytest.mark.approved
def test_resume_refuses_an_adapter_without_a_resume_form(env, monkeypatch):
    _setup(env, monkeypatch)
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": NO_RESUME})
    con = env.con()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert "declares no resume form" in err.value.message


@pytest.mark.approved
def test_resume_launches_a_linked_dispatch_carrying_the_resume_argv(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    from office import adapters, rerun
    monkeypatch.setattr(adapters, "load_all", lambda: {"claude": RESUMABLE})
    con = env.con()
    res = rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)
    assert "resuming " + parent in res.lines[0], res.lines
    child = con.execute("SELECT id, resumed_from FROM dispatches WHERE task_id='T1' AND role='executor' "
                        "ORDER BY started_at DESC LIMIT 1").fetchone()
    assert child["resumed_from"] == parent
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert resume["parent"] == parent and resume["session_id"] == "sess-123" and resume["argv"][-2:] == ["--resume", "sess-123"]
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert f"resumed from {parent}" in out, out


@pytest.mark.approved
@pytest.mark.parametrize("adapter,form", [("claude", ["--resume"]), ("codex", ["resume"])])
def test_resume_argv_comes_from_the_recorded_id_after_the_pane_closed(env, monkeypatch, adapter, form):
    sid = "6f1c2a9e-3b7d-4c55-9a41-0d2e8b7a1f33"
    parent = _setup(env, monkeypatch, session=sid)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    con = env.con()
    con.execute("UPDATE dispatches SET adapter_id=?, harness=?, effort='high', pane_closed_at='2026-10-05T00:00:00Z' WHERE id=?",
                (adapter, adapter, parent))
    con.commit()
    from office import rerun
    rerun.rerun(con, _run(con), "T1", resume=True, fresh=False)  # the seed adapter, not a stand-in
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert resume["session_id"] == sid and resume["argv"][-2:] == [*form, sid], resume
    assert "--session-id" not in resume["argv"]


@pytest.mark.approved
def test_inspect_task_names_the_harness_that_gave_no_session_id(env, monkeypatch):
    parent = _setup(env, monkeypatch, session=None)
    con = env.con()
    con.execute("UPDATE dispatches SET harness='agy' WHERE id=?", (parent,))
    con.commit()
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert "session unavailable: agy exposes none" in out, out
    con.execute("UPDATE dispatches SET session_id='sess-7' WHERE id=?", (parent,))
    con.commit()
    _, out = env.office("inspect", "task", "T1", env=EXTERNAL)
    assert "| session sess-7" in out and "unavailable" not in out, out


@pytest.mark.approved
def test_dismiss_closes_ended_panes_and_refuses_live_ones(env, monkeypatch):
    parent = _setup(env, monkeypatch)
    from office import dispatch, rerun
    calls = []
    monkeypatch.setattr(dispatch, "reclaim_pane", lambda run, did, explicit=False: calls.append((did, explicit)) or "closed",
                        raising=False)
    con = env.con()
    res = rerun.dismiss(con, _run(con), "T1")
    assert calls == [(parent, True)] and "closed" in res.lines[0]
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE id=?", (parent,))
    con.commit()
    with pytest.raises(rerun.Refused):
        rerun.dismiss(con, _run(con), parent)
    assert rerun.dismiss(con, _run(con), None, all_=True).lines[-1].startswith("left running")


@pytest.mark.approved
def test_rerun_keeps_its_route_unless_rerouted_and_refuses_on_live_quota(env, monkeypatch):
    """#300: replay is sticky; fresh quota can refuse it; --reroute routes from current evidence."""
    parent = _setup(env, monkeypatch)
    con = env.con()
    triple = con.execute("SELECT triple FROM dispatches WHERE id=?", (parent,)).fetchone()[0]
    harness = triple.split("@", 1)[0]
    from office import candidates
    candidates._QUOTA_CACHE.clear()
    code, out = env.office("rerun", "T1", "--fresh", env={**EXTERNAL, "OFFICE_QUOTA_FIXTURE": json.dumps({harness: 1})})
    assert code != 0 and "cannot run now" in out and "--reroute" in out, out
    code, out = env.office("rerun", "T1", "--resume", "--reroute", env=EXTERNAL)
    assert code != 0 and "--fresh --reroute" in out, out
    candidates._QUOTA_CACHE.clear()
    code, out = env.office("rerun", "T1", "--fresh", env={**EXTERNAL, "OFFICE_QUOTA_FIXTURE": json.dumps({harness: 90})})
    assert code == 0 and f"on {triple} (original route)" in out, out


def _launching(env, monkeypatch):
    """A second session for T1 whose launch job is still queued (no pid yet), as
    `office amend --contract` leaves while it relaunches the task to ack."""
    from office import db, dispatch, state
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    con = env.con()
    run = _run(con)
    with db.transaction(con):
        did = dispatch.request_launch(con, run, "T1", role="executor")
    return con, run, did


@pytest.mark.approved
def test_rerun_refuses_while_a_launch_for_the_task_is_pending(env, monkeypatch):
    # Run 330605a8: amend --contract launched an ack session; rerun --resume started a
    # second one beside it in the same worktree because the first had no pid yet.
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import gates, rerun
    assert gates.worker_live(con, did)
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, run, "T1", resume=False, fresh=True)
    assert "still has a live worker" in err.value.message and did in err.value.message
    # Once its launch job is gone (failed or finished without a process), it no longer holds the task.
    con.execute("UPDATE outbox SET status='failed' WHERE dedup_key=?", (f"launch:{did}",))
    con.commit()
    assert not gates.worker_live(con, did)


@pytest.mark.approved
def test_revoking_one_dispatch_stops_only_that_dispatch(env, monkeypatch):
    # Run 330605a8: `office revoke <dispatch>` also killed the task's other live session.
    _setup(env, monkeypatch, session=None)
    con, run, first = _launching(env, monkeypatch)
    from office import db, dispatch
    # Two live sessions can no longer be requested; build the state the bug left behind.
    con.execute("UPDATE outbox SET status='done' WHERE dedup_key=?", (f"launch:{first}",))
    con.commit()
    with db.transaction(con):
        second = dispatch.request_launch(con, run, "T1", role="executor")
    con.execute("UPDATE dispatches SET status='running', launcher='external' WHERE id IN (?,?)", (first, second))
    con.commit()
    stopped = []
    monkeypatch.setattr(dispatch, "stop_dispatch", lambda run, d, notes=None: stopped.append(d["id"]) or True)
    res = dispatch.revoke(con, run, second, "test")
    assert stopped == [second], stopped
    assert any(first in ln and "left running" in ln for ln in res.lines), res.lines


@pytest.mark.approved
def test_rerun_refuses_while_a_non_current_session_of_the_task_is_live(env, monkeypatch):
    # Review F1/F9: revoking the current dispatch can leave an older one running; rerun must not
    # start a third session beside it.
    _setup(env, monkeypatch, session=None)
    con, run, older = _launching(env, monkeypatch)
    from office import db, dispatch, rerun
    # Two sessions can no longer be requested; build the state an older bug left behind.
    con.execute("UPDATE outbox SET status='done' WHERE dedup_key=?", (f"launch:{older}",))
    con.commit()
    with db.transaction(con):
        current = dispatch.request_launch(con, run, "T1", role="executor")
    con.execute("UPDATE outbox SET status='queued' WHERE dedup_key=?", (f"launch:{older}",))
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at, terminal_classification='success' "
                "WHERE id=?", (current,))
    con.commit()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, run, "T1", resume=False, fresh=True)
    assert older in err.value.message


@pytest.mark.approved
def test_revoking_a_task_cancels_a_launch_that_has_not_started(env, monkeypatch):
    # Review F2: the remedy rerun names (office revoke) must be able to end a pending launch.
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import dispatch, gates, state
    res = dispatch.revoke(con, run, "T1", "test")
    assert any("cancelled" in ln and did in ln for ln in res.lines), res.lines
    assert not gates.worker_live(con, did) and state.get_dispatch(con, did)["status"] == "cancelled"
    job = con.execute("SELECT status FROM outbox WHERE dedup_key=?", (f"launch:{did}",)).fetchone()
    assert job["status"] == "failed"


@pytest.mark.approved
def test_a_launch_revoked_during_setup_starts_no_agent(env, monkeypatch):
    # Review F2: a claimed launch job re-checks the dispatch before it starts the agent.
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import dispatch, state, worktree_setup

    def setup_then_revoked(*a, **kw):
        con.execute("UPDATE dispatches SET status='cancelled', ended_at=started_at WHERE id=?", (did,))
        con.commit()
        return None
    monkeypatch.setattr(worktree_setup, "prepare", setup_then_revoked)
    launched = []
    monkeypatch.setattr(dispatch, "launch", lambda *a, **kw: launched.append(a) or {})
    job = {"kind": "launch_agent", "payload": {"dispatch_id": did, "task_id": "T1", "role": "executor"}}
    out = dispatch.job_launch_agent(con, run, job)
    assert out.get("skipped") == "cancelled" and not launched, out


@pytest.mark.approved
def test_an_amendment_made_while_launching_is_confirmed_from_the_brief(env, monkeypatch):
    # Review F7: a delivery created after the dispatch started but carried by its brief is confirmed.
    import json as _json
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import amend, paths
    con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, content, "
                "created_at) VALUES('dl1', ?, 'A9', 'T1', ?, 9, 'queued', 'x', '2999-01-01T00:00:00+00:00')",
                (run["id"], did))
    con.execute("UPDATE dispatches SET status='running' WHERE id=?", (did,))
    con.commit()
    ddir = paths.run_dir(run["id"]) / "dispatches" / did
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "launch.json").write_text(_json.dumps({"prompt_landed": True}))
    (ddir / "brief-deliveries.json").write_text(_json.dumps(["dl1"]))
    assert amend.confirm_launch_deliveries(con, run) == 1


@pytest.mark.approved
def test_revoke_leaves_a_claimed_launch_to_finish_instead_of_orphaning_its_agent(env, monkeypatch):
    # Review #446 F2/F3: cancelling under a launch job that is already starting the agent left
    # an agent nothing tracked.
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    con.execute("UPDATE outbox SET status='claimed' WHERE dedup_key=?", (f"launch:{did}",))
    con.commit()
    from office import dispatch, state
    res = dispatch.revoke(con, run, "T1", "test")
    assert state.get_dispatch(con, did)["status"] == "launching", res.lines
    assert any(did in ln and "starting its agent" in ln for ln in res.lines), res.lines


@pytest.mark.approved
def test_cancel_never_ends_a_dispatch_that_recorded_its_launch_since_the_snapshot(env, monkeypatch):
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import dispatch, state
    stale = state.get_dispatch(con, did)
    con.execute("UPDATE dispatches SET status='running', launcher='process', pid=1 WHERE id=?", (did,))
    con.commit()
    assert dispatch._cancel_pending_launch(con, run, stale, "test") is False
    assert state.get_dispatch(con, did)["status"] == "running"
    assert con.execute("SELECT status FROM outbox WHERE dedup_key=?", (f"launch:{did}",)).fetchone()[0] == "queued"


@pytest.mark.approved
def test_request_launch_refuses_a_second_executor_session(env, monkeypatch):
    # Review #446 F4: amend and auto-relaunch reach request_launch directly.
    _setup(env, monkeypatch, session=None)
    con, run, did = _launching(env, monkeypatch)
    from office import db, dispatch
    with pytest.raises(dispatch.Refused) as err:
        with db.transaction(con):
            dispatch.request_launch(con, run, "T1", role="executor")
    assert did in err.value.message


# Route flags on rerun (#308): `--as` and `--review-as` work like dispatch's, and a rerun
# without `--as` keeps a user override route, checked for quota, and says so.
CODEX = "codex@1/gpt-6-astra@low"


def _planned_executor_ended(env, *, as_route=None):
    _approve(env)
    args = ["dispatch", "T1", *(["--as", as_route] if as_route else [])]
    code, out = env.office(*args, env=_quota(env))
    assert code == 0, out
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at='2026-10-09T00:00:00+00:00' WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()


def _latest(env):
    con = env.con()
    row = con.execute("SELECT id, triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()
    return row["id"], row["triple"]


def test_as_external_records_the_launch_form_of_the_named_route(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", FB1, "--external", env=_quota(env))
    assert code == 0 and f"on {CODEX} (user override) launching" in out, out
    did, _ = _latest(env)
    override = json.loads(env.con().execute("SELECT override_json FROM dispatches WHERE id=?", (did,)).fetchone()[0])
    assert override["external"] is True and override["triple"] == CODEX


def test_as_cannot_be_combined_with_reroute(env):
    _planned_executor_ended(env, as_route=FB1)
    code, out = env.office("rerun", "T1", "--fresh", "--reroute", "--as", FB1, env=_quota(env))
    assert code == 2 and "--reroute routes from evidence" in out, out


def test_resume_cannot_move_to_another_harness(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--resume", "--as", FB1, env=_quota(env))
    assert code == 4 and "cannot resume T1 on codex" in out and "--fresh --as codex/gpt-6-astra@low" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 1


def test_fresh_as_runs_the_named_route_and_says_it_is_a_user_override(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", FB1, env=_quota(env))
    assert code == 0 and f"on {CODEX} (user override) launching" in out, out
    assert _latest(env)[1] == CODEX


def test_as_needs_the_route_shape_and_cli_needs_as(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", "codex", env=_quota(env))
    assert code == 2 and "<harness>/<model>" in out, out
    code, out = env.office("rerun", "T1", "--fresh", "--cli", "claude", env=_quota(env))
    assert code == 2 and "--cli needs --as" in out, out


def test_rerun_without_as_keeps_the_user_override_route_and_says_so(env):
    _planned_executor_ended(env, as_route=FB1)
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert code == 0 and f"on {CODEX} (user override) launching" in out, out
    assert "original route" not in out and _latest(env)[1] == CODEX


def test_a_user_override_route_is_still_quota_checked_on_rerun(env):
    _planned_executor_ended(env, as_route=FB1)
    before = env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env, codex=1))
    assert code == 4 and f"original route {CODEX} cannot run now" in out, out
    assert "--as <harness>/<model>" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == before


def test_review_as_pins_the_code_reviewer_for_the_task(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--review-as", "codex/gpt-6-astra@low", env=_quota(env))
    assert code == 0, out
    review = json.loads(env.con().execute("SELECT review_override_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert review == {"as": "codex/gpt-6-astra@low", "cli": None, "external": False, "by": "user"}


def test_review_cli_needs_review_as(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--review-cli", "x", env=_quota(env))
    assert code == 2 and "--review-as" in out, out


def test_resume_as_the_same_model_resumes_and_a_model_switch_is_refused(env):
    _planned_executor_ended(env)
    con = env.con()
    con.execute("UPDATE dispatches SET session_id='S1' WHERE task_id='T1'")
    con.commit()
    code, out = env.office("rerun", "T1", "--resume", "--as", "claude/claude-opus-5-5@medium", env=_quota(env))
    assert code == 0 and "resuming" in out, out
    did, _ = _latest(env)
    payload = json.loads(con.execute("SELECT payload_json FROM outbox WHERE dedup_key=?", (f"launch:{did}",)).fetchone()[0])
    argv = payload["resume"]["argv"]
    assert "S1" in argv and "claude-opus-5-5" in argv, argv
    con.execute("UPDATE dispatches SET status='ended', ended_at='2026-10-09T00:00:01+00:00' WHERE id=?", (did,))
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    code, out = env.office("rerun", "T1", "--resume", "--as", "claude/claude-haiku-5-5@medium", env=_quota(env))
    assert code == 4 and "keeps its model and effort" in out and "--fresh --as" in out, out


def test_rerun_follows_a_route_declared_between_rounds_and_resume_refuses_it(env):
    _planned_executor_ended(env)
    code, out = env.office("amend", "route", "T1", "--as", FB1, "--quote", "astra next round", env=_quota(env))
    assert code == 0, out
    code, out = env.office("rerun", "T1", "--resume", env=_quota(env))
    assert code == 4 and "route was changed since" in out, out
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert code == 0 and f"on {CODEX} (user override) launching" in out, out
    assert _latest(env)[1] == CODEX


def test_review_cli_records_the_reviewer_argv(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--review-as", "codex/gpt-6-astra@low",
                           "--review-cli", "agent --flag", env=_quota(env))
    assert code == 0, out
    review = json.loads(env.con().execute("SELECT review_override_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert review == {"as": "codex/gpt-6-astra@low", "cli": "agent --flag", "external": False, "by": "user"}


def test_review_external_records_that_the_user_starts_the_reviewer(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--review-as", "codex/gpt-6-astra@low", "--review-external",
                           env=_quota(env))
    assert code == 0, out
    review = json.loads(env.con().execute("SELECT review_override_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert review == {"as": "codex/gpt-6-astra@low", "cli": None, "external": True, "by": "user"}


# #456: a contract amendment relaunches the executor on the route the task records. The relaunch
# used to take the ended session's route, so a route declared with `office amend route` was lost.
def _contract_amendment(env):
    env.write_plan(PLANNED.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(1, 1) == 2"))
    return env.office("amend", "T1", "--contract", "--", "also check add(1, 1)", env=_quota(env))


def _latest_triple(env):
    return env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0]


def test_a_contract_amendment_relaunches_on_the_as_route(env):
    _planned_executor_ended(env, as_route=FB1)
    code, out = _contract_amendment(env)
    assert code == 0, out
    assert _latest_triple(env) == CODEX


def test_a_contract_amendment_relaunches_on_a_route_declared_since_the_last_round(env):
    _planned_executor_ended(env)
    code, out = env.office("amend", "route", "T1", "--as", "claude/claude-opus-5-5@high", "--quote", "high next round",
                           env=_quota(env))
    assert code == 0, out
    code, out = _contract_amendment(env)
    assert code == 0, out
    assert _latest_triple(env) == "claude@1/claude-opus-5-5@high", "the declared route wins over the ended session's"


def test_an_empty_as_is_refused_not_ignored(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", "", env=_quota(env))
    assert code == 2 and "<harness>/<model>" in out, out
    code, out = env.office("rerun", "T1", "--fresh", "--review-as", "codex", env=_quota(env))
    assert code == 2 and "<harness>/<model>" in out, out


def test_cli_is_recorded_on_the_launch_of_the_named_route(env):
    _planned_executor_ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", FB1, "--cli", "agent --x", env=_quota(env))
    assert code == 0, out
    did, _ = _latest(env)
    payload = json.loads(env.con().execute("SELECT payload_json FROM outbox WHERE dedup_key=?", (f"launch:{did}",)).fetchone()[0])
    assert payload["cli"] == "agent --x", payload


def test_external_without_a_route_to_carry_it_is_refused(env):
    _planned_executor_ended(env)
    con = env.con()
    con.execute("UPDATE dispatches SET route_json='{}' WHERE task_id='T1'")
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]
    code, out = env.office("rerun", "T1", "--fresh", "--external", env=_quota(env))
    assert code == 4 and "has no route to carry --external" in out and "--as <harness>/<model>" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == before
