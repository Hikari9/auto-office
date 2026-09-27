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
elif args[:2] == ["pane", "get"]:
    result = {{}} if args[2] in data["closed"] else {{"pane": {{"pane_id": args[2]}}}}
elif args[:2] == ["tab", "get"]:
    result = {{"tab": {{"tab_id": args[2]}}}}
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
    pane = dispatch._herdr_pane(run, env.repo, label="office plan_reviewer D1")
    assert pane == "w1:p101"
    calls = _calls(state)
    assert ["tab", "create"] not in [c[:2] for c in calls]
    split = next(c for c in calls if c[:2] == ["pane", "split"])
    assert split[split.index("--pane") + 1] == "w1:pQ"
    assert split[split.index("--direction") + 1] == "right"
    assert "--no-focus" in split
    assert ["pane", "rename", "w1:p101", "office plan_reviewer D1"] in calls
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
