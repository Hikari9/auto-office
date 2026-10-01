"""Herdr dispatch starts the real agent: `herdr agent start` + `agent prompt`, never `sh launch.sh`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import approved_run

FAKE_HERDR = r'''#!{python}
import json, os, shlex, sys
state = os.environ["FAKE_HERDR_STATE"]
data = json.load(open(state)) if os.path.exists(state) else {{"calls": [], "n": 0, "get": []}}
args = sys.argv[1:]
data["calls"].append(args)
result = {{}}
code = 0
if args[:2] == ["pane", "split"]:
    data["n"] += 1
    result = {{"pane": {{"pane_id": "w1:p%d" % (100 + data["n"]), "tab_id": "w1:t1"}}}}
elif args[:2] == ["pane", "run"] and " && touch " in args[3]:
    # The shell runs Office's setup line, except the first FAKE_HERDR_SHELL_DROP
    # sent before it was ready (a fresh pane); "deaf" never runs it.
    data["shell_runs"] = data.get("shell_runs", 0) + 1
    drop = os.environ.get("FAKE_HERDR_SHELL_DROP", "0")
    if drop != "deaf" and data["shell_runs"] > int(drop):
        open(shlex.split(args[3])[-1], "w").close()
elif args[:2] == ["agent", "start"] and os.environ.get("FAKE_HERDR_START_FAIL"):
    code = 1
    result = {{"error": {{"code": "invalid_agent_name"}}}}
elif args[:2] == ["agent", "start"] and not os.environ.get("FAKE_HERDR_NO_AGENT"):
    # herdr sees the agent in its pane once it has started.
    data.setdefault("pane_agents", {{}})[args[args.index("--pane") + 1]] = args[2]
elif args[:2] == ["pane", "get"]:
    result = {{"pane": {{"pane_id": args[2], "agent": data.get("pane_agents", {{}}).get(args[2])}}}}
elif args[:2] == ["agent", "get"]:
    seq = data["get"]
    status = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else "gone")
    if status == "gone":
        code = 1
    else:
        result = {{"agent": {{"name": args[2], "status": status}}}}
elif args[:2] == ["agent", "read"] and os.environ.get("FAKE_HERDR_READ_FAIL"):
    json.dump(data, open(state, "w"))
    sys.exit(1)
elif args[:2] == ["agent", "read"]:
    reads = data.setdefault("reads", [])
    print(reads.pop(0) if len(reads) > 1 else (reads[0] if reads else data.get("content", "")))
    json.dump(data, open(state, "w"))
    sys.exit(0)
json.dump(data, open(state, "w"))
print(json.dumps({{"result": result}}))
sys.exit(code)
'''


BUSY = "Working (1s \u2022 esc to interrupt)"
EMPTY = "> composer empty"
# Two pane reads precede the prompt: the agent-UI wait and the ctx baseline.
PRE = [EMPTY, EMPTY]


def _fake(env, monkeypatch, gets=(), reads=()) -> Path:
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = env.tmp / "herdr-state.json"
    state.write_text(json.dumps({"calls": [], "n": 0, "get": list(gets), "reads": list(reads)}))
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    # No real waiting: the fake answers at once, so session capture and the pause before Enter only cost time.
    monkeypatch.setenv("OFFICE_HERDR_SESSION_WAIT", "0")
    monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
    return state


def _calls(state: Path) -> list:
    return json.loads(state.read_text())["calls"]


def _live_dispatch(env, monkeypatch):
    approved_run(env)
    code, out = env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"})
    assert code == 0, out
    from office import state
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
        d = state.get_dispatch(con, con.execute("SELECT id FROM dispatches WHERE task_id='T1'").fetchone()[0])
    finally:
        con.close()
    return run, d


@pytest.mark.approved
def test_herdr_path_starts_the_agent_and_prompts_it(env, monkeypatch):
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, reads=[BUSY], adapter="claude",
                                                    model="fake-model", effort="high")
    assert res["launcher"] == "herdr" and res["agent"] == f"office-{d['id'].lower()}"
    assert res["prompt_landed"] is True
    calls = _calls(state_file)
    start = next(c for c in calls if c[:2] == ["agent", "start"])
    assert start[start.index("--kind") + 1] == "claude"
    assert start[start.index("--pane") + 1] == res["pane"]
    agent_args = start[start.index("--") + 1:]
    assert agent_args[agent_args.index("--model") + 1] == "fake-model"
    assert agent_args[agent_args.index("--effort") + 1] == "high"
    prompt = next(c for c in calls if c[:2] == ["agent", "prompt"])
    assert prompt[2] == res["agent"] and str(ddir / "brief.md") in prompt[3]
    assert calls.index(start) < calls.index(prompt)
    assert not any("launch.sh" in " ".join(c) or c[-1].startswith("sh ") for c in calls)
    assert not (ddir / "launch.sh").exists()
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["herdr_agent"] == res["agent"]


def test_model_and_effort_in_interactive_argv_for_each_harness():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import adapters
    seeds = adapters.load_all()
    for harness in ("claude", "codex", "agy"):
        got = adapters.interactive_argv(seeds[harness], "worker", model="m-x", effort="medium", cwd=Path("/w"))
        assert got, harness
        args, kind = got
        assert kind == harness
        joined = " ".join(args)
        if harness == "agy":
            # Native Gemini takes the combined slug (gemini-3.8-flash-medium); no --effort.
            assert "m-x" in args and "--effort" not in args, args
        else:
            assert "m-x" in args and "medium" in joined, (harness, args)
        assert args[0] != harness  # the executable comes from --kind, not the args


def _spec(tmp: Path, output: Path | None = None) -> dict:
    return {"herdr_agent": "office-D", "output": str(output) if output else None}


@pytest.mark.approved
def test_single_done_sample_does_not_end_but_repeated_done_does(env, monkeypatch):
    state_file = _fake(env, monkeypatch, gets=["done", "working", "done", "done", "done"])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    code, cls = dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0)
    assert (code, cls) == (0, "success")
    gets = [c for c in _calls(state_file) if c[:2] == ["agent", "get"]]
    assert len(gets) == 5  # the lone first `done` was not trusted


@pytest.mark.approved
def test_repeated_idle_ends_and_process_exit_ends(env, monkeypatch):
    _fake(env, monkeypatch, gets=["idle", "idle", "idle"])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0) == (0, "success")
    _fake(env, monkeypatch, gets=["working", "gone"])
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0) == (None, "nonzero")


@pytest.mark.approved
def test_output_file_or_submit_ends_the_dispatch(env, monkeypatch):
    state_file = _fake(env, monkeypatch, gets=["working"])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: PASS")
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    # Complete means written and stable across two polls (one sample each), no done/idle needed.
    # At least two: under heavy load a fake-herdr call can time out, which is an
    # unknown sample and correctly costs one more poll.
    assert len([c for c in _calls(state_file) if c[:2] == ["agent", "get"]]) >= 2
    monkeypatch.setattr(dispatch, "_submitted", lambda con, disp: True)
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0) == (0, "success")


@pytest.mark.approved
def test_no_interactive_profile_stays_headless(env, monkeypatch):
    state_file = _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    from office import dispatch, paths
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    d = {**d, "adapter_id": "gemini", "model": "fake-model", "effort": "high"}
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    res = dispatch.launch(run, d, "worker", ddir, cwd=env.repo)
    assert res["launcher"] == "process"
    assert not any(c[:1] == ["agent"] for c in _calls(state_file))


@pytest.mark.approved
def test_reviewer_dispatch_launches_in_herdr_able_to_write_its_reply(env, monkeypatch):
    state_file = _fake(env, monkeypatch, reads=[BUSY])
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    from office import dispatch, paths
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("You are a code reviewer\n")
    out = ddir / "reply.txt"
    for adapter_id, kind in (("claude", "claude"), ("codex", "codex")):
        rd = {**d, "adapter_id": adapter_id, "model": "fake-model", "effort": "high"}
        res = dispatch.launch(run, rd, "reviewer", ddir, cwd=env.repo, output=out, include_dirs=[env.tmp])
        assert res["launcher"] == "herdr", res
        calls = _calls(state_file)
        start = [c for c in calls if c[:2] == ["agent", "start"]][-1]
        assert start[start.index("--kind") + 1] == kind
        args = start[start.index("--") + 1:]
        assert "fake-model" in args and "high" in " ".join(args)
        # R12: a reviewer may write its reply file, and gets that file's directory.
        assert str(out.parent) in args, args
        if kind == "claude":
            assert args[args.index("--disallowedTools") + 1].startswith("Edit,Bash,NotebookEdit,Edit(/")
            # Writes are allowed to the dispatch dir alone; dontAsk denies the rest.
            assert args[args.index("--allowedTools") + 1] == f"Read,Grep,Glob,Edit(/{out.parent}/**)"
            assert args[args.index("--permission-mode") + 1] == "dontAsk"
            assert str(env.tmp) in args
        else:
            assert args[args.index("--sandbox") + 1] == "workspace-write"
        prompt = [c for c in calls if c[:2] == ["agent", "prompt"]][-1]
        assert str(out) in prompt[3] and "office submit" not in prompt[3]
        assert not any(c[-1].startswith("sh ") for c in calls)
    env_text = (ddir / "agent.env").read_text()
    assert "OFFICE_ROLE=reviewer" in env_text and "OFFICE_DISPATCH_ID" not in env_text


def test_reviewer_pane_text_is_never_taken_as_the_reply(env, monkeypatch):
    # R13: the pane shows a verdict but no file was written; nothing is scraped.
    state_file = _fake(env, monkeypatch, gets=["done", "done", "done"])
    data = json.loads(state_file.read_text())
    data["content"] = "VERDICT: PASS"
    state_file.write_text(json.dumps(data))
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    out = env.tmp / "reply.txt"
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    assert not out.exists()


def test_agent_names_are_valid_for_herdr():
    import re
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import dispatch
    for did in ("Dbd54d9db", "D880825EE", "DXYZ_" + "A" * 40):
        name = dispatch.herdr_agent_name(did)
        assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name), name


def launch_in_herdr(env, monkeypatch, *, gets=(), reads=(), adapter="agy", model="gemini-3.8-flash-medium",
                    effort="medium", role="worker", brief="ROLE executor\n", output=False, owned_cwd=False,
                    settings=None, real_watcher=False, **launch_kwargs):
    """Launch the approved run's T1 dispatch through the herdr launcher against the fake herdr.

    `reads` may be a function of the brief path for panes whose content names it.
    `owned_cwd` runs the agent in the dispatch directory (an Office-owned folder) instead of the repo.
    `real_watcher` leaves the detached watcher process real instead of stubbing it.
    Returns state_file, run, d (the dispatch with its route), ddir and the launch result.
    """
    state_file = _fake(env, monkeypatch, gets=gets, reads=() if callable(reads) else reads)
    run, d = _live_dispatch(env, monkeypatch)
    for key, value in {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:pQ", "OFFICE_LAUNCHER": "herdr",
                       "OFFICE_HERDR_LAND_TIMEOUT": "0", "OFFICE_HERDR_KEY_DELAY": "0", **(settings or {})}.items():
        monkeypatch.setenv(key, value)
    from office import db, dispatch, paths
    d = {**d, "adapter_id": adapter, "model": model, "effort": effort, "harness": adapter}
    if real_watcher:  # the watcher is a separate process: it reads the route from the database
        con = env.con()
        try:
            with db.transaction(con):
                con.execute("UPDATE dispatches SET adapter_id=?, model=?, effort=? WHERE id=?", (adapter, model, effort, d["id"]))
        finally:
            con.close()
    else:
        monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text(brief)
    if callable(reads):
        data = json.loads(state_file.read_text())
        data["reads"] = reads(ddir / "brief.md")
        state_file.write_text(json.dumps(data))
    res = dispatch.launch(run, d, role, ddir, cwd=ddir if owned_cwd else env.repo, output=ddir / "reply.txt" if output else None,
                          **launch_kwargs)
    return state_file, run, d, ddir, res


def _launch_events(env, run):
    con = env.con()
    try:
        return [r[0] for r in con.execute("SELECT summary FROM events WHERE run_id=? AND kind='launch'", (run["id"],))]
    finally:
        con.close()


@pytest.mark.approved
def test_unlanded_prompt_is_retried_by_typing_it(env, monkeypatch):
    state_file, run, d, _, res = launch_in_herdr(env, monkeypatch, reads=[*PRE, EMPTY, EMPTY, BUSY])
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    calls = _calls(state_file)
    assert any(c[:2] == ["pane", "send-text"] and "brief.md" in c[3] for c in calls)
    assert any(c[:2] == ["pane", "send-keys"] and c[-1] == "Enter" for c in calls)
    assert _launch_events(env, run) == []


@pytest.mark.approved
def test_prompt_that_never_lands_is_reported_not_relaunched(env, monkeypatch):
    # `working` alone is not proof: codex reports it while a trust dialog holds the composer.
    state_file, run, d, _, res = launch_in_herdr(env, monkeypatch, gets=["working"], reads=["> composer empty"])
    assert res["launcher"] == "herdr" and res["prompt_landed"] is False
    events = _launch_events(env, run)
    assert len(events) == 1 and "did not land" in events[0] and res["agent"] in events[0]


@pytest.mark.approved
def test_failed_agent_start_is_disclosed_before_headless_fallback(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    state_file, run, d, _, res = launch_in_herdr(env, monkeypatch, reads=[BUSY])
    assert res["launcher"] == "process-fallback"
    con = env.con()
    try:
        assert con.execute("SELECT launcher FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0] == "process-fallback"
    finally:
        con.close()
    events = _launch_events(env, run)
    assert len(events) == 1 and "herdr agent start failed" in events[0] and "invalid_agent_name" in events[0]


def test_busy_pane_never_settles_as_done(env, monkeypatch):
    state_file = _fake(env, monkeypatch, gets=["idle", "idle", "idle", "idle", "gone"])
    data = json.loads(state_file.read_text())
    data["content"] = "Running command...\nesc to cancel    Gemini 3.8 Flash"
    state_file.write_text(json.dumps(data))
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    # agy reads idle mid-turn; the busy footer keeps it live until the agent is gone.
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0) == (None, "nonzero")


@pytest.mark.approved
def test_external_dispatch_writes_agent_env_for_a_manual_launch(env, monkeypatch):
    _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    from office import paths
    env_text = (paths.run_dir(run["id"]) / "dispatches" / d["id"] / "agent.env").read_text()
    assert f"OFFICE_DISPATCH_ID={d['id']}" in env_text and "OFFICE_ROLE=executor" in env_text


@pytest.mark.approved
def test_stable_output_does_not_settle_while_the_pane_is_busy(env, monkeypatch):
    state_file = _fake(env, monkeypatch, gets=["working", "working", "working", "gone"], reads=[BUSY])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: PASS (draft)")
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    # It waited through every busy sample and ended only once the agent was gone.
    assert len([c for c in _calls(state_file) if c[:2] == ["agent", "get"]]) == 4


def test_hung_prompt_call_does_not_escape_the_launch(env, monkeypatch):
    import subprocess as sp
    from office import dispatch
    real = sp.run

    def run(args, *a, **k):
        if list(args[:3]) == ["herdr", "agent", "prompt"]:
            raise sp.TimeoutExpired(args, 30)
        return real(args, *a, **k)

    monkeypatch.setattr(dispatch.subprocess, "run", run)
    state_file, run_, d, _, res = launch_in_herdr(env, monkeypatch, reads=[*PRE, EMPTY, EMPTY, BUSY])
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True and res["watcher_pid"]


@pytest.mark.approved
def test_unreadable_pane_never_settles_stable_output_or_idle(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_READ_FAIL", "1")
    state_file = _fake(env, monkeypatch, gets=["idle", "idle", "idle", "idle", "gone"])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: PASS (draft)")
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    # Unknown busy state held it open until the agent was gone.
    assert len([c for c in _calls(state_file) if c[:2] == ["agent", "get"]]) == 5


def test_pane_still_holding_a_finished_agent_is_not_reused(env, monkeypatch):
    state_file = _fake(env, monkeypatch)
    data = json.loads(state_file.read_text())
    data["pane_agents"] = {"w1:p50": "codex"}  # ended dispatch, session still in the pane
    state_file.write_text(json.dumps(data))
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    tab_file = env.tmp / "herdr-tab.json"
    pane = dispatch._herdr_split_pane(run, env.repo, tab_file, {"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p50"]},
                                      "w1:pQ")
    assert pane != "w1:p50" and pane.startswith("w1:p1")
    assert any(c[:2] == ["pane", "split"] for c in _calls(state_file))
    # A plain shell pane is still reused.
    data = json.loads(state_file.read_text()); data["pane_agents"] = {}; state_file.write_text(json.dumps(data))
    assert dispatch._herdr_split_pane(run, env.repo, tab_file, {"mode": "split", "anchor": "w1:pQ",
                                                                "panes": ["w1:p50"]}, "w1:pQ") == "w1:p50"


@pytest.mark.approved
def test_persistently_unreadable_pane_is_reported_once(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_READ_FAIL", "1")
    monkeypatch.setenv("OFFICE_HERDR_UNKNOWN_LIMIT", "2")
    _fake(env, monkeypatch, gets=["idle", "idle", "idle", "idle", "gone"])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp), poll=0) == (None, "nonzero")
    events = _launch_events(env, run)
    assert len(events) == 1 and "unreadable for 2 polls" in events[0]


@pytest.mark.approved
def test_unreadable_pane_past_the_limit_accepts_stable_output(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_READ_FAIL", "1")
    monkeypatch.setenv("OFFICE_HERDR_UNKNOWN_LIMIT", "3")
    state_file = _fake(env, monkeypatch, gets=["idle"])  # never gone
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: PASS")
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    # The limit is reached on poll 3, which accepts the stable file.
    assert len([c for c in _calls(state_file) if c[:2] == ["agent", "get"]]) == 3
    assert any("unreadable for 3 polls" in e for e in _launch_events(env, run))


@pytest.mark.approved
def test_recovered_busy_pane_is_not_settled_by_the_blind_limit(env, monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_UNKNOWN_LIMIT", "3")
    # Two unreadable samples, then the pane reads busy (resetting the count), then the agent exits.
    state_file = _fake(env, monkeypatch, gets=["working", "working", "working", "gone"], reads=[BUSY])
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    samples = iter([{"status": "unknown", "content_hash": None, "busy": None}] * 2)
    real = dispatch._herdr_agent_sample
    monkeypatch.setattr(dispatch, "_herdr_agent_sample", lambda name: next(samples, None) or real(name))
    out = env.tmp / "reply.txt"
    out.write_text("VERDICT: PASS (draft)")
    assert dispatch.watch_herdr_agent(d["id"], _spec(env.tmp, out), poll=0) == (0, "success")
    # It did not settle on the recovered busy samples; it ended only when the agent was gone.
    assert len([c for c in _calls(state_file) if c[:2] == ["agent", "get"]]) == 4


@pytest.mark.approved
def test_typed_pointer_left_in_the_composer_gets_a_second_enter(env, monkeypatch):
    state_file, run, d, _, res = launch_in_herdr(env, monkeypatch, reads=[*PRE, EMPTY, EMPTY,
                                                                     "> pointer typed, not sent", BUSY])
    assert res["prompt_landed"] is True
    enters = [c for c in _calls(state_file) if c[:2] == ["pane", "send-keys"] and c[-1] == "Enter"]
    assert len(enters) == 2


# A freshly split pane draws its prompt before the shell reads input, and a
# `pane run` sent then is dropped (5 of 5 on zsh with an instant prompt).

@pytest.mark.approved
def test_setup_line_dropped_by_a_fresh_shell_is_cleared_and_sent_again(env, monkeypatch):
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, reads=[BUSY],
                                                    settings={"FAKE_HERDR_SHELL_DROP": "2", "OFFICE_HERDR_SHELL_RETRY": "0"})
    assert res["launcher"] == "herdr", res
    calls = _calls(state_file)
    runs = [i for i, c in enumerate(calls) if c[:2] == ["pane", "run"] and "agent.env" in c[3]]
    assert len(runs) == 3 and (ddir / "shell-ready").exists()
    # Each resend follows a Ctrl-U that clears what the shell left typed (never a SIGINT).
    assert all(calls[i - 1][:2] == ["pane", "send-keys"] and calls[i - 1][-1] == "ctrl+u" for i in runs[1:])
    start = next(i for i, c in enumerate(calls) if c[:2] == ["agent", "start"])
    assert runs[-1] < start


@pytest.mark.approved
def test_shell_that_never_runs_the_setup_line_falls_back_to_headless(env, monkeypatch):
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, reads=[BUSY],
                                                    settings={"FAKE_HERDR_SHELL_DROP": "deaf", "OFFICE_HERDR_SHELL_RETRY": "0",
                                                              "OFFICE_HERDR_SHELL_TIMEOUT": "0.2"})
    assert res["launcher"] == "process-fallback"
    assert not any(c[:2] == ["agent", "start"] for c in _calls(state_file))
    assert any("never ran Office's setup line" in e for e in _launch_events(env, run))
