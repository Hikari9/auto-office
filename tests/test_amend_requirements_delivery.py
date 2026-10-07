"""A requirements amendment reaches only the tasks it concerns. One that only adds done criteria
changes nothing a running task was told, so it is recorded and authorized but not delivered (no
ack wait); one that drops a criterion is delivered to every live task; naming tasks as the
amendment's scope forces delivery of an additive change to just those."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_V31 = ROOT / "tests" / "v31"
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
QUOTE = ("--quote", "also handle zero")
NEW = "add(0, 0) == 0"


def _v31_conftest():
    if "v31_env" not in sys.modules:
        sys.path.insert(0, str(_V31))
        spec = importlib.util.spec_from_file_location("v31_env", _V31 / "conftest.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["v31_env"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["v31_env"]


@pytest.fixture
def cenv(tmp_path, monkeypatch):
    v = _v31_conftest()
    e = v.Env(tmp_path, monkeypatch, contract=None)
    e.trust_snapshots = {}
    return v._activate(e, monkeypatch)


def _rows(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


@pytest.fixture
def live(cenv):
    """T1 and T2 both dispatched and running."""
    v = _v31_conftest()
    v.approved_run(cenv, plan=v.PLAN_TWO, executor=[{}, {}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    for tid in ("T1", "T2"):
        cenv.office("dispatch", tid, env=EXTERNAL, check=0)
    tasks = _rows(cenv, "SELECT id, status, current_dispatch_id FROM tasks ORDER BY id")
    assert [(t["id"], t["status"]) for t in tasks] == [("T1", "running"), ("T2", "running")], tasks
    return cenv


def _deliveries(env):
    return [(r["task_id"], r["status"]) for r in
            _rows(env, "SELECT task_id, status FROM deliveries ORDER BY task_id")]


def _notifies(env):
    return _rows(env, "SELECT 1 FROM outbox WHERE kind='notify_worker'")


def _criteria(env):
    import json
    return json.loads(_rows(env, "SELECT frozen_json FROM requirements ORDER BY version DESC LIMIT 1")[0]["frozen_json"])[
        "done_criteria"]


def test_an_additive_change_is_recorded_but_not_delivered_to_live_tasks(live):
    before = _rows(live, "SELECT id, status, pause_reason FROM tasks ORDER BY id")
    code, out = live.office("amend", "requirements", *QUOTE, "--add-criterion", NEW, "--", "also handle zero")
    assert code == 0, out
    assert "not delivered to live tasks" in out and "authorization for r3 required" in out, out
    assert _criteria(live)[-1] == NEW
    assert _rows(live, "SELECT requirements_version FROM runs")[0]["requirements_version"] == 3
    assert _deliveries(live) == [] and _notifies(live) == [], "no delivery, so nothing to ack"
    assert _rows(live, "SELECT id, status, pause_reason FROM tasks ORDER BY id") == before, "no task is paused"
    assert [a["class"] for a in _rows(live, "SELECT class FROM amendments")] == ["requirements"]
    events = _rows(live, "SELECT kind, summary FROM events WHERE kind='requirements.changed'")
    assert len(events) == 1 and "not delivered to live tasks" in events[0]["summary"], events


def test_a_dropped_criterion_is_delivered_to_every_live_task(live):
    code, out = live.office("amend", "requirements", *QUOTE, "--drop-criterion", "add and mul",
                            "--add-criterion", NEW, "--", "drop the old criterion")
    assert code == 0, out
    assert "delivered to T1, T2" in out, out
    assert _deliveries(live) == [("T1", "queued"), ("T2", "queued")]


def test_a_change_without_criteria_flags_is_still_delivered_to_every_live_task(live):
    code, out = live.office("amend", "requirements", *QUOTE, "--", "non-goal: no float support")
    assert code == 0, out
    assert _deliveries(live) == [("T1", "queued"), ("T2", "queued")]


def test_naming_tasks_forces_an_additive_change_to_only_those(live):
    code, out = live.office("amend", "T1", "--requirements", *QUOTE, "--add-criterion", NEW, "--", "also handle zero")
    assert code == 0, out
    assert "delivered to T1" in out and "T2" not in out, out
    assert _deliveries(live) == [("T1", "queued")]
    assert _criteria(live)[-1] == NEW


def test_naming_an_unknown_task_is_refused_without_recording_anything(live):
    code, out = live.office("amend", "T9", "--requirements", *QUOTE, "--add-criterion", NEW, "--", "x")
    assert code != 0 and "unknown task" in out, out
    assert _rows(live, "SELECT requirements_version FROM runs")[0]["requirements_version"] == 2
