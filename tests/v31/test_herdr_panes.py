"""Herdr dispatch panes: split beside the caller's pane, never a hidden tab."""
from __future__ import annotations

import json
import sys
from pathlib import Path

FAKE_HERDR = r'''#!{python}
import json, os, sys
state = os.environ["FAKE_HERDR_STATE"]
data = json.load(open(state)) if os.path.exists(state) else {{"calls": [], "n": 0, "closed": []}}
args = sys.argv[1:]
data["calls"].append(args)
result = {{}}
if args[:2] == ["pane", "split"]:
    data["n"] += 1
    result = {{"pane": {{"pane_id": "w1:p%d" % (100 + data["n"]), "tab_id": "w1:t1"}}}}
elif args[:2] == ["pane", "get"] and os.environ.get("FAKE_HERDR_GET_DOWN"):
    json.dump(data, open(state, "w"))
    sys.stderr.write("server unavailable")  # transient: not pane_not_found
    sys.exit(1)
elif args[:2] == ["pane", "get"] and args[2] in data["closed"]:
    json.dump(data, open(state, "w"))
    print(json.dumps({{"error": {{"code": "pane_not_found"}}}}))  # what herdr says for a closed pane
    sys.exit(1)
elif args[:2] == ["pane", "get"]:
    result = {{"pane": {{"pane_id": args[2]}}}}
elif args[:2] == ["tab", "get"] and os.environ.get("FAKE_HERDR_TAB_DOWN"):
    json.dump(data, open(state, "w"))
    sys.stderr.write("server unavailable")
    sys.exit(1)
elif args[:2] == ["tab", "get"] and args[2] in data.get("tabs_gone", []):
    json.dump(data, open(state, "w"))
    print(json.dumps({{"error": {{"code": "tab_not_found"}}}}))
    sys.exit(1)
elif args[:2] == ["tab", "get"]:
    result = {{"tab": {{"tab_id": args[2]}}}}
elif args[:2] == ["tab", "close"]:
    data.setdefault("tabs_closed", []).append(args[2])
elif args[:2] == ["tab", "create"]:
    result = {{"tab": {{"tab_id": "w1:t9"}}, "root_pane": {{"pane_id": "w1:p900"}}}}
json.dump(data, open(state, "w"))
print(json.dumps({{"result": result}}))
'''


def _fake(env, monkeypatch) -> Path:
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = env.tmp / "herdr-state.json"
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    return state


def _calls(state: Path) -> list:
    return json.loads(state.read_text())["calls"]


def _run(env) -> dict:
    from office import paths
    run = {"id": "r-herdr-test"}
    paths.run_dir(run["id"]).mkdir(parents=True, exist_ok=True)
    return run


def test_first_dispatch_splits_caller_pane_right_in_current_tab(env, monkeypatch):
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch, paths
    run = _run(env)
    pane = dispatch._herdr_pane(run, env.repo, label="plan plan_reviewer Dabcd")
    assert pane == "w1:p101"
    calls = _calls(state)
    assert ["tab", "create"] not in [c[:2] for c in calls]
    split = next(c for c in calls if c[:2] == ["pane", "split"])
    assert split[split.index("--pane") + 1] == "w1:pQ"
    assert split[split.index("--direction") + 1] == "right"
    assert "--no-focus" in split
    assert ["pane", "rename", "w1:p101", "plan plan_reviewer Dabcd"] in calls
    layout = json.loads((paths.run_dir(run["id"]) / "herdr-tab.json").read_text())
    assert layout["mode"] == "split" and layout["panes"] == ["w1:p101"] and layout["anchor"] == "w1:pQ"


def test_busy_pane_stacks_down_and_idle_pane_is_reused(env, monkeypatch):
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    run = _run(env)
    first = dispatch._herdr_pane(run, env.repo)
    monkeypatch.setattr(dispatch, "_busy_panes", lambda _run: {first})
    second = dispatch._herdr_pane(run, env.repo)
    split = [c for c in _calls(state) if c[:2] == ["pane", "split"]][-1]
    assert split[split.index("--pane") + 1] == first
    assert split[split.index("--direction") + 1] == "down"
    monkeypatch.setattr(dispatch, "_busy_panes", lambda _run: {second})
    assert dispatch._herdr_pane(run, env.repo) == first


def test_close_closes_only_office_panes_not_the_callers_tab(env, monkeypatch):
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    run = _run(env)
    pane = dispatch._herdr_pane(run, env.repo)
    dispatch.close_herdr_tab(run)
    calls = _calls(state)
    assert ["pane", "close", pane] in calls
    assert not any(c[:2] == ["tab", "close"] for c in calls)
    assert not any(c[:2] == ["pane", "close"] and c[2] == "w1:pQ" for c in calls)


def test_no_caller_pane_falls_back_to_own_tab(env, monkeypatch):
    state = _fake(env, monkeypatch)
    from office import dispatch
    run = _run(env)
    assert dispatch._herdr_pane(run, env.repo) == "w1:p900"
    assert any(c[:2] == ["tab", "create"] for c in _calls(state))


def test_legacy_own_tab_run_keeps_its_tab(env, monkeypatch):
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch, paths
    run = _run(env)
    (paths.run_dir(run["id"]) / "herdr-tab.json").write_text(json.dumps({"tab_id": "w1:tD", "panes": ["w1:p18"]}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p18"
    assert not any(c[:2] == ["pane", "split"] for c in _calls(state))
    dispatch.close_herdr_tab(run)
    assert ["tab", "close", "w1:tD"] in _calls(state)


def test_label_task_role_pr_and_short_id(env):
    from office import dispatch
    run = {"id": "r1"}
    d = {"id": "D4f2a91c0", "role": "executor", "task_id": None}
    assert dispatch.pane_label(run, d) == "plan executor D4f2a"
    d["gate_id"] = None
    assert dispatch.pane_label(run, {"id": "D9c1bffff", "role": "plan_reviewer"}) == "plan plan_reviewer D9c1b"


def test_label_with_task_pr_and_integration(env, monkeypatch):
    from office import dispatch, state
    task = {"pr": {"number": 261}}
    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: task if tid == "T3" else None)
    run = {"id": "r1"}
    assert dispatch.pane_label(run, {"id": "D4f2a0000", "role": "executor", "task_id": "T3"}) == "T3 executor PR#261 D4f2a"
    assert dispatch.pane_label(run, {"id": "D9c1b0000", "role": "code_reviewer", "task_id": "T3"}) == "T3 code_reviewer PR#261 D9c1b"
    task["pr"] = {}
    assert dispatch.pane_label(run, {"id": "D4f2a0000", "role": "executor", "task_id": "T3"}) == "T3 executor D4f2a"


def test_integration_label_and_pr(env, monkeypatch):
    from office import dispatch, state
    class Con:
        def execute(self, *a):
            class R:
                def fetchone(self_): return {"subject": "integration"}
            return R()
        def close(self): pass
    monkeypatch.setattr(dispatch.db, "connect", lambda: Con())
    monkeypatch.setattr(state, "get_run", lambda con, rid: {"landing": {"integration": {"pr": 270}}})
    d = {"id": "D7e0c1234", "role": "code_reviewer", "task_id": None, "gate_id": "G1"}
    assert dispatch.pane_label({"id": "r1"}, d) == "integration code_reviewer PR#270 D7e0c"
    monkeypatch.setattr(state, "get_run", lambda con, rid: {"landing": {}})
    assert dispatch.pane_label({"id": "r1"}, d) == "integration code_reviewer D7e0c"


def test_reused_pane_gets_the_new_dispatch_label(env, monkeypatch):
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    run = _run(env)
    first = dispatch._herdr_pane(run, env.repo, label="T1 executor D1111")
    assert dispatch._herdr_pane(run, env.repo, label="T2 executor PR#9 D2222") == first
    renames = [c for c in _calls(state) if c[:2] == ["pane", "rename"]]
    assert renames[-1] == ["pane", "rename", first, "T2 executor PR#9 D2222"]


def test_relabel_task_panes_renames_live_panes_and_swallows_failures(env, monkeypatch):
    state_file = _fake(env, monkeypatch)
    from office import dispatch
    class Con:
        def execute(self, *a):
            class R:
                def fetchall(self_): return [{"id": "D4f2a0000", "role": "executor", "task_id": "T3", "kind": "worker",
                                              "pane_id": "w1:p101"}]
            return R()
        def close(self): pass
    monkeypatch.setattr(dispatch.db, "connect", lambda: Con())
    monkeypatch.setattr(dispatch, "pane_label", lambda run, d, kind="": f"T3 {d['role']} PR#261 {d['id'][:5]}")
    dispatch.relabel_task_panes({"id": "r1"}, "T3")
    assert ["pane", "rename", "w1:p101", "T3 executor PR#261 D4f2a"] in _calls(state_file)
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    dispatch.relabel_task_panes({"id": "r1"}, "T3")  # must not raise


def test_ensure_pr_relabels_when_pr_becomes_known(env, monkeypatch):
    from office import dispatch, prs
    calls = []
    monkeypatch.setattr(dispatch, "relabel_task_panes", lambda run, tid: calls.append(tid))
    monkeypatch.setattr(prs, "body", lambda *a: "b")
    monkeypatch.setattr(prs, "pr_base", lambda *a: "main")
    monkeypatch.setattr(prs, "_find", lambda repo, branch: {"number": 261, "url": "u", "baseRefName": "main"})
    monkeypatch.setattr(prs, "_gh", lambda *a, **k: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(prs.state, "update_task", lambda *a, **k: None)
    monkeypatch.setattr(prs.db, "transaction", lambda con: __import__("contextlib").nullcontext())
    run = {"id": "r1", "repo_root": str(env.repo)}
    prs.ensure_pr(None, run, {"id": "T3", "title": "t"}, {"id": "D1", "branch": "b"})
    assert calls == ["T3"]
    calls.clear()
    prs.ensure_pr(None, run, {"id": "T3", "title": "t", "pr": {"number": 261}}, {"id": "D1", "branch": "b"})
    assert calls == []


def test_rename_failure_never_fails_pane_setup(env, monkeypatch):
    import subprocess
    _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    real = subprocess.run
    for exc in (OSError("no herdr"), subprocess.TimeoutExpired("herdr", 30)):
        def run(args, *a, _exc=exc, **k):
            if args[:3] == ["herdr", "pane", "rename"]:
                raise _exc
            return real(args, *a, **k)
        monkeypatch.setattr(dispatch.subprocess, "run", run)
        run_ = _run(env)
        first = dispatch._herdr_pane(run_, env.repo, label="T1 executor D1111")  # fresh pane
        assert first
        assert dispatch._herdr_pane(run_, env.repo, label="T2 executor D2222") == first  # reused pane


def test_relabel_continues_past_one_failing_pane(env, monkeypatch):
    import subprocess
    from office import dispatch
    rows = [{"id": f"D{i}0000000", "role": "executor", "task_id": "T3", "kind": "worker", "pane_id": f"w1:p{i}"}
            for i in (1, 2, 3)]
    class Con:
        def execute(self, *a):
            class R:
                def fetchall(self_): return rows
            return R()
        def close(self): pass
    monkeypatch.setattr(dispatch.db, "connect", lambda: Con())
    monkeypatch.setattr(dispatch, "pane_label", lambda run, d, kind="": "L " + d["pane_id"])
    done = []
    def run(args, *a, **k):
        if args[3] == "w1:p2":
            raise subprocess.TimeoutExpired("herdr", 30)
        done.append(args[3])
    monkeypatch.setattr(dispatch.subprocess, "run", run)
    dispatch.relabel_task_panes({"id": "r1"}, "T3")
    assert done == ["w1:p1", "w1:p3"]


def test_pane_label_survives_db_connect_failure_during_launch(env, monkeypatch):
    from office import dispatch
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(dispatch.db, "connect", boom)
    d = {"id": "D4f2a0000", "role": "executor", "task_id": "T3"}
    assert dispatch.pane_label({"id": "r1"}, d) == "T3 executor D4f2a"
    assert dispatch.pane_label({"id": "r1"}, {"id": "D9c1b0000", "role": "code_reviewer"}) == "code_reviewer D9c1b"
    dispatch.relabel_task_panes({"id": "r1"}, "T3")  # must not raise either


def test_prs_relabel_hook_failure_is_swallowed(env, monkeypatch):
    from office import dispatch, prs
    monkeypatch.setattr(dispatch, "relabel_task_panes", lambda run, tid: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(prs, "body", lambda *a: "b")
    monkeypatch.setattr(prs, "pr_base", lambda *a: "main")
    monkeypatch.setattr(prs, "_find", lambda repo, branch: {"number": 5, "url": "u", "baseRefName": "main"})
    monkeypatch.setattr(prs, "_gh", lambda *a, **k: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(prs.state, "update_task", lambda *a, **k: None)
    monkeypatch.setattr(prs.db, "transaction", lambda con: __import__("contextlib").nullcontext())
    pr = prs.ensure_pr(None, {"id": "r1", "repo_root": str(env.repo)}, {"id": "T3", "title": "t"}, {"id": "D1", "branch": "b"})
    assert pr["number"] == 5


def test_label_refreshed_when_pr_lands_before_pane_id_is_recorded(env, monkeypatch):
    import types
    import pytest
    from office import dispatch
    calls = []
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **k: Path("/dev/null"))
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **k: True)
    monkeypatch.setattr(dispatch, "_record_launch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_started_session", lambda out: "sess")
    monkeypatch.setattr(dispatch, "_set_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "atomic_write_json", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "pane_label", lambda run, d, kind="": "T3 executor PR#261 D4f2a")

    class Stop(Exception):
        pass

    def stop(*a, **k):
        raise Stop
    monkeypatch.setattr(dispatch, "_pane_ledger", stop)
    monkeypatch.setattr(dispatch.subprocess, "run", lambda args, **k: calls.append(args) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    run = _run(env)
    d = {"id": "D4f2a0000", "role": "executor", "task_id": "T3", "kind": "worker"}
    with pytest.raises(Stop):
        dispatch._herdr_agent_start(run, d, {"kind": "worker"}, {}, (["x"], "claude"), "w1:p101", env.repo,
                                    env.tmp, label="T3 executor D4f2a")
    assert ["herdr", "pane", "rename", "w1:p101", "T3 executor PR#261 D4f2a"] in calls
    calls.clear()
    with pytest.raises(Stop):  # label unchanged: no extra rename
        dispatch._herdr_agent_start(run, d, {"kind": "worker"}, {}, (["x"], "claude"), "w1:p101", env.repo,
                                    env.tmp, label="T3 executor PR#261 D4f2a")
    assert not any(c[:3] == ["herdr", "pane", "rename"] for c in calls)


def test_a_pane_picked_for_a_launching_dispatch_is_not_handed_to_a_parallel_launch(env, monkeypatch):
    # Run f00446ac: a T1 executor and a plan reviewer launched together got the same
    # idle pane; the reviewer's agent took it and T1's setup line never ran.
    from test_herdr_agent_launch import _live_dispatch
    run, d = _live_dispatch(env, monkeypatch)
    _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    first = dispatch._herdr_pane(run, env.repo, dispatch_id=d["id"])
    assert dispatch._herdr_pane(run, env.repo, dispatch_id="Dother") != first
    assert first in dispatch._busy_panes(run)


def test_own_tab_mode_never_splits_from_a_closed_pane(env, monkeypatch):
    # Review F1: own-tab layouts were not filtered for live panes.
    state = _fake(env, monkeypatch)
    state.write_text(json.dumps({"calls": [], "n": 0, "closed": ["w1:p19"]}))
    from office import dispatch, paths
    run = _run(env)
    (paths.run_dir(run["id"]) / "herdr-tab.json").write_text(json.dumps({"tab_id": "w1:tD", "panes": ["w1:p18", "w1:p19"]}))
    monkeypatch.setattr(dispatch, "_busy_panes", lambda _run: {"w1:p18"})
    dispatch._herdr_pane(run, env.repo)
    split = [c for c in _calls(state) if c[:2] == ["pane", "split"]][-1]
    assert split[split.index("--pane") + 1] == "w1:p18", split


def test_a_fresh_pane_after_agent_pane_busy_is_reserved(env, monkeypatch):
    # Review F2: the retry pane must be reserved like a picked one.
    _fake(env, monkeypatch)
    from office import dispatch, paths
    run = _run(env)
    pane = dispatch._herdr_fresh_pane(run, env.repo, "w1:p5", dispatch_id="Dabc")
    held = json.loads((paths.run_dir(run["id"]) / "herdr-reservations.json").read_text())
    assert held.get(pane) == "Dabc", held


def test_the_cosmetic_rename_runs_outside_the_pane_lock(env, monkeypatch):
    # Review F9: a slow rename must not hold every parallel launch.
    import fcntl
    _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch, paths
    run = _run(env)
    free = []

    def rename(pane, label):
        with open(paths.run_dir(run["id"]) / "herdr-tab.lock", "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                free.append(False)
    monkeypatch.setattr(dispatch, "_herdr_rename", rename)
    dispatch._herdr_pane(run, env.repo, label="T1 executor D1")
    assert free == [True]


def test_a_gone_anchor_pane_is_replaced_by_the_callers_pane(env, monkeypatch):
    # The orchestrator's original pane was closed; every split from it failed and each
    # later launch ran headless ("no herdr pane could be opened").
    state = _fake(env, monkeypatch)
    state.write_text(json.dumps({"calls": [], "n": 0, "closed": ["w1:pOld", "w1:p30"]}))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pNew")
    from office import dispatch, paths
    run = _run(env)
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    tab_file.write_text(json.dumps({"mode": "split", "anchor": "w1:pOld", "panes": ["w1:p30"], "tab_id": "w1:t1"}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p101"
    split = [c for c in _calls(state) if c[:2] == ["pane", "split"]][-1]
    assert split[split.index("--pane") + 1] == "w1:pNew", split
    assert json.loads(tab_file.read_text())["anchor"] == "w1:pNew"


def test_a_gone_anchor_without_a_caller_pane_falls_back_to_an_own_tab(env, monkeypatch):
    state = _fake(env, monkeypatch)
    state.write_text(json.dumps({"calls": [], "n": 0, "closed": ["w1:pOld"]}))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    from office import dispatch, paths
    run = _run(env)
    (paths.run_dir(run["id"]) / "herdr-tab.json").write_text(json.dumps({"mode": "split", "anchor": "w1:pOld", "panes": []}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p900"


def test_an_unreachable_herdr_never_replaces_the_split_layout(env, monkeypatch):
    # Review #448 F2: a transient `pane get` failure is not a closed anchor.
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("FAKE_HERDR_GET_DOWN", "1")
    from office import dispatch, paths
    run = _run(env)
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    tab_file.write_text(json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": [], "tab_id": "w1:t1"}))
    dispatch._herdr_pane(run, env.repo)
    assert json.loads(tab_file.read_text())["mode"] == "split"
    assert not any(c[:2] == ["tab", "create"] for c in _calls(state))


def test_an_own_tab_that_outlived_its_panes_is_still_closed_at_the_end(env, monkeypatch):
    # Review #448 F5.
    state = _fake(env, monkeypatch)
    state.write_text(json.dumps({"calls": [], "n": 0, "closed": ["w1:p18"]}))
    from office import dispatch, paths
    run = _run(env)
    (paths.run_dir(run["id"]) / "herdr-tab.json").write_text(json.dumps({"mode": "tab", "tab_id": "w1:tD", "panes": ["w1:p18"]}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p900"
    dispatch.close_herdr_tab(run)
    assert set(json.loads(state.read_text())["tabs_closed"]) == {"w1:tD", "w1:t9"}


def test_reusing_a_pane_runs_no_herdr_command_under_the_lock(env, monkeypatch):
    # Prior #434 F9: the setup line cds; the pick only reads.
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    from office import dispatch
    run = _run(env)
    first = dispatch._herdr_pane(run, env.repo)
    assert dispatch._herdr_pane(run, env.repo) == first
    assert not any(c[:2] == ["pane", "run"] for c in _calls(state))


def test_orphan_tabs_survive_when_the_current_tab_is_gone(env, monkeypatch):
    # R3-12.
    state = _fake(env, monkeypatch)
    state.write_text(json.dumps({"calls": [], "n": 0, "closed": [], "tabs_gone": ["w1:tB"]}))
    from office import dispatch, paths
    run = _run(env)
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    tab_file.write_text(json.dumps({"mode": "tab", "tab_id": "w1:tB", "panes": ["w1:p50"], "orphan_tabs": ["w1:tA"]}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p900"
    assert "w1:tA" in json.loads(tab_file.read_text()).get("orphan_tabs", [])


def test_an_unreachable_herdr_never_drops_the_own_tab(env, monkeypatch):
    # R3-13: a transient `tab get` failure is not "the user closed it".
    state = _fake(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_TAB_DOWN", "1")
    from office import dispatch, paths
    run = _run(env)
    tab_file = paths.run_dir(run["id"]) / "herdr-tab.json"
    tab_file.write_text(json.dumps({"mode": "tab", "tab_id": "w1:tD", "panes": ["w1:p18"]}))
    assert dispatch._herdr_pane(run, env.repo) == "w1:p18"
    assert not any(c[:2] == ["tab", "create"] for c in _calls(state))
    assert json.loads(tab_file.read_text())["tab_id"] == "w1:tD"
