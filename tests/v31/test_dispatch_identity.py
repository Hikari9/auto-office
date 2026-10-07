"""Dispatch pane identity: a pane is chosen and reserved for one dispatch in a single step,
Office verifies it before the brief pointer is typed, and `office prompt` names the pane's cwd
and task and refuses another task's pane.

#385/#332: two tasks dispatched in parallel got the same pane (one agent briefed the other).
#328: two resumes back to back reused the same ended pane.

The fake herdr is racy on purpose: a split takes its pane id from a counter it read before a
pause, so two splits at once return the same id, and a pane that has no agent yet looks idle.
Unit-tier tests (no `env`) drive pane selection and the launch checks directly; integration-tier
tests launch two real dispatches against it."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

FAKE_HERDR = r'''#!{python}
import fcntl, json, os, shlex, sys, time
state = os.environ["FAKE_HERDR_STATE"]
delay = float(os.environ.get("FAKE_HERDR_DELAY", "0.2"))
args = sys.argv[1:]
lock = open(state + ".lock", "a")


def txn(fn):
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        data = json.load(open(state)) if os.path.exists(state) else {{"calls": [], "n": 0, "cwd": {{}}, "agents": {{}}}}
        out = fn(data)
        json.dump(data, open(state, "w"))
        return out
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)


txn(lambda d: d["calls"].append(args))
result, code = {{}}, 0
if args[:2] == ["pane", "split"]:
    cwd = args[args.index("--cwd") + 1]
    n = txn(lambda d: d["n"])
    time.sleep(delay)  # the id comes from the counter read before this pause: two splits at once agree on it
    pane = "w1:p%d" % (101 + n)
    def put(d):
        d["n"] = max(d["n"], n + 1)
        d["cwd"][pane] = cwd
    txn(put)
    result = {{"pane": {{"pane_id": pane, "tab_id": "w1:t1"}}}}
elif args[:2] == ["pane", "get"]:
    time.sleep(delay / 2)
    d = txn(lambda d: d)
    if args[2] in d["cwd"]:
        result = {{"pane": {{"pane_id": args[2], "agent": d["agents"].get(args[2]),
                            "cwd": os.environ.get("FAKE_HERDR_CWD_LIE") or d["cwd"][args[2]]}}}}
elif args[:2] == ["tab", "create"]:
    cwd = args[args.index("--cwd") + 1]
    n = txn(lambda d: d["n"])
    time.sleep(delay)  # racy like a split: two creates at once agree on the root pane
    pane = "w1:p%d" % (101 + n)
    def make(d):
        d["n"] = max(d["n"], n + 1)
        d["cwd"][pane] = cwd
    txn(make)
    result = {{"tab": {{"tab_id": "w1:t9"}}, "root_pane": {{"pane_id": pane}}}}
elif args[:2] == ["tab", "get"]:
    result = {{"tab": {{"tab_id": args[2]}}}}
elif args[:2] == ["pane", "run"]:
    words = shlex.split(args[3])
    if "cd" in words:
        txn(lambda d: d["cwd"].__setitem__(args[2], words[words.index("cd") + 1]))
    if " && touch " in args[3]:
        open(words[-1], "w").close()
elif args[:2] == ["agent", "start"] and os.environ.get("FAKE_HERDR_START_FAIL"):
    code, result = 1, {{"error": {{"code": "invalid_agent_name"}}}}
elif args[:2] == ["agent", "start"]:
    pane = args[args.index("--pane") + 1]
    def start(d):
        if d["agents"].get(pane):
            return False
        d["agents"][pane] = args[2]
        return True
    if not txn(start):
        code, result = 1, {{"error": {{"code": "agent_pane_busy"}}}}
elif args[:2] == ["agent", "get"]:
    if args[2] in txn(lambda d: list(d["agents"].values())):
        result = {{"agent": {{"name": args[2], "status": "working"}}}}
    else:
        code = 1
elif args[:2] in (["agent", "read"], ["pane", "read"]):
    print("Working (1s • esc to interrupt)")
    sys.exit(0)
print(json.dumps({{"result": result}}))
sys.exit(code)
'''

TASKS = {"D1": "T1", "D2": "T2", "D3": "T3"}


def install_fake(bin_dir: Path, tmp: Path, monkeypatch, delay: str = "0.2") -> Path:
    herdr = bin_dir / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = tmp / "herdr-state.json"
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    monkeypatch.setenv("FAKE_HERDR_DELAY", delay)
    monkeypatch.setenv("PATH", f"{bin_dir}:" + __import__("os").environ["PATH"])
    for k, v in {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:pQ", "OFFICE_HERDR_LAND_TIMEOUT": "0", "OFFICE_HERDR_KEY_DELAY": "0",
                 "OFFICE_HERDR_SESSION_WAIT": "0", "OFFICE_HERDR_ENTER_WAIT": "0"}.items():
        monkeypatch.setenv(k, v)
    return state


def calls(state: Path) -> list:
    return json.loads(state.read_text())["calls"]


def pane_state(state: Path, pane: str, *, cwd: str | None = None, agent: str | None = None) -> None:
    data = json.loads(state.read_text()) if state.exists() else {"calls": [], "n": 0, "cwd": {}, "agents": {}}
    data["cwd"][pane] = cwd or "/somewhere"
    data["agents"].pop(pane, None) if agent is None else data["agents"].__setitem__(pane, agent)
    data["n"] = max(data["n"], int(pane.rsplit("p", 1)[1]) - 100)
    state.write_text(json.dumps(data))


def together(*jobs):
    """Run the callables at once (released by one barrier); returns their results in order."""
    barrier, results, errors = threading.Barrier(len(jobs)), [None] * len(jobs), []

    def runner(i, job):
        try:
            barrier.wait()
            results[i] = job()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    if errors:
        raise errors[0]
    return results


# ------------------------------------------------------------------ unit tier: no Office env


@pytest.fixture
def light(tmp_path, monkeypatch):
    """A bare state home with a runs.db, a fake herdr on PATH, and one run's directory."""
    for k in ("OFFICE_DATA_HOME", "OFFICE_STATE_HOME"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    (tmp_path / "bin").mkdir()
    state = install_fake(tmp_path / "bin", tmp_path, monkeypatch)
    from office import db, paths
    run = {"id": "r-ident"}
    paths.run_dir(run["id"]).mkdir(parents=True)
    con = db.connect()
    for did, task in TASKS.items():
        wt = tmp_path / "wt" / task
        wt.mkdir(parents=True)
        con.execute("INSERT INTO dispatches(id, run_id, role, task_id, status, worktree, started_at) "
                    "VALUES(?,?,?,?,?,?,?)", (did, run["id"], "executor", task, "launching", str(wt), f"2026-01-0{did[1]}"))
    class Light:
        pass
    light = Light()
    light.run, light.state, light.con, light.tmp = run, state, con, tmp_path
    light.wt = lambda did: tmp_path / "wt" / TASKS[did]
    light.layout = lambda: json.loads((paths.run_dir(run["id"]) / "herdr-tab.json").read_text())
    return light


def test_parallel_dispatches_never_pick_the_same_pane_or_lose_the_layout(light):
    from office import dispatch
    p1, p2 = together(lambda: dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1"),
                      lambda: dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2"))
    assert p1 and p2 and p1 != p2, (p1, p2)
    layout = light.layout()
    assert sorted(layout["panes"]) == sorted([p1, p2]), layout  # neither read-modify-write was lost
    assert layout["reserved"] == {p1: "D1", p2: "D2"}, layout


def test_back_to_back_resumes_never_reuse_one_ended_pane(light):
    from office import dispatch, paths
    pane_state(light.state, "w1:p101")  # an ended dispatch's pane: alive, a shell, no agent in it
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"], "reserved": {"w1:p101": "D3"}}))
    light.con.execute("UPDATE dispatches SET status='exited', ended_at='2026-01-09' WHERE id='D3'")
    p1, p2 = together(lambda: dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1"),
                      lambda: dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2"))
    assert {p1, p2} != {"w1:p101"} and p1 != p2, (p1, p2)
    assert "w1:p101" in (p1, p2)  # one of them still gets the free pane


def test_a_pane_reserved_for_a_launching_dispatch_is_not_handed_out_again(light):
    # No concurrency needed: the dispatch row records its pane only once its agent has started.
    from office import dispatch, paths
    pane_state(light.state, "w1:p101")
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"]}))
    first = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    assert first == "w1:p101"
    second = dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2")
    assert second != first
    assert light.layout()["reserved"] == {first: "D1", second: "D2"}
    # Once D1 ended without an agent in it, the pane is free again.
    light.con.execute("UPDATE dispatches SET status='exited', ended_at='2026-01-09' WHERE id='D1'")
    assert dispatch._herdr_pane(light.run, light.wt("D3"), dispatch_id="D3") == first
    assert light.layout()["reserved"][first] == "D3"


def test_a_failed_launch_releases_its_pane(light):
    from office import dispatch
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    dispatch._release_panes(light.run, "D1")
    assert light.layout()["reserved"] == {}
    assert dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2") == pane


def test_a_retried_launch_takes_back_its_own_reserved_pane(light):
    # A launch that died after reserving its pane (job retried) is not blocked by its own reservation.
    from office import dispatch, paths
    pane_state(light.state, "w1:p101")
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"], "reserved": {"w1:p101": "D1"}}))
    assert dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1") == "w1:p101"
    assert dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2") != "w1:p101"
    assert light.layout()["reserved"]["w1:p101"] == "D1" and len(light.layout()["reserved"]) == 2


def test_a_dispatch_holds_one_pane_at_a_time(light):
    from office import dispatch
    layout = {"reserved": {"w1:p101": "D1", "w1:p102": "D2"}}
    dispatch._reserve(layout, "w1:p103", "D1")
    assert layout["reserved"] == {"w1:p102": "D2", "w1:p103": "D1"}
    dispatch._reserve(layout, "w1:p102", None)  # a caller naming no dispatch takes the pane over
    assert layout["reserved"] == {"w1:p103": "D1"}


def test_a_release_and_a_reservation_at_once_lose_neither_record(light):
    from office import dispatch
    held = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    _, other = together(lambda: dispatch._release_panes(light.run, "D1"),
                        lambda: dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2"))
    layout = light.layout()
    assert layout["reserved"] == {other: "D2"} and other in layout["panes"] and held in layout["panes"], layout


def test_printable_collapses_control_characters_and_caps_length():
    from office import dispatch
    assert dispatch.printable("/wt/a\n\x1b[31mFAKE\tline") == "/wt/a [31mFAKE line"
    assert len(dispatch.printable("/x" * 500)) == 200


def _start_stubs(light, monkeypatch):
    """Stub the launch steps around the pane checks; collect the launch notices."""
    import types
    from office import dispatch
    notices, typed = [], []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda run, d, text: notices.append(text))
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **k: Path("/dev/null"))
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **k: typed.append("setup") or True)
    monkeypatch.setattr(dispatch, "_record_launch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_pane_ledger", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_started_session", lambda out: "s1")
    monkeypatch.setattr(dispatch, "_record_session", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "pane_label", lambda *a, **k: "label")
    monkeypatch.setattr(dispatch, "_herdr_rename", lambda *a, **k: None)
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **k: typed.append("pointer") or True)
    return notices, typed


def _start(light, did, pane):
    from office import dispatch
    d = {"id": did, "role": "executor", "task_id": TASKS[did], "kind": "worker", "session_id": "s0", "harness": "claude"}
    ddir = light.tmp / "ddir" / did
    ddir.mkdir(parents=True, exist_ok=True)
    spec = {"kind": "worker", "prompt_file": str(ddir / "brief.md"), "cwd": str(light.wt(did))}
    return dispatch._herdr_agent_start(light.run, d, spec, {}, (["x"], "claude"), pane, light.wt(did), ddir)


def test_a_pane_reserved_for_another_dispatch_is_refused_before_anything_is_typed(light, monkeypatch):
    from office import dispatch
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2")
    assert _start(light, "D1", pane) is None  # D1 was handed D2's pane by a racy chooser
    assert not typed and not any(c[:2] == ["agent", "start"] for c in calls(light.state))
    assert len(notices) == 1 and "D1 (T1)" in notices[0] and "D2 (T2)" in notices[0] and pane in notices[0], notices


def test_a_pane_in_another_tasks_worktree_never_gets_the_brief_pointer(light, monkeypatch):
    from office import dispatch, paths
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    monkeypatch.setenv("FAKE_HERDR_CWD_LIE", str(light.wt("D2")))  # herdr says the pane sits in T2's worktree
    res = _start(light, "D1", pane)
    assert res and res["prompt_landed"] is False
    assert "pointer" not in typed
    assert not any(c[:2] in (["agent", "prompt"], ["pane", "send-text"]) for c in calls(light.state))
    assert len(notices) == 1 and "NOT sent" in notices[0], notices
    assert "D1 (T1)" in notices[0] and "D2 (T2)" in notices[0] and str(light.wt("D2")) in notices[0], notices
    assert json.loads((paths.run_dir(light.run["id"]) / "dispatches" / "D1" / "launch.json").read_text())["prompt_landed"] is False


def test_the_pane_that_is_this_dispatchs_gets_its_pointer(light, monkeypatch):
    from office import dispatch
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    res = _start(light, "D1", pane)
    assert res["prompt_landed"] is True and typed == ["setup", "pointer"] and not notices, (res, typed, notices)


def _prompt_target(light, monkeypatch, did, pane, cwd):
    from office import gates
    from office import dispatch
    light.con.execute("UPDATE dispatches SET launcher='herdr', pane_id=?, status='running' WHERE id=?", (pane, did))
    pane_state(light.state, pane, cwd=str(cwd), agent=dispatch.herdr_agent_name(did))
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, text, **kw: sent.append(text) or "landed")
    return sent


def test_office_prompt_reports_the_panes_cwd_and_task(light, monkeypatch):
    from office import prompting
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", light.wt("D1"))
    res = prompting.prompt(light.con, light.run, "D1", "hello")
    assert sent == ["hello"]
    assert f"cwd {light.wt('D1')}, task T1" in res.lines[0] and "pane w1:p101" in res.lines[0], res.lines


def test_office_prompt_refuses_a_pane_in_another_tasks_worktree(light, monkeypatch):
    from office import prompting
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", light.wt("D2"))
    with pytest.raises(prompting.Refused) as err:
        prompting.prompt(light.con, light.run, "D1", "hello")
    assert err.value.category == "pane-mismatch" and "T2" in err.value.message and "D2" in err.value.message
    assert not sent, "nothing is typed into the other task's pane"


def test_office_prompt_refuses_a_pane_reserved_for_another_dispatch(light, monkeypatch):
    from office import dispatch, paths, prompting
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", light.wt("D1"))
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"], "reserved": {"w1:p101": "D2"}}))
    with pytest.raises(prompting.Refused) as err:
        prompting.prompt(light.con, light.run, "D1", "hello")
    assert err.value.category == "pane-mismatch" and "reserved for dispatch D2" in err.value.message
    assert not sent


def test_office_prompt_never_echoes_a_control_character_in_the_panes_cwd(light, monkeypatch):
    from office import prompting
    nasty = light.wt("D1") / "sub\n\x1b[2Jfake line"
    nasty.mkdir()
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", nasty)
    res = prompting.prompt(light.con, light.run, "D1", "hello")
    assert sent == ["hello"] and "\x1b" not in res.lines[0] and "\n" not in res.lines[0], res.lines


def test_office_prompt_still_prompts_a_pane_herdr_reports_no_cwd_for(light, monkeypatch):
    from office import prompting
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", light.wt("D1"))
    monkeypatch.setenv("FAKE_HERDR_CWD_LIE", "")
    data = json.loads(light.state.read_text())
    del data["cwd"]["w1:p101"]  # herdr no longer describes the pane
    light.state.write_text(json.dumps(data))
    res = prompting.prompt(light.con, light.run, "D1", "hello")
    assert sent == ["hello"] and "cwd not reported by herdr" in res.lines[0]


def _own_tab(monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID")  # no caller pane to split: the run owns a tab


def test_own_tab_mode_reserves_the_root_pane_and_hands_out_no_pane_twice(light, monkeypatch):
    from office import dispatch
    _own_tab(monkeypatch)
    root = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    assert light.layout()["mode"] == "tab" and light.layout()["reserved"] == {root: "D1"}
    second = dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2")
    assert second != root
    assert light.layout()["reserved"] == {root: "D1", second: "D2"} and light.layout()["panes"] == [root, second]
    # D1 ended with nothing left in its pane: the next dispatch reuses it, and that is recorded.
    light.con.execute("UPDATE dispatches SET status='exited', ended_at='2026-01-09' WHERE id='D1'")
    assert dispatch._herdr_pane(light.run, light.wt("D3"), dispatch_id="D3") == root
    assert light.layout()["reserved"] == {root: "D3", second: "D2"}


def test_own_tab_mode_launches_at_once_share_one_tab_and_never_one_pane(light, monkeypatch):
    from office import dispatch
    _own_tab(monkeypatch)
    p1, p2 = together(lambda: dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1"),
                      lambda: dispatch._herdr_pane(light.run, light.wt("D2"), dispatch_id="D2"))
    assert p1 != p2 and sorted(light.layout()["panes"]) == sorted([p1, p2]), (p1, p2, light.layout())
    assert len([c for c in calls(light.state) if c[:2] == ["tab", "create"]]) == 1


def test_a_busy_pane_retry_reserves_the_fresh_pane_in_its_place(light, monkeypatch):
    from office import dispatch, paths
    notices, typed = _start_stubs(light, monkeypatch)
    pane_state(light.state, "w1:p101", cwd=str(light.wt("D1")), agent="someone-elses-finished-session")
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"], "reserved": {"w1:p101": "D1"}}))
    res = _start(light, "D1", "w1:p101")  # herdr refuses it as busy (agent_pane_busy)
    assert res["pane"] != "w1:p101" and res["prompt_landed"] is True and not notices, (res, notices)
    assert light.layout()["reserved"] == {res["pane"]: "D1"}, light.layout()
    assert res["pane"] in light.layout()["panes"] and typed[-1] == "pointer"


def test_fresh_panes_split_at_once_are_distinct_and_both_reserved(light):
    from office import dispatch, paths
    pane_state(light.state, "w1:p101", agent="busy")
    (paths.run_dir(light.run["id"]) / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p101"], "reserved": {"w1:p101": "D1"}}))
    f1, f2 = together(lambda: dispatch._herdr_fresh_pane(light.run, light.wt("D1"), "w1:p101", "D1"),
                      lambda: dispatch._herdr_fresh_pane(light.run, light.wt("D2"), "w1:p101", "D2"))
    assert f1 != f2
    assert light.layout()["reserved"] == {f1: "D1", f2: "D2"} and sorted(light.layout()["panes"]) == sorted(["w1:p101", f1, f2])


def test_a_pane_reserved_for_another_dispatch_after_the_agent_started_is_refused_at_the_pointer(light, monkeypatch):
    from office import dispatch, paths
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")

    def steal(*a, **k):  # the reservation changes between the agent start and the pointer
        tab = paths.run_dir(light.run["id"]) / "herdr-tab.json"
        layout = json.loads(tab.read_text())
        layout["reserved"][pane] = "D2"
        tab.write_text(json.dumps(layout))
    monkeypatch.setattr(dispatch, "_pane_ledger", steal)
    res = _start(light, "D1", pane)
    assert res["prompt_landed"] is False and "pointer" not in typed
    assert len(notices) == 1 and "NOT sent" in notices[0] and "D2 (T2)" in notices[0] and "D1 (T1)" in notices[0], notices


def test_a_pane_herdr_reports_no_cwd_for_still_gets_its_pointer_at_launch(light, monkeypatch):
    from office import dispatch
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    data = json.loads(light.state.read_text())
    del data["cwd"][pane]  # herdr no longer describes the pane
    light.state.write_text(json.dumps(data))
    assert _start(light, "D1", pane)["prompt_landed"] is True and typed[-1] == "pointer" and not notices


def test_a_worktree_sharing_only_a_name_prefix_is_not_inside_it(light, monkeypatch):
    from office import dispatch
    notices, typed = _start_stubs(light, monkeypatch)
    sibling = str(light.wt("D1")) + "0"  # .../T10 beside .../T1
    assert dispatch.cwd_owner(light.con, light.run["id"], sibling) is None
    assert dispatch.cwd_owner(light.con, light.run["id"], str(light.wt("D1") / "sub"))["id"] == "D1"
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    monkeypatch.setenv("FAKE_HERDR_CWD_LIE", sibling)
    assert _start(light, "D1", pane)["prompt_landed"] is False and "pointer" not in typed


def test_office_prompt_allows_a_pane_in_the_same_tasks_worktree_held_by_another_dispatch(light, monkeypatch):
    from office import prompting
    light.con.execute("INSERT INTO dispatches(id, run_id, role, task_id, status, worktree, started_at) "
                      "VALUES('D4','r-ident','executor','T1','exited',?,'2026-01-05')", (str(light.wt("D1")),))
    sent = _prompt_target(light, monkeypatch, "D1", "w1:p101", light.wt("D1"))
    assert "task T1" in prompting.prompt(light.con, light.run, "D1", "hello").lines[0] and sent == ["hello"]


def test_a_reservation_of_a_dispatch_with_no_row_does_not_hold_the_pane(light):
    from office import dispatch
    assert dispatch._reserved_busy({"reserved": {"w1:p9": "D9"}}) == set()
    assert dispatch._reserved_busy({"reserved": {"w1:p9": "D1"}}) == {"w1:p9"}


def test_the_deepest_worktree_owns_a_cwd_inside_nested_worktrees(light):
    from office import dispatch
    inner = light.wt("D1") / "nested"
    light.con.execute("INSERT INTO dispatches(id, run_id, role, task_id, status, worktree, started_at) "
                      "VALUES('D5','r-ident','executor','T5','running',?,'2026-01-01')", (str(inner),))
    assert dispatch.cwd_owner(light.con, light.run["id"], str(inner / "src"))["id"] == "D5"
    assert dispatch.cwd_owner(light.con, light.run["id"], str(light.wt("D1")))["id"] == "D1"


def test_a_pane_in_no_dispatchs_worktree_is_not_blamed_on_the_dispatch_itself(light, monkeypatch):
    from office import dispatch
    notices, typed = _start_stubs(light, monkeypatch)
    pane = dispatch._herdr_pane(light.run, light.wt("D1"), dispatch_id="D1")
    monkeypatch.setenv("FAKE_HERDR_CWD_LIE", "/tmp")
    assert _start(light, "D1", pane)["prompt_landed"] is False
    assert "no dispatch worktree of this run" in notices[0] and "belongs to dispatch D1" not in notices[0], notices


# ------------------------------------------------------------------ integration tier: real launches


def _two_dispatches(env, monkeypatch):
    """T1 and T2 of an approved two-task plan, each with a dispatch row and a worktree directory."""
    from conftest import PLAN_TWO, approved_run
    from office import dispatch, paths, state
    approved_run(env, plan=PLAN_TWO)
    for tid in ("T1", "T2"):
        env.office("dispatch", tid, env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    out = []
    for tid in ("T1", "T2"):
        d = dict(state.get_dispatch(con, state.get_task(con, run["id"], tid)["current_dispatch_id"]))
        d.update(adapter_id="agy", model="gemini-3.8-flash-medium", effort="medium", harness="agy")
        Path(d["worktree"]).mkdir(parents=True, exist_ok=True)
        ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / "brief.md").write_text("ROLE executor\n")
        out.append((d, ddir))
    con.close()
    return run, out


def _launch(run, d, ddir):
    from office import dispatch
    return dispatch.launch(run, d, "worker", ddir, cwd=Path(d["worktree"]))


def test_two_tasks_dispatched_in_parallel_each_get_their_own_pane_and_brief(env, monkeypatch):
    # #385/#332
    state = install_fake(env.bin, env.tmp, monkeypatch)
    run, ((d1, dir1), (d2, dir2)) = _two_dispatches(env, monkeypatch)
    r1, r2 = together(lambda: _launch(run, d1, dir1), lambda: _launch(run, d2, dir2))
    starts = [c[c.index("--pane") + 1] for c in calls(state) if c[:2] == ["agent", "start"]]
    assert len(starts) == len(set(starts)) == 2, starts  # no pane was started twice: no busy-pane retry rescued a shared one
    assert r1["launcher"] == r2["launcher"] == "herdr"
    assert r1["pane"] != r2["pane"], (r1, r2)
    assert r1["prompt_landed"] is True and r2["prompt_landed"] is True
    from office import paths
    layout = json.loads((paths.run_dir(run["id"]) / "herdr-tab.json").read_text())
    assert sorted(layout["panes"]) == sorted([r1["pane"], r2["pane"]]), layout
    assert layout["reserved"] == {r1["pane"]: d1["id"], r2["pane"]: d2["id"]}, layout
    by_agent = {c[2]: c[3] for c in calls(state) if c[:2] == ["agent", "prompt"]}
    assert str(dir1 / "brief.md") in by_agent[r1["agent"]] and str(dir2 / "brief.md") in by_agent[r2["agent"]]
    con = env.con()
    rows = {r["id"]: r["pane_id"] for r in con.execute("SELECT id, pane_id FROM dispatches WHERE launcher='herdr'")}
    assert rows == {d1["id"]: r1["pane"], d2["id"]: r2["pane"]}
    assert [r[0] for r in con.execute("SELECT summary FROM events WHERE kind='launch'")] == []


def test_a_pane_that_herdr_places_in_the_wrong_worktree_is_refused_and_prompts_are_never_typed(env, monkeypatch):
    state = install_fake(env.bin, env.tmp, monkeypatch, delay="0")
    run, ((d1, dir1), (d2, _)) = _two_dispatches(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_CWD_LIE", d2["worktree"])
    res = _launch(run, d1, dir1)
    assert res["launcher"] == "herdr" and res["prompt_landed"] is False
    assert not any(c[:2] in (["agent", "prompt"], ["pane", "send-text"]) for c in calls(state))
    con = env.con()
    notice = [r[0] for r in con.execute("SELECT summary FROM events WHERE kind='launch'")]
    assert len(notice) == 1 and d1["id"] in notice[0] and d2["id"] in notice[0] and "T2" in notice[0], notice


def test_office_prompt_names_the_pane_cwd_and_task_and_refuses_another_tasks_pane(env, monkeypatch):
    state = install_fake(env.bin, env.tmp, monkeypatch, delay="0")
    run, ((d1, dir1), (d2, dir2)) = _two_dispatches(env, monkeypatch)
    r1, r2 = _launch(run, d1, dir1), _launch(run, d2, dir2)
    code, out = env.office("prompt", "T1", "--", "hello T1")
    assert code == 0 and f"pane {r1['pane']}" in out and f"cwd {d1['worktree']}, task T1" in out, out
    # Swap the recorded panes: T1's row now points at T2's pane.
    con = env.con()
    con.execute("UPDATE dispatches SET pane_id=? WHERE id=?", (r2["pane"], d1["id"]))
    n = len([c for c in calls(state) if c[:2] == ["agent", "prompt"]])
    code, out = env.office("prompt", "T1", "--", "hello again")
    assert code != 0 and "reserved for dispatch " + d2["id"] in out, out
    # A run laid out before reservations existed still has the cwd to go by.
    from office import paths
    tab = paths.run_dir(run["id"]) / "herdr-tab.json"
    layout = json.loads(tab.read_text())
    layout.pop("reserved")
    tab.write_text(json.dumps(layout))
    code, out = env.office("prompt", "T1", "--", "hello again")
    assert code != 0 and "belongs to T2" in out and f"dispatch {d2['id']}" in out, out
    assert len([c for c in calls(state) if c[:2] == ["agent", "prompt"]]) == n


def test_inspect_task_shows_a_user_route_change_on_the_dispatch(env):
    # A4 / #301: `office inspect task` names the route a dispatch was changed from.
    from conftest import approved_run
    approved_run(env)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    before = con.execute("SELECT triple FROM dispatches WHERE task_id='T1'").fetchone()[0]
    code, out = env.office("inspect", "task", "T1")
    assert code == 0 and "route changed from" not in out, out
    con.execute("UPDATE dispatches SET triple='codex@1/fake-new@high', override_json=? WHERE task_id='T1'",
                (json.dumps({"by": "user", "declared": True, "triple": "codex@1/fake-new@high",
                             "route_changed_from": before, "route_changed_at": "2026-01-01T00:00:00Z"}),))
    con.commit()
    code, out = env.office("inspect", "task", "T1")
    line = next(l for l in out.splitlines() if l.startswith("dispatch "))
    assert code == 0 and f"| route changed from {before}" in line and "codex@1/fake-new@high" in line, out


def test_a_launch_that_falls_back_to_headless_gives_its_pane_back(env, monkeypatch):
    install_fake(env.bin, env.tmp, monkeypatch, delay="0")
    run, ((d1, dir1), (d2, dir2)) = _two_dispatches(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_START_FAIL", "1")
    assert _launch(run, d1, dir1)["launcher"] == "process-fallback"
    from office import paths
    layout = json.loads((paths.run_dir(run["id"]) / "herdr-tab.json").read_text())
    assert layout["reserved"] == {} and len(layout["panes"]) == 1, layout
    monkeypatch.delenv("FAKE_HERDR_START_FAIL")
    res = _launch(run, d2, dir2)
    assert res["launcher"] == "herdr" and res["pane"] == layout["panes"][0]
