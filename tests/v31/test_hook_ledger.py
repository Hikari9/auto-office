"""The Stop hook as a safety net over the v3.1 per-run pane ledger.

The runtime closes an accepted dispatch pane itself (snapshot, accepted row,
close, closed_at). If it died after the accepted row, close_finished_panes.mjs
finishes the close, and only then.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "hooks" / "close_finished_panes.mjs"
RUN = "r-hook-1"

FAKE = r'''#!{python}
import json, os, sys
state = os.environ["FAKE_HERDR_STATE"]
data = json.load(open(state))
args = sys.argv[1:]
data.setdefault("calls", []).append(args)
json.dump(data, open(state, "w"))
if data.get("down"):
    sys.exit(1)
if args[:2] == ["pane", "list"]:
    print(json.dumps({{"result": {{"panes": [{{"pane_id": p}} for p in data["panes"]]}}}}))
elif args[:2] == ["agent", "list"]:
    print(json.dumps({{"result": {{"agents": []}}}}))
elif args[:2] == ["pane", "close"]:
    data["panes"] = [p for p in data["panes"] if p != args[2]]
    json.dump(data, open(state, "w"))
    print(json.dumps({{"result": {{"type": "ok"}}}}))
elif args[:2] == ["pane", "get"]:
    if args[2] in data["panes"]:
        print(json.dumps({{"result": {{"pane": {{"pane_id": args[2]}}}}}}))
    else:
        sys.stderr.write(json.dumps({{"error": {{"code": "pane_not_found"}}}}))
        sys.exit(1)
'''


def _node():
    found = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return found[-1] if found else shutil.which("node")


NODE = _node()
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".office" / "active").mkdir(parents=True)
    (repo / ".office" / "active" / RUN).write_text("executing\tgoal\n")
    state_home = tmp_path / "state"
    ddir = state_home / "runs" / RUN
    ddir.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "herdr"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    herdr_state = tmp_path / "herdr.json"

    def run(rows, panes, *, down=False, self_pane="w1:pQ"):
        herdr_state.write_text(json.dumps({"panes": panes, "down": down}))
        (ddir / "panes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
               "OFFICE_STATE_HOME": str(state_home), "FAKE_HERDR_STATE": str(herdr_state),
               "HERDR_PANE_ID": self_pane}
        for k in ("OFFICE_STATE_DIR", "OFFICE_PANE_LEDGER", "OFFICE_RUN_ID", "OFFICE_SESSION_ID", "HERDR_SESSION_ID"):
            env.pop(k, None)
        subprocess.run([NODE, str(HOOK)], cwd=repo, env=env, input="", capture_output=True, text=True, timeout=60)
        data = json.loads(herdr_state.read_text())
        ledger = [json.loads(l) for l in (ddir / "panes.jsonl").read_text().splitlines() if l.strip()]
        return data, ledger

    def snapshot(name="pane-final.txt", text="final pane text\n"):
        p = ddir / name
        p.write_text(text)
        return str(p)

    return run, snapshot


def _launch(pane="w1:p101", dispatch="D1"):
    return {"pane_id": pane, "run_id": RUN, "dispatch_id": dispatch, "role": "executor", "status": "working",
            "closed": False}


def _accepted(snap, pane="w1:p101", dispatch="D1"):
    return {"pane_id": pane, "run_id": RUN, "dispatch_id": dispatch, "result": "accepted", "snapshot": snap}


def _closes(data):
    return [c for c in data["calls"] if c[:2] == ["pane", "close"]]


def test_accepted_with_snapshot_closes_and_records(setup):
    run, snapshot = setup
    data, ledger = run([_launch(), _accepted(snapshot())], ["w1:pQ", "w1:p101"])
    assert _closes(data) == [["pane", "close", "w1:p101"]]
    assert "w1:p101" not in data["panes"]
    assert ledger[-1].get("closed_at") and ledger[-1]["pane_id"] == "w1:p101"


def test_accepted_without_snapshot_stays(setup):
    run, snapshot = setup
    missing = str(Path(snapshot()).with_name("absent.txt"))
    empty = snapshot("empty.txt", "")
    for snap in (None, missing, empty):
        data, _ = run([_launch(), _accepted(snap)], ["w1:p101"])
        assert _closes(data) == [], snap


def test_kept_pane_stays(setup):
    run, snapshot = setup
    rows = [_launch(), _accepted(snapshot()), {"pane_id": "w1:p101", "run_id": RUN, "dispatch_id": "D1",
                                               "kept": True, "reason": "OFFICE_KEEP_PANES"}]
    data, _ = run(rows, ["w1:p101"])
    assert _closes(data) == []


def test_already_closed_pane_stays(setup):
    run, snapshot = setup
    rows = [_launch(), _accepted(snapshot()), {"pane_id": "w1:p101", "run_id": RUN, "dispatch_id": "D1",
                                               "closed_at": "2026-09-30T00:00:00Z"}]
    data, _ = run(rows, ["w1:p101"])
    assert _closes(data) == []


def test_relaunch_on_a_reused_pane_starts_a_fresh_record(setup):
    run, snapshot = setup
    rows = [_launch(), _accepted(snapshot()), _launch(dispatch="D2")]
    data, _ = run(rows, ["w1:p101"])
    assert _closes(data) == []


def test_herdr_outage_closes_nothing(setup):
    run, snapshot = setup
    data, ledger = run([_launch(), _accepted(snapshot())], ["w1:p101"], down=True)
    assert _closes(data) == [] and not any(r.get("closed_at") for r in ledger)


def test_caller_pane_is_never_closed(setup):
    run, snapshot = setup
    rows = [_launch(pane="w1:pQ"), _accepted(snapshot(), pane="w1:pQ")]
    data, _ = run(rows, ["w1:pQ"], self_pane="w1:pQ")
    assert _closes(data) == []


def test_row_without_run_or_dispatch_identity_stays(setup):
    run, snapshot = setup
    rows = [{"pane_id": "w1:p101", "result": "accepted", "snapshot": snapshot()}]
    data, _ = run(rows, ["w1:p101"])
    assert _closes(data) == []


def test_launch_row_alone_never_closes(setup):
    run, _ = setup
    data, _ = run([_launch()], ["w1:p101"])
    assert _closes(data) == []
