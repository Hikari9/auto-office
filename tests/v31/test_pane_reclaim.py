"""#200: a dispatch pane is snapshotted and closed once its result is accepted,
kept open otherwise, and never mistaken for idle or reused while an agent holds it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import approved_run

FAKE_HERDR = r'''#!{python}
import json, os, shlex, sys
state = os.environ["FAKE_HERDR_STATE"]
data = json.load(open(state))
args = sys.argv[1:]
data["calls"].append(args)
result, code, text = {{}}, 0, None
closed = data.setdefault("closed", [])
if args[:2] == ["pane", "split"]:
    data["n"] += 1
    result = {{"pane": {{"pane_id": "w1:p%d" % (100 + data["n"]), "tab_id": "w1:t1"}}}}
elif args[:2] == ["pane", "get"]:
    if args[2] in closed:
        code, result = 1, None
        print(json.dumps({{"error": {{"code": "pane_not_found"}}}}))
    else:
        result = {{"pane": {{"pane_id": args[2], "agent": data.get("pane_agents", {{}}).get(args[2])}}}}
elif args[:2] == ["pane", "run"] and " && touch " in args[3]:
    open(shlex.split(args[3])[-1], "w").close()  # the shell ran Office's setup line
elif args[:2] == ["pane", "close"]:
    if not data.get("close_fails"):
        closed.append(args[2])
elif args[:2] == ["agent", "start"]:
    busy = data.get("busy_panes", [])
    pane = args[args.index("--pane") + 1]
    if pane in busy:
        code, result = 1, None
        print(json.dumps({{"error": {{"code": "agent_pane_busy", "message": "not an available shell"}}}}))
elif args[:2] == ["agent", "get"]:
    seq = data.setdefault("get", [])
    status = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else "working")
    if status == "gone":
        code = 1
    else:
        result = {{"agent": {{"name": args[2], "status": status,
                             "agent_session": {{"kind": "id", "value": data.get("session")}}}}}}
elif args[1:2] == ["read"]:
    text = data.get("text", "")
if text is not None:
    print(text)
elif result is not None:
    print(json.dumps({{"result": result}}))
json.dump(data, open(state, "w"))
sys.exit(code)
'''


def _fake(env, monkeypatch, **data) -> Path:
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = env.tmp / "herdr-state.json"
    state.write_text(json.dumps({"calls": [], "n": 0, **data}))
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    monkeypatch.setenv("OFFICE_RECLAIM_GRACE", "0")
    monkeypatch.setenv("OFFICE_HERDR_SESSION_WAIT", "0")
    return state


def _data(state: Path) -> dict:
    return json.loads(state.read_text())


def _dispatch(env, *, pane="w1:p7", launcher="herdr", keep=None):
    approved_run(env)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    from office import state
    con = env.con()
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1'").fetchone()[0]
    con.execute("UPDATE dispatches SET launcher=?, pane_id=?, keep_pane=? WHERE id=?", (launcher, pane, keep, did))
    con.commit()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    con.close()
    return run, did


def _ledger(run) -> list[dict]:
    from office import paths
    f = paths.run_dir(run["id"]) / "panes.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.is_file() else []


def _col(env, did, col):
    con = env.con()
    try:
        return con.execute(f"SELECT {col} FROM dispatches WHERE id=?", (did,)).fetchone()[0]
    finally:
        con.close()


@pytest.mark.approved
def test_reclaim_snapshots_then_records_then_closes_and_is_idempotent(env, monkeypatch):
    state_file = _fake(env, monkeypatch, text="VERDICT: PASS\nfinal words")
    run, did = _dispatch(env)
    from office import dispatch, paths
    assert dispatch.reclaim_pane(run, did) == "closed"
    snap = paths.run_dir(run["id"]) / "dispatches" / did / "pane-final.txt"
    assert "final words" in snap.read_text()
    rows = [r for r in _ledger(run) if r.get("dispatch_id") == did]
    kinds = [("accepted" if r.get("result") == "accepted" else "closed" if r.get("closed_at") else "?") for r in rows]
    assert kinds == ["accepted", "closed"] and rows[0]["snapshot"] == str(snap)
    calls = _data(state_file)["calls"]
    close_at = calls.index(["pane", "close", "w1:p7"])
    read_at = max(i for i, c in enumerate(calls) if c[1:2] == ["read"])
    assert read_at < close_at
    assert all(c[2] == "w1:p7" for c in calls if c[:2] == ["pane", "close"])
    assert _col(env, did, "pane_closed_at")
    n = len(_data(state_file)["calls"])
    assert dispatch.reclaim_pane(run, did) == "closed"
    assert len(_data(state_file)["calls"]) == n  # nothing touched twice


@pytest.mark.approved
def test_failed_snapshot_keeps_the_pane_unless_explicit(env, monkeypatch):
    state_file = _fake(env, monkeypatch, text="")
    run, did = _dispatch(env)
    from office import dispatch
    assert dispatch.reclaim_pane(run, did) == "kept"
    assert "w1:p7" not in _data(state_file).get("closed", [])
    assert any(r.get("kept") and r.get("reason") == "snapshot failed" for r in _ledger(run))
    con = env.con()
    notes = [r[0] for r in con.execute("SELECT summary FROM events WHERE kind='launch'")]
    con.close()
    assert any("kept open" in n for n in notes), notes
    assert dispatch.reclaim_pane(run, did, explicit=True) == "closed"
    assert "w1:p7" in _data(state_file)["closed"]


@pytest.mark.approved
def test_keep_panes_keeps_after_snapshot_and_dismiss_still_closes(env, monkeypatch):
    state_file = _fake(env, monkeypatch, text="work log")
    run, did = _dispatch(env, keep=1)
    from office import dispatch, paths
    assert dispatch.reclaim_pane(run, did) == "kept"
    assert (paths.run_dir(run["id"]) / "dispatches" / did / "pane-final.txt").read_text().strip() == "work log"
    assert "w1:p7" not in _data(state_file).get("closed", [])
    assert dispatch.reclaim_pane(run, did, explicit=True) == "closed"


@pytest.mark.approved
def test_non_herdr_or_paneless_dispatch_is_skipped(env, monkeypatch):
    state_file = _fake(env, monkeypatch, text="x")
    run, did = _dispatch(env, launcher="process", pane=None)
    from office import dispatch
    assert dispatch.reclaim_pane(run, did) == "skipped"
    assert _data(state_file)["calls"] == []


@pytest.mark.approved
def test_pane_that_will_not_close_is_kept_not_recorded_closed(env, monkeypatch):
    _fake(env, monkeypatch, text="x", close_fails=True)
    run, did = _dispatch(env)
    from office import dispatch
    assert dispatch.reclaim_pane(run, did) == "kept"
    assert not _col(env, did, "pane_closed_at")


@pytest.mark.approved
def test_accepted_worker_end_closes_its_pane_and_a_failed_end_keeps_it(env, monkeypatch):
    state_file = _fake(env, monkeypatch, text="done")
    run, did = _dispatch(env)
    from office import dispatch
    dispatch._finish(did, 1, None, "nonzero", 1.0)
    assert "w1:p7" not in _data(state_file).get("closed", [])
    # A second dispatch that did submit: its pane closes.
    run2_did = did
    con = env.con()
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL, pane_closed_at=NULL WHERE id=?", (run2_did,))
    con.commit()
    con.close()
    monkeypatch.setattr(dispatch, "_submitted", lambda con, d: True)
    dispatch._finish(run2_did, 0, None, "success", 1.0)
    assert "w1:p7" in _data(state_file)["closed"]


def test_spinner_status_line_counts_as_busy():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import dispatch
    assert dispatch._pane_busy("✽ Harmonizing… (1m 2s)")
    assert dispatch._pane_busy("· Frolicking… (12s · ↓ 1.2k tokens)")
    assert dispatch._pane_busy("Working (1s • esc to interrupt)")
    assert not dispatch._pane_busy("✻ Cooked for 10m 49s · done 1:41 PM\n❯ ")


@pytest.mark.approved
def test_idle_worker_is_never_ended_and_a_held_submit_does_not_end_it(env, monkeypatch):
    _fake(env, monkeypatch, get=["idle"] * 8 + ["gone"], text="❯ ")
    run, did = _dispatch(env)
    from office import dispatch
    spec = {"herdr_agent": "office-x", "output": None, "kind": "worker"}
    # Many settled idle samples: a reviewer would end, a worker waits until it is gone.
    assert dispatch.watch_herdr_agent(did, spec, poll=0) == (None, "nonzero")
    con = env.con()
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, requirements_version, "
                "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "SELECT 'Rheld', run_id, 'T1', 99, id, 'c', 't', 1, 1, 1, 'e', 'op-held', 'amendment_pending', "
                "'2026-01-01' FROM dispatches WHERE id=?", (did,))
    con.commit()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())
    assert dispatch._submitted(con, d) is False
    con.execute("UPDATE revisions SET status='current' WHERE id='Rheld'")
    con.commit()
    assert dispatch._submitted(con, d) is True
    con.close()


@pytest.mark.approved
def test_session_id_is_captured_and_a_busy_pane_gets_a_fresh_split(env, monkeypatch):
    state_file = _fake(env, monkeypatch, session="sess-123", busy_panes=["w1:p101"],
                       text="Working (1s • esc to interrupt)")
    approved_run(env)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    from office import dispatch, paths, state
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    d = state.get_dispatch(con, con.execute("SELECT id FROM dispatches WHERE task_id='T1'").fetchone()[0])
    con.close()
    d = {**d, "adapter_id": "claude", "model": "m", "effort": "high"}
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("ROLE executor\n")
    res = dispatch.launch(run, d, "worker", ddir, cwd=env.repo)
    assert res["launcher"] == "herdr" and res["pane"] == "w1:p102", res
    starts = [c for c in _data(state_file)["calls"] if c[:2] == ["agent", "start"]]
    assert [c[c.index("--pane") + 1] for c in starts] == ["w1:p101", "w1:p102"]
    # claude's session id is assigned at launch; herdr's differing report never overwrites it.
    assigned = starts[-1][starts[-1].index("--session-id") + 1]
    assert _col(env, d["id"], "session_id") == assigned


@pytest.mark.approved
def test_keep_panes_env_is_recorded_at_launch(env, monkeypatch):
    _fake(env, monkeypatch, text="x")
    run, did = _dispatch(env)
    monkeypatch.setenv("OFFICE_KEEP_PANES", "1")
    from office import dispatch
    dispatch._record_launch(run, did, launcher="herdr", pane_id="w1:p7")
    assert _col(env, did, "keep_pane") == 1
