"""The sync launcher supervises and runs fake agents in this process.

`OFFICE_LAUNCHER=sync` used to start `office _supervise` as a child process and
the fake harness as a grandchild. These tests hold the in-process path to the
same recorded outcome as the real processes it replaces.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import fake_agent
from conftest import GOOD_ADD, Env, _activate, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PASS = {"reply": "VERDICT: PASS"}


def _supervise_as_child(dispatch_id, cwd, extra):
    """What the sync launcher did before: a real `office _supervise` process."""
    from office import frontdoor
    argv, _ = frontdoor.current_argv()
    env = {**os.environ, **extra}
    env.pop(frontdoor.HOP_ENV, None)
    subprocess.run(argv + ["_supervise", dispatch_id], cwd=str(cwd), stdin=subprocess.DEVNULL, env=env)


def _identity_pids(env, which):
    pids = set()
    for f in env.state.rglob(f"{which}.identity"):
        pids.add(json.loads(f.read_text())["pid"])
    return pids


def _outcome(env):
    """Everything a dispatch leaves behind that must not depend on how it ran."""
    con = env.con()
    try:
        return {
            "dispatches": sorted(tuple(r) for r in con.execute(
                "SELECT role, status, terminal_classification, exit_code, signal FROM dispatches")),
            "revisions": sorted(tuple(r) for r in con.execute("SELECT task_id, seq, status FROM revisions")),
            "gates": sorted((r[0], r[1], r[2] or "") for r in con.execute("SELECT kind, status, verdict FROM gates")),
            "task": task_row(env)["status"],
        }
    finally:
        con.close()


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    con.close()
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor"}, Path(d["worktree"])


def _executor_flow(env):
    """The agent itself submits, which launches the code reviewer."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[PASS])
    env.office("dispatch", "T1", check=0)


def _reviewer_flow(env):
    """The executor is external; the code reviewer is launched by this process's own submit."""
    approved_run(env, executor=[{}], code_reviewer=[PASS])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    env.office("submit", cwd=wt, env=wenv, check=0)


def _run_flow(tmp_path, monkeypatch, name, flow, *, as_child):
    from office import dispatch
    e = _activate(Env(tmp_path / name, monkeypatch), monkeypatch)
    if as_child:
        monkeypatch.setattr(dispatch, "_supervise_in_process", _supervise_as_child)
    ran = []
    real_run = fake_agent.run
    monkeypatch.setattr(fake_agent, "run", lambda *a: ran.append(a[0][0]) or real_run(*a))
    flow(e)
    return e, ran


@pytest.mark.parametrize("flow", [_executor_flow, _reviewer_flow], ids=["executor", "reviewer"])
def test_in_process_launch_records_what_real_processes_record(tmp_path, monkeypatch, flow):
    inproc, ran = _run_flow(tmp_path, monkeypatch, "inproc", flow, as_child=False)
    inproc_outcome = _outcome(inproc)
    assert ran, "no agent ran in this process"
    assert _identity_pids(inproc, "supervisor") == {os.getpid()}
    assert _identity_pids(inproc, "agent") == {os.getpid()}

    child, ran = _run_flow(tmp_path, monkeypatch, "child", flow, as_child=True)
    assert not ran, "the real supervisor ran an agent in this process"
    assert os.getpid() not in _identity_pids(child, "supervisor") | _identity_pids(child, "agent")
    assert _outcome(child) == inproc_outcome
    # The flow reached the end: the revision was accepted by its code review.
    assert inproc_outcome["task"] == "accepted", inproc_outcome
    assert any(v == "PASS" for _, _, v in inproc_outcome["gates"]), inproc_outcome
    ended = {role: [d[2:4] for d in inproc_outcome["dispatches"] if d[0] == role] for role in ("executor", "code_reviewer")}
    assert ended["code_reviewer"] == [("success", 0)], ended
    # The reviewer flow's executor is external: it was never supervised.
    assert ended["executor"] == ([("success", 0)] if flow is _executor_flow else [(None, None)]), ended


@pytest.mark.approved
def test_subprocess_supervisor_path_stays_end_to_end(env, monkeypatch):
    # One flow with a real supervisor process and real harness processes all the way down.
    from office import dispatch
    monkeypatch.setattr(dispatch, "_supervise_in_process", _supervise_as_child)
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[PASS])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    assert os.getpid() not in _identity_pids(env, "supervisor") | _identity_pids(env, "agent")
    assert [c["role"] for c in env.calls()] == ["executor", "code_reviewer"]


@pytest.mark.approved
def test_supervisor_gets_the_subprocess_environment_and_hands_it_back(env, monkeypatch):
    from office import dispatch, frontdoor
    calls = []
    real = dispatch.supervise
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

    def spy(dispatch_id):
        seen = {"hops": os.environ.get(frontdoor.HOP_ENV, "absent"), "cwd": Path(os.getcwd()).resolve(),
                "pythonpath": os.environ.get("PYTHONPATH", "").split(os.pathsep)[0],
                "handlers_before": {s: signal.getsignal(s) for s in handlers}}
        calls.append(seen)
        out = real(dispatch_id)
        seen["handlers_after"] = {s: signal.getsignal(s) for s in handlers}
        return out

    during = []
    real_run = fake_agent.run
    monkeypatch.setattr(fake_agent, "run", lambda *a: during.append({s: signal.getsignal(s) for s in handlers}) or real_run(*a))
    monkeypatch.setattr(dispatch, "supervise", spy)
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[PASS])
    before = dict(os.environ), os.getcwd()
    env.office("dispatch", "T1", env={frontdoor.HOP_ENV: "1"}, check=0)
    assert before == (dict(os.environ), os.getcwd())
    executor, reviewer = calls  # the reviewer is supervised from inside the executor's supervisor
    assert executor["cwd"] == _worker(env)[1].resolve()
    for seen in calls:
        assert seen["hops"] == "absent"
        assert seen["pythonpath"] == str(Path(frontdoor.__file__).resolve().parents[1])
    # While an agent ran, its supervisor's own forwarding handlers were installed.
    assert len(during) == 2 and all(d[s] is not handlers[s] and callable(d[s]) for d in during for s in handlers)
    # Each hands back what it found: the test's, then the executor supervisor's.
    assert executor["handlers_after"] == handlers
    assert reviewer["handlers_before"] == during[0] == reviewer["handlers_after"]


@pytest.mark.parametrize("action", [{"exit": 0}, {"exit": 3}, {"signal": "TERM"}, {"signal": "KILL"}, {"signal": "HUP"},
                                    {"reply": "hello", "stderr": "warn\n", "raw": "raw line"},
                                    {"write": {"sub/x.txt": "x"}, "exit": 7}],
                         ids=["exit0", "exit3", "term", "kill", "hup", "stream-order", "write-exit7"])
def test_scripted_ending_equals_a_real_harness_process(env, action):
    env.script(executor=[action])
    prompt = "ROLE executor\nbrief\n"
    child_env = {**os.environ, "OFFICE_RUN_ID": "abcd1234-run", "OFFICE_TASK_ID": "T1"}
    work = env.tmp / "work-real"
    work2 = env.tmp / "work-inproc"
    work.mkdir()
    work2.mkdir()
    real = subprocess.run([str(env.bin / "codex")], input=prompt.encode(), cwd=work, env=child_env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    env.script(executor=[action])  # rewind the scenario's counter
    code, output = fake_agent.run([str(env.bin / "codex")], prompt, {**child_env, "FAKE_HARNESS": "codex"}, work2)
    assert (code, output) == (real.returncode, real.stdout)
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()) == \
        sorted(p.relative_to(work2).as_posix() for p in work2.rglob("*") if p.is_file())


def test_scenarios_that_need_a_real_process_are_detected(env):
    def ok(**roles):
        env.script(**roles)
        return fake_agent.scenario_runs_in_process({"FAKE_SCENARIO": str(env.scenario)})

    assert ok(executor=[{"exit": 3}, {"signal": "TERM"}, {"signal": "KILL"}, {"signal": "HUP"}])
    assert not ok(executor=[{"exit": 0}, {"sleep": 1}])
    assert not ok(code_reviewer=[{"signal": "INT"}])
    assert not ok(executor=[{"signal": "USR1"}])
    assert not fake_agent.scenario_runs_in_process({})
    assert not fake_agent.scenario_runs_in_process({"FAKE_SCENARIO": str(env.tmp / "missing.json")})


@pytest.mark.approved
@pytest.mark.parametrize("scenario,env_extra", [
    ({"executor": [{"sleep": 0.2, "exit": 3}]}, {}),
    ({"executor": [{"exit": 3}]}, {"OFFICE_WORKER_MAX_MINUTES": "5"}),
], ids=["sleep", "wall-cap"])
def test_what_in_process_cannot_reproduce_falls_back_to_a_real_process(env, scenario, env_extra, monkeypatch):
    ran = []
    real_run = fake_agent.run
    monkeypatch.setattr(fake_agent, "run", lambda *a: ran.append(a) or real_run(*a))
    approved_run(env, **scenario)
    env.office("dispatch", "T1", env=env_extra, check=0)
    con = env.con()
    rows = con.execute("SELECT terminal_classification, exit_code FROM dispatches WHERE role='executor'").fetchall()
    assert rows and {tuple(r) for r in rows} == {("nonzero", 3)}, [tuple(r) for r in rows]
    assert not ran, "a scenario that needs a real process ran in this process"
    assert os.getpid() not in _identity_pids(env, "agent")
    assert _identity_pids(env, "agent"), "the agent was never started"


def test_the_seam_only_replaces_the_unmodified_fake_harness(env):
    from conftest import InProcessAgent
    started = []

    def real(argv, cwd, child_env, stdin):
        started.append(argv)
        return "real child"

    child_env = {"PATH": os.environ["PATH"], "FAKE_SCENARIO": str(env.scenario)}
    spawn = lambda argv: env._spawn_agent(real, argv, str(env.repo), child_env, subprocess.PIPE)
    assert isinstance(spawn(["codex"]), InProcessAgent) and not started
    assert spawn(["git", "--version"]) == "real child"  # not a fake harness
    (env.bin / "codex").write_text(f"#!{sys.executable}\nraise SystemExit(5)\n")
    assert spawn(["codex"]) == "real child"  # a test's own stand-in runs as itself
    assert started == [["git", "--version"], ["codex"]]


@pytest.mark.approved
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_an_in_process_agent_stops_it_like_a_killed_child(env, monkeypatch, signum):
    after = []

    def agent(argv, prompt, child_env, cwd):
        os.kill(os.getpid(), signum)
        after.append("kept running")  # a killed child never gets here
        return 0, b""

    monkeypatch.setattr(fake_agent, "run", agent)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    approved_run(env, executor=[{}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    rows = {tuple(r) for r in con.execute(
        "SELECT terminal_classification, exit_code, signal FROM dispatches WHERE role='executor'")}
    assert rows == {("signal", -int(signum), int(signum))}, rows
    assert not after
    assert handlers == {s: signal.getsignal(s) for s in handlers}
