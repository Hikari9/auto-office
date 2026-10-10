"""3.6.1 regression: a Herdr pane must never receive another dispatch's brief.

The launch boundary is shared by parallel dispatch and `rerun --resume`.
Tests deliberately make Herdr report another task's cwd, agent, or session.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from office import dispatch, prompting
from office.state import Refused


def _run(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda run_id: tmp_path / run_id)
    return {"id": "r361"}


def _d(task="T1", serial="Da111"):
    return {"id": serial, "task_id": task, "session_id": "session-A", "worktree": "/tmp/office/T1",
            "launcher": "herdr", "pane_id": "w1:p1", "role": "executor"}


@pytest.mark.parametrize("field,actual", [
    ("cwd", "/tmp/office/T2"),
    ("foreground_cwd", "/tmp/office/T2"),
])
def test_other_worktree_refused_even_when_reserved_for_this_dispatch(monkeypatch, tmp_path, field, actual):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {field: actual}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert "pane w1:p1" in mismatch and "T2" in mismatch


def test_subdirectory_of_correct_worktree_is_permitted(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"foreground_cwd": "/tmp/office/T1/src"}})
    assert dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1")) is None


def test_sibling_with_same_prefix_is_not_this_worktree(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1-other"}})
    assert dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))


def test_other_agent_or_resumed_session_cannot_receive_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": {"name": "office-other", "session_id": "session-B"}}})
    assert "runs office-other" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")
    assert "session does not match" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-other")


def test_reserved_for_another_dispatch_refused_before_any_pane_commands(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    dest = tmp_path / run["id"]
    dest.mkdir()
    (dest / "herdr-reservations.json").write_text(json.dumps({"w1:p1": "Db222"}))
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: pytest.fail("setup reached wrong pane"))
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    notes = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notes.append(a[-1]))
    result = dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {},
                                          ([], "claude"), "w1:p1", Path("/tmp/office/T1"), dest)
    assert result is None
    assert any("pane-mismatch" in n for n in notes)


def test_wrong_cwd_after_setup_prevents_agent_start(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"foreground_cwd": "/tmp/office/T2"}})
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: True)
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch_form", lambda *a, **kw: pytest.fail("wrong agent started"))
    notes = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notes.append(a[-1]))
    assert dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {}, ([], "claude"),
                                       "w1:p1", Path("/tmp/office/T1"), tmp_path) is None
    assert "agent and brief were not started" in notes[-1]


def test_manual_prompt_refuses_foreign_pane(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(prompting, "_resolve", lambda *a: _d())
    monkeypatch.setattr(prompting.gates, "_agent_alive", lambda *a: True)
    monkeypatch.setattr(dispatch, "_pane_identity_mismatch", lambda *a, **kw: "wrong worktree")
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("prompt crossed task"))
    with pytest.raises(Refused, match="wrong worktree"):
        prompting.prompt(None, run, "T1", "fix this")


def test_parallel_pane_picks_keep_unique_reservations(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    def pick(_run, _cwd):
        file = tmp_path / run["id"] / "herdr-reservations.json"
        held = json.loads(file.read_text()) if file.exists() else {}
        return "w1:p1" if "w1:p1" not in held else "w1:p2"
    monkeypatch.setattr(dispatch, "_herdr_pick_pane", pick)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda did: dispatch._herdr_pane(run, Path("/tmp/office") / did,
                                                               dispatch_id=did), ["Da111", "Db222"]))
    assert {a, b} == {"w1:p1", "w1:p2"}
    assert set(json.loads((tmp_path / run["id"] / "herdr-reservations.json").read_text()).values()) == {"Da111", "Db222"}


def _mock_started_herdr(monkeypatch, tmp_path, observed):
    """Exercise the actual post-start delivery boundary with no harness process."""
    import subprocess
    calls = {"get": 0}
    def pane_get(_):
        calls["get"] += 1
        # Before `agent start` this is a clean shell. After it, Herdr
        # reports the actual agent identity (correct or cross-assigned).
        return {"pane": {"cwd": "/tmp/office/T1"}} if calls["get"] <= 2 else {"pane": observed}
    monkeypatch.setattr(dispatch, "_herdr_json", pane_get)
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: True)
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch_form", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_pane_agent_group", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_pane_ledger", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "pane_label", lambda *a, **kw: "T1 executor")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "{}", ""))
    class FakeWatcher:
        pid = 1234
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **kw: FakeWatcher())


def test_post_start_cross_task_agent_never_gets_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    _mock_started_herdr(monkeypatch, tmp_path, {
        "foreground_cwd": "/tmp/office/T1", "agent": "office-the-other-task"})
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **kw: pytest.fail("brief sent to wrong agent"))
    notices = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a: notices.append(a[-1]))
    spec = {"kind": "worker", "prompt_file": str(tmp_path / "brief.md"), "images": []}
    out = dispatch._herdr_agent_start(run, _d(), spec, {}, ([], "claude"), "w1:p1",
                                      Path("/tmp/office/T1"), tmp_path)
    assert out["launcher"] == "herdr" and out["prompt_landed"] is False
    assert "identity_failure" in spec and "brief NOT sent" in notices[-1]
    assert spec["pane_evidence"]["reported_agent"] == "office-the-other-task"


def test_post_start_matching_agent_receives_one_brief(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    d = _d()
    agent = dispatch.herdr_agent_name(d["id"])
    _mock_started_herdr(monkeypatch, tmp_path, {"foreground_cwd": "/tmp/office/T1",
                                                "agent": {"name": agent, "session_id": "session-A"}})
    calls = []
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **kw: calls.append(a) or True)
    spec = {"kind": "worker", "prompt_file": str(tmp_path / "brief.md"), "images": []}
    out = dispatch._herdr_agent_start(run, d, spec, {}, ([], "claude"), "w1:p1",
                                      Path("/tmp/office/T1"), tmp_path)
    assert out["prompt_landed"] is True and len(calls) == 1
    assert spec["pane_evidence"]["reported_session"] == "session-A"


def test_rapid_resume_pane_claims_are_fenced_per_dispatch(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    def pick(_run, _cwd):
        f = tmp_path / run["id"] / "herdr-reservations.json"
        held = json.loads(f.read_text()) if f.exists() else {}
        return "w1:p3" if "w1:p3" not in held else "w1:p4"
    monkeypatch.setattr(dispatch, "_herdr_pick_pane", pick)
    resumed = [_d("T3", "Dc333"), _d("T1", "Da111")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        panes = list(pool.map(lambda d: dispatch._herdr_pane(run, Path(d["worktree"]), dispatch_id=d["id"]), resumed))
    assert panes[0] != panes[1]
    held = json.loads((tmp_path / run["id"] / "herdr-reservations.json").read_text())
    assert held[panes[0]] == "Dc333" and held[panes[1]] == "Da111"


@pytest.mark.parametrize("broken", ["{not-json", "[]", '{"w1:p1": 23}'])
def test_corrupt_reservation_ledger_fails_closed(monkeypatch, tmp_path, broken):
    run = _run(monkeypatch, tmp_path)
    root = tmp_path / run["id"]
    root.mkdir()
    (root / "herdr-reservations.json").write_text(broken)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert "reservation evidence" in mismatch


def test_existing_agent_prevents_shell_setup_even_if_cwd_is_correct(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": "office-somebody-else"}})
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a, **kw: pytest.fail("wrong pane shell modified"))
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_launch_notice", lambda *a, **kw: None)
    assert dispatch._herdr_agent_start(run, _d(), {"kind": "worker"}, {},
                                       ([], "claude"), "w1:p1", Path("/tmp/office/T1"), tmp_path) is None


def test_manual_prompt_fences_reassignment_until_delivery_exits(monkeypatch, tmp_path):
    from threading import Event
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(prompting, "_resolve", lambda *a: _d())
    monkeypatch.setattr(prompting.gates, "_agent_alive", lambda *a: True)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    entered, proceed, changed = Event(), Event(), Event()

    def send(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=3)
        raise RuntimeError("finished sending")
    monkeypatch.setattr(dispatch, "submit_prompt", send)

    def move_reservation():
        assert entered.wait(timeout=3)
        with dispatch._pane_lock(run):
            changed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(prompting.prompt, None, run, "T1", "fix this")
        other = pool.submit(move_reservation)
        assert entered.wait(timeout=3)
        assert not changed.wait(timeout=0.05)  # assignment cannot interleave with send
        proceed.set()
        with pytest.raises(RuntimeError, match="finished sending"):
            future.result(timeout=3)
        other.result(timeout=3)
    assert changed.is_set()


def test_inspect_optional_pane_column_in_old_schema():
    import sqlite3
    from office import inspect_cmd
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE dispatches (id TEXT, session_id TEXT, resumed_from TEXT, harness TEXT)")
        assert "NULL AS pane_id" in inspect_cmd._optional_cols(con)
        con.execute("ALTER TABLE dispatches ADD COLUMN pane_id TEXT")
        assert "NULL AS pane_id" not in inspect_cmd._optional_cols(con)
    finally:
        con.close()


def test_empty_reservation_owner_is_not_authority(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    p = tmp_path / run["id"] / "herdr-reservations.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"w1:p1": " "}))
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1"}})
    assert "malformed" in dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))


def test_unidentified_agent_is_not_a_safe_shell(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {
        "cwd": "/tmp/office/T1", "agent": {"session_id": "session-B"}}})
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), check_cwd=False)
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")


def test_reported_control_chars_are_not_rendered(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    control = chr(27) + "[31m"
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/other" + control}})
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1" + control))
    assert control not in mismatch
    held = tmp_path / run["id"] / "herdr-reservations.json"
    held.parent.mkdir(exist_ok=True)
    held.write_text(json.dumps({"w1:p1": "other" + control}))
    mismatch = dispatch._pane_identity_mismatch(run, _d(), "w1:p1", Path("/tmp/office/T1"))
    assert control not in mismatch


def test_empty_agent_object_is_unverified(monkeypatch, tmp_path):
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatch, "_herdr_json", lambda _: {"pane": {"cwd": "/tmp/office/T1", "agent": {}}})
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), check_cwd=False)
    assert "without a verifiable identity" in dispatch._pane_identity_mismatch(
        run, _d(), "w1:p1", Path("/tmp/office/T1"), agent="office-a111")


# ------------------------------------------------------------ the ledger fails closed everywhere

BROKEN_LEDGERS = ["{not-json", "[]", '{"w1:p1": 23}', '{"w1:p1": " "}', "dir"]


def _break_ledger(run, tmp_path, broken):
    root = tmp_path / run["id"]
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "herdr-reservations.json"
    if broken == "dir":
        ledger.mkdir()  # exists, cannot be read as a file
    else:
        ledger.write_text(broken)
    return ledger


@pytest.mark.parametrize("broken", BROKEN_LEDGERS)
def test_unreadable_ledger_refuses_reserve_and_reserved_panes(monkeypatch, tmp_path, broken):
    run = _run(monkeypatch, tmp_path)
    ledger = _break_ledger(run, tmp_path, broken)
    before = None if broken == "dir" else ledger.read_text()
    with pytest.raises(ValueError, match="reservation evidence"):
        dispatch._reserve_pane(run, "w1:p2", "Da111")  # never rewrites from an empty ledger
    with pytest.raises(ValueError, match="reservation evidence"):
        dispatch._reserved_panes(run, {"Da111"})
    assert before is None or ledger.read_text() == before


@pytest.mark.parametrize("broken", BROKEN_LEDGERS)
def test_pane_picks_refuse_before_any_pane_is_touched(monkeypatch, tmp_path, broken):
    run = _run(monkeypatch, tmp_path)
    _break_ledger(run, tmp_path, broken)
    monkeypatch.setattr(dispatch, "_herdr_pick_pane", lambda *a: pytest.fail("picked a pane on a broken ledger"))
    monkeypatch.setattr(dispatch, "_herdr_json", lambda *a: pytest.fail("split a pane on a broken ledger"))
    with pytest.raises(ValueError, match="reservation evidence"):
        dispatch._herdr_pane(run, Path("/tmp/office/T1"), dispatch_id="Da111")
    with pytest.raises(ValueError, match="reservation evidence"):
        dispatch._herdr_fresh_pane(run, Path("/tmp/office/T1"), "w1:p1", dispatch_id="Da111")


@pytest.mark.parametrize("broken", BROKEN_LEDGERS)
def test_abandoned_pane_is_kept_when_the_ledger_cannot_show_it_is_free(env, monkeypatch, broken):
    from office import paths
    run = {"id": "r361-close"}
    paths.run_dir(run["id"]).mkdir(parents=True, exist_ok=True)
    _break_ledger(run, paths.run_dir(run["id"]).parent, broken)
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: pytest.fail("closed a pane the ledger could not clear"))
    assert dispatch._close_abandoned_pane(run, _d(), "w1:p9") is False


def test_launch_with_a_broken_ledger_falls_back_headless_with_the_reason(env, monkeypatch):
    from test_herdr_agent_launch import launch_in_herdr
    from office import paths
    # The run id is only known once the dispatch exists: break the ledger from inside the pane pick.
    real = dispatch._read_reservations

    def broken(run):
        (paths.run_dir(run["id"]) / "herdr-reservations.json").write_text("{not-json")
        return real(run)
    monkeypatch.setattr(dispatch, "_read_reservations", broken)
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, reads=[], adapter="claude", model="m", effort="high")
    assert res["launcher"] in ("process", "process-fallback")
    spec = json.loads((ddir / "launch.json").read_text())
    assert "reservation evidence is unreadable" in spec["fallback_reason"]
    assert not any(c[:2] == ["agent", "start"] for c in json.loads(state_file.read_text())["calls"])


# ------------------------------------------------------------ concurrent launches in one fake Herdr

class FakeHerdr:
    """An in-process Herdr: panes that each hold a cwd and an agent, changed only by what Office does to them."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.panes, self.n, self.log, self.delivered = {}, 0, [], []
        self.busy_once: set[str] = set()   # `agent start` here answers agent_pane_busy once
        self.foreign: dict[str, str] = {}  # pane -> name of an agent that appears there instead of the started one

    def add(self, pane, cwd="/home/shell"):
        self.panes[pane] = {"pane_id": pane, "cwd": cwd}

    def json(self, args):
        import time
        time.sleep(0.005)  # widen every check-then-act window
        with self.lock:
            self.log.append(args)
            if args[:2] == ["pane", "get"]:
                return {"pane": dict(self.panes[args[2]])} if args[2] in self.panes else {}
            if args[:2] == ["pane", "split"]:
                self.n += 1
                self.add(f"w1:p{100 + self.n}")
                return {"pane": {"pane_id": f"w1:p{100 + self.n}"}}
        return {}

    def shell_run(self, pane, command, marker, timeout=None):
        import shlex, time
        time.sleep(0.005)
        with self.lock:
            self.panes[pane]["cwd"] = shlex.split(command.split("&& cd ", 1)[1])[0]
        return True

    def run(self, argv, **kw):
        import subprocess
        out, code = "{}", 0
        with self.lock:
            self.log.append(argv[1:])
            if argv[1:3] == ["agent", "start"]:
                name, pane = argv[3], argv[argv.index("--pane") + 1]
                if pane in self.busy_once:
                    self.busy_once.discard(pane)
                    out, code = '{"error": {"code": "agent_pane_busy"}}', 1
                else:
                    self.panes[pane]["agent"] = {"name": self.foreign.get(pane, name)}
        return subprocess.CompletedProcess(argv, code, out, "")

    def deliver(self, name, pane, pointer, **kw):
        with self.lock:
            self.delivered.append((name, pane, dict(self.panes[pane])))
        return True


@pytest.fixture
def world(monkeypatch, tmp_path):
    import subprocess
    herdr = FakeHerdr()
    run = _run(monkeypatch, tmp_path)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.delenv("OFFICE_HERDR_ANCHOR", raising=False)
    herdr.add("w1:pQ")
    herdr.add("w1:p1")
    herdr.add("w1:p2")
    (tmp_path / run["id"]).mkdir()
    (tmp_path / run["id"] / "herdr-tab.json").write_text(
        json.dumps({"mode": "split", "anchor": "w1:pQ", "panes": ["w1:p1", "w1:p2"]}))
    monkeypatch.setattr(dispatch, "_herdr_json", herdr.json)
    monkeypatch.setattr(dispatch, "_shell_run", herdr.shell_run)
    monkeypatch.setattr(dispatch, "_pane_exists", lambda pane: pane in herdr.panes)
    # Every reservation holder counts as open: the ledger is what keeps parallel launches apart.
    monkeypatch.setattr(dispatch, "_busy_panes", lambda r: set(dispatch._read_reservations(r)))
    monkeypatch.setattr(dispatch.subprocess, "run", herdr.run)
    monkeypatch.setattr(dispatch, "_deliver_prompt", herdr.deliver)
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "_record_launch_form", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_launch", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_record_pane_agent_group", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "_pane_ledger", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch, "pane_label", lambda *a, **kw: "label")
    monkeypatch.setattr(dispatch, "_herdr_rename", lambda *a: None)
    herdr.specs, herdr.notices = [], []
    monkeypatch.setattr(dispatch, "write_launch_spec", lambda r, did, spec: herdr.specs.append((did, dict(spec))))
    monkeypatch.setattr(dispatch, "_launch_notice", lambda r, d, text: herdr.notices.append((d["id"], text)))

    class Watcher:
        pid = 4321
    monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **kw: Watcher())
    herdr.run_ctx = (run, tmp_path)
    return herdr


def _resume_one(world, task, serial):
    """One `office rerun --resume` launch: pick a pane for the dispatch, then start its agent in it."""
    run, tmp_path = world.run_ctx
    d = {**_d(task, serial), "worktree": f"/tmp/office/{task}", "pane_id": None, "resumed_from": "Dold",
         "session_id": f"session-{task}"}
    spec = {"kind": "worker", "prompt_file": str(tmp_path / "brief.md"), "images": [], "resume_findings": "fix it"}
    pane = dispatch._herdr_pane(run, Path(d["worktree"]), dispatch_id=d["id"])
    return d, dispatch._herdr_agent_start(run, d, spec, {}, ([], "claude"), pane, Path(d["worktree"]), tmp_path), spec


def _two_resumes(world):
    """Two rerun --resume launches racing for panes: {task: (dispatch, launch result)}."""
    jobs = {"T1": "Da111", "T3": "Dc333"}
    pool_args = list(jobs.items())
    with ThreadPoolExecutor(max_workers=2) as pool:
        return {t: (d, res) for t, (d, res, _spec) in zip(jobs, pool.map(lambda job: _resume_one(world, *job), pool_args))}


def test_concurrent_pane_picks_never_return_the_same_pane(world):
    run, _ = world.run_ctx
    with ThreadPoolExecutor(max_workers=4) as pool:
        picks = list(pool.map(lambda i: dispatch._herdr_pane(run, Path("/tmp/office/T1"), dispatch_id=f"D{i}"),
                              range(4)))
    assert len(set(picks)) == 4, picks  # two idle panes, then two fresh splits
    held = json.loads((world.run_ctx[1] / run["id"] / "herdr-reservations.json").read_text())
    assert sorted(held) == sorted(picks) and sorted(held.values()) == ["D0", "D1", "D2", "D3"]


def test_back_to_back_resume_launches_end_with_each_pane_matching_its_own_dispatch(world):
    out = _two_resumes(world)
    (d1, r1), (d3, r3) = out["T1"], out["T3"]
    assert r1["pane"] != r3["pane"] and r1["prompt_landed"] and r3["prompt_landed"]
    for d, res in ((d1, r1), (d3, r3)):
        pane = world.panes[res["pane"]]
        assert pane["agent"]["name"] == dispatch.herdr_agent_name(d["id"])
        assert pane["cwd"] == d["worktree"]
    # Each brief went to the pane whose agent and cwd belong to the dispatch it names.
    assert sorted((name, pane, snap["cwd"]) for name, pane, snap in world.delivered) == sorted(
        (dispatch.herdr_agent_name(d["id"]), res["pane"], d["worktree"]) for d, res in ((d1, r1), (d3, r3)))


def test_a_cross_assigned_agent_fails_only_its_own_launch(world):
    # Herdr puts someone else's agent in T3's pane: T3's brief is refused, T1's lands untouched.
    world.foreign = {"w1:p1": "office-someone-else", "w1:p2": "office-someone-else"}
    out = _two_resumes(world)
    outcomes = {t: res["prompt_landed"] for t, (d, res) in out.items()}
    assert outcomes == {"T1": False, "T3": False}  # both panes were poisoned here: neither brief is sent
    assert world.delivered == []
    assert all(spec.get("prompt_landed") is False for _did, spec in world.specs if "prompt_landed" in spec)
    assert {did for did, _ in world.notices} == {"Da111", "Dc333"}  # each failure is attributed to its dispatch
    assert not any(c[:2] == ["pane", "close"] for c in world.log)


def test_one_poisoned_pane_leaves_the_healthy_dispatch_untouched(world):
    world.foreign = {"w1:p2": "office-someone-else"}
    out = _two_resumes(world)
    landed = {t: res["prompt_landed"] for t, (d, res) in out.items()}
    healthy = [t for t, ok in landed.items() if ok]
    refused = [t for t, ok in landed.items() if not ok]
    assert len(healthy) == len(refused) == 1, landed
    (name, pane, snap), = world.delivered
    d_ok = out[healthy[0]][0]
    assert name == dispatch.herdr_agent_name(d_ok["id"]) and snap["cwd"] == d_ok["worktree"]
    assert [did for did, _ in world.notices] == [out[refused[0]][0]["id"]]
    assert "brief NOT sent" in world.notices[0][1]
    final = {did: spec for did, spec in world.specs}
    assert final[out[refused[0]][0]["id"]]["prompt_landed"] is False
    assert final[d_ok["id"]]["prompt_landed"] is True


def test_failed_then_retried_launch_reserves_a_fresh_pane_and_lands_only_after_verification(world):
    run, tmp_path = world.run_ctx
    (tmp_path / "brief.md").write_text("brief")
    world.busy_once = {"w1:p1"}
    d, res, spec = _resume_one(world, "T1", "Da111")
    assert res["pane"] == "w1:p101" and res["prompt_landed"] is True
    held = json.loads((tmp_path / run["id"] / "herdr-reservations.json").read_text())
    assert held["w1:p101"] == "Da111"
    assert [(n, p) for n, p, _ in world.delivered] == [(dispatch.herdr_agent_name("Da111"), "w1:p101")]
    # The spec never says landed until the identity fence passed and the brief was delivered.
    flags = [s.get("prompt_landed") for _did, s in world.specs if "prompt_landed" in s]
    assert flags == [True] and spec["pane_evidence"]["mismatch"] is None
    starts = [i for i, c in enumerate(world.log) if c[:2] == ["agent", "start"]]
    assert len(starts) == 2 and starts[1] > starts[0]


def test_retried_launch_into_a_foreign_agent_never_records_landed(world):
    run, tmp_path = world.run_ctx
    (tmp_path / "brief.md").write_text("brief")
    world.busy_once = {"w1:p1"}
    world.foreign = {"w1:p101": "office-someone-else"}
    d, res, spec = _resume_one(world, "T1", "Da111")
    assert res["prompt_landed"] is False and world.delivered == []
    assert spec["prompt_landed"] is False and "someone-else" in spec["identity_failure"]


# ------------------------------------------------------------ the native nudge uses the same fence

def _nudged(env, monkeypatch, reported, **payload):
    """A running herdr dispatch in pane w1:p7 whose pane Herdr reports as `reported`; returns (result, sends, events)."""
    from test_herdr_agent_launch import _live_dispatch
    from office import state
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p7', status='running', worktree='/tmp/office/T1' "
                "WHERE id=?", (d["id"],))
    con.commit()
    monkeypatch.setattr(dispatch, "_herdr_json", lambda args: {"pane": reported(d)})
    sends = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda target, text, **kw: sends.append((target, text)) or "landed")
    result = dispatch.job_notify_worker(con, state.get_run(con, run["id"]),
                                        {"payload": {"dispatch_id": d["id"], "text": "office status has an update", **payload}})
    events = [r["summary"] for r in con.execute("SELECT summary FROM events WHERE kind='prompt'").fetchall()]
    con.close()
    return result, sends, events


@pytest.mark.approved
@pytest.mark.parametrize("reported", [
    lambda d: {"cwd": "/tmp/office/T2", "agent": {"name": dispatch.herdr_agent_name(d["id"])}},  # another task's worktree
    lambda d: {"cwd": "/tmp/office/T1", "agent": {"name": "office-dsomebodyelse"}},               # another dispatch's agent
    lambda d: {"cwd": "/tmp/office/T1", "agent": {"name": dispatch.herdr_agent_name(d["id"]), "session_id": "other"}},
], ids=["cwd", "agent", "session"])
def test_notify_worker_refuses_a_pane_that_belongs_to_another_dispatch(env, monkeypatch, reported):
    result, sends, events = _nudged(env, monkeypatch, reported)
    assert sends == [] and result["sent"] is False and "refused" in result
    assert len(events) == 1 and "native nudge refused" in events[0]


@pytest.mark.approved
def test_notify_worker_refusal_keeps_an_unblock_amendment_undelivered(env, monkeypatch):
    called = []
    monkeypatch.setattr(dispatch, "_amendment_undelivered", lambda *a: called.append(a))
    from office import gates
    monkeypatch.setattr(gates, "_agent_alive", lambda *a: True)
    result, sends, _ = _nudged(env, monkeypatch, lambda d: {"cwd": "/tmp/office/T2"}, unblock=True,
                               task_id="T1", amendment_id="A1", block_id="B1")
    assert sends == [] and result["sent"] is False and len(called) == 1


@pytest.mark.approved
def test_notify_worker_sends_when_the_pane_is_this_dispatchs_own(env, monkeypatch):
    result, sends, events = _nudged(env, monkeypatch, lambda d: {
        "cwd": "/tmp/office/T1/src", "agent": {"name": dispatch.herdr_agent_name(d["id"])}})
    assert result == {"sent": True, "landed": "landed"} and sends == [("w1:p7", "office status has an update")]
    assert events == []
