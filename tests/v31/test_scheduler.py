"""Scheduler policy, pause semantics, `office queue`, and dispatch's scheduler-paused refusal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from hypothesis import given, strategies as st

from conftest import PLAN_TWO, approved_run
from office import db, queuecmd, scheduler

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OK = {"status": "ok", "value": 0.2}
HOST_OK = {"cpu": OK, "ram": OK}
UNMEASURED = {"cpu": {"status": "unavailable", "value": None}, "ram": {"status": "unavailable", "value": None}}
CONFIG = {"scheduler": {"aging_per_hour": 2.0, "cpu_pressure": 0.9, "ram_pressure": 0.9, "max_active_runs": None},
          "quota": {"reserve_percent": 5}}


def item(id_, *, priority="normal", hours=0.0, **kw):
    return {"id": id_, "priority": priority, "enqueued_at": (NOW - timedelta(hours=hours)).isoformat(), **kw}


def plan(items, *, active=(), host=HOST_OK, quota=None, config=CONFIG):
    return scheduler.plan_admission(list(items), list(active), host, quota or {}, config, NOW)


def by_id(result):
    return {e["id"]: e for e in result["entries"]}


# --- policy ---------------------------------------------------------------

def test_score_components_and_order():
    res = plan([item("low", priority="low", hours=1), item("high", priority="high"),
                item("aged", hours=10), item("blocker", blocks=3), item("orch", protected=True)])
    order = [e["id"] for e in res["entries"]]
    assert order == ["orch", "high", "aged", "blocker", "low"]
    comps = by_id(res)["aged"]["score"]["components"]
    assert comps == {"priority": 20.0, "aging": 20.0, "critical_path": 0.0, "protected": 0.0}
    assert by_id(res)["blocker"]["score"]["components"]["critical_path"] == 15.0
    assert by_id(res)["orch"]["reason"] == "protected active orchestrator"
    assert all(e["decision"] == "admit" and e["reason"] for e in res["entries"])


def test_no_recommended_concurrency_without_calibration():
    res = plan([item("a")])
    flat = repr(res)
    assert "recommend" not in flat and "concurrency" not in flat


def test_unmeasured_host_does_not_gate_and_says_so():
    res = plan([item("a")], host=UNMEASURED)
    e = by_id(res)["a"]
    assert e["decision"] == "admit"
    assert "cpu unmeasured; not gating" in e["notes"] and "ram unmeasured; not gating" in e["notes"]
    assert res["host"] == {"cpu": "unmeasured", "ram": "unmeasured"}


def test_quota_gates_only_when_known_and_unknown_is_never_ok():
    res = plan([item("low", provider="codex"), item("fine", provider="claude"), item("anon", provider="agy")],
               quota={"codex": {"remaining_percent": 3}, "claude": {"remaining_percent": 60}})
    e = by_id(res)
    assert e["low"]["decision"] == "hold" and "reserve" in e["low"]["reason"] and e["low"]["quota"] == "exhausted"
    assert e["fine"]["decision"] == "admit" and e["fine"]["quota"] == "ok"
    assert e["anon"]["decision"] == "admit" and e["anon"]["quota"] == "unknown"
    assert "quota unknown; not gating" in e["anon"]["notes"]


def test_idle_and_paused_agents_do_not_hold_capacity():
    config = {**CONFIG, "scheduler": {**CONFIG["scheduler"], "max_active_runs": 2}}
    active = [{"id": "x", "state": "running"}, {"id": "y", "state": "idle"}, {"id": "z", "state": "paused"}]
    res = plan([item("a", priority="high"), item("b")], active=active, config=config)
    assert res["active"] == 1
    assert by_id(res)["a"]["decision"] == "admit"
    assert by_id(res)["b"]["decision"] == "hold" and "max_active_runs 2" in by_id(res)["b"]["reason"]


def test_paused_work_goes_to_the_end_and_is_held():
    res = plan([item("p", priority="urgent", paused=True, demoted_seq=1), item("d", priority="high", demoted_seq=2),
                item("n", priority="low")])
    assert [e["id"] for e in res["entries"]] == ["n", "d", "p"]
    assert by_id(res)["p"]["decision"] == "hold" and by_id(res)["p"]["reason"] == "paused by operator"


# --- admission properties ---------------------------------------------------

_PRIORITIES = st.sampled_from(["urgent", "high", "normal", "low", None])
_items = st.lists(
    st.fixed_dictionaries({
        "priority": _PRIORITIES, "hours": st.integers(min_value=0, max_value=200), "blocks": st.integers(min_value=0, max_value=3),
        "protected": st.booleans(), "paused": st.booleans(), "demoted_seq": st.one_of(st.none(), st.integers(min_value=1, max_value=5)),
        "auto_mode": st.sampled_from(["on", "paused", "off"]), "provider": st.sampled_from([None, "codex", "claude"]),
    }), max_size=7).map(lambda rows: [item(f"i{n}", **{**row}) for n, row in enumerate(rows)])
_sample = st.one_of(st.just({"status": "unavailable", "value": None}),
                    st.builds(lambda v: {"status": "ok", "value": v}, st.floats(min_value=0, max_value=1)))
_hosts = st.fixed_dictionaries({"cpu": _sample, "ram": _sample})
_quotas = st.dictionaries(st.sampled_from(["codex", "claude"]),
                          st.builds(lambda r: {"remaining_percent": r}, st.integers(min_value=0, max_value=100)))
_caps = st.one_of(st.none(), st.integers(min_value=0, max_value=4))
_active = st.lists(st.sampled_from(["running", "idle", "paused"]), max_size=4).map(
    lambda states: [{"id": f"a{n}", "state": state} for n, state in enumerate(states)])


def _conf(cap):
    return {"scheduler": {**CONFIG["scheduler"], "max_active_runs": cap}, "quota": {"reserve_percent": 5}}


def _admitted(res):
    return [e for e in res["entries"] if e["decision"] == "admit"]


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps)
def test_every_item_gets_one_decision_and_a_reason_and_none_is_lost(items, active, host, quota, cap):
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap))
    assert sorted(e["id"] for e in res["entries"]) == sorted(i["id"] for i in items)
    assert all(e["decision"] in ("admit", "hold") and e["reason"] for e in res["entries"])


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=st.integers(min_value=0, max_value=4))
def test_admission_never_exceeds_max_active_runs_except_for_the_protected_orchestrator(items, active, host, quota, cap):
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap))
    running = sum(1 for a in active if a["state"] == "running")
    ordinary = [e for e in _admitted(res) if not e.get("protected")]
    assert len(ordinary) <= max(0, cap - running)


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps)
def test_a_paused_item_is_never_admitted_and_a_protected_one_always_is(items, active, host, quota, cap):
    for e in plan(items, active=active, host=host, quota=quota, config=_conf(cap))["entries"]:
        if e.get("paused"):
            assert e["decision"] == "hold" and e["reason"] == "paused by operator"
        elif e.get("protected"):
            assert e["decision"] == "admit"


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps)
def test_measured_pressure_auto_mode_and_exhausted_quota_each_hold_ordinary_work(items, active, host, quota, cap):
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap))
    pressure = any(h["status"] == "ok" and h["value"] >= 0.9 for h in host.values())
    for e in res["entries"]:
        if e.get("paused") or e.get("protected"):
            continue
        exhausted = (quota.get(e.get("provider")) or {}).get("remaining_percent", 100) <= 5
        if pressure or exhausted or e.get("auto_mode", "on") != "on":
            assert e["decision"] == "hold", e


@given(items=_items)
def test_unmeasured_host_and_unknown_quota_never_gate_ordinary_work(items):
    """Absent evidence is reported as absent; it does not stop work the operator has not held."""
    res = plan(items, host=UNMEASURED, quota={}, config=_conf(None))
    for e in res["entries"]:
        if not e.get("paused") and e.get("auto_mode", "on") == "on":
            assert e["decision"] == "admit", e
        assert e["quota"] == "unknown"


@given(items=_items)
def test_ready_order_puts_paused_last_then_demoted_and_otherwise_the_highest_score_first(items):
    entries = plan(items)["entries"]
    klass = [2 if e.get("paused") else 1 if e.get("demoted_seq") is not None else 0 for e in entries]
    assert klass == sorted(klass)
    plain = [e["score"]["total"] for e in entries if not e.get("paused") and e.get("demoted_seq") is None]
    assert plain == sorted(plain, reverse=True)


@given(priority=_PRIORITIES, hours=st.integers(min_value=0, max_value=100), older_by=st.integers(min_value=1, max_value=100))
def test_an_older_item_never_ranks_behind_an_otherwise_identical_younger_one(priority, hours, older_by):
    res = plan([item("young", priority=priority, hours=hours), item("old", priority=priority, hours=hours + older_by)])
    assert [e["id"] for e in res["entries"]] == ["old", "young"]


@given(items=_items, host=_hosts)
def test_planning_leaves_its_inputs_alone(items, host):
    import copy
    before = copy.deepcopy((items, host))
    plan(items, host=host)
    assert (items, host) == before


# --- pause semantics against runs.db ----------------------------------------

@pytest.fixture
def two_task_run(env):
    approved_run(env, plan=PLAN_TWO)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC").fetchone()[0]
    yield env, con, run_id
    con.close()


def _ready_ids(con):
    return [e["id"] for e in queuecmd.list_(con, host=HOST_OK, config=CONFIG, now=NOW).data["entries"]]


def test_pausing_a_task_demotes_it_and_last_runnable_pauses_the_run(two_task_run):
    env, con, run_id = two_task_run
    queuecmd.set_priority(con, None, "high", run_arg=run_id, task="T1")
    queuecmd.set_priority(con, None, "low", run_arg=run_id, task="T2")
    res = queuecmd.pause(con, run_arg=run_id, task="T1")
    assert res.data["auto_paused"] is None
    assert queuecmd.auto_mode(con, run_id) == "on"
    order = _ready_ids(con)
    assert order.index(f"task:{run_id}:T2") < order.index(f"task:{run_id}:T1") == len(order) - 1

    res = queuecmd.pause(con, run_arg=run_id, task="T2")
    assert res.data["auto_paused"] == "run"
    assert queuecmd.auto_mode(con, run_id) == "paused"
    assert queuecmd.auto_mode(con) == "on"  # the global switch is untouched
    kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE run_id=? AND kind LIKE 'queue.%'", (run_id,))]
    assert kinds.count("queue.paused") == 2

    # Resuming one task is the explicit command that restores the run's auto mode.
    queuecmd.resume(con, run_arg=run_id, task="T1")
    assert queuecmd.auto_mode(con, run_id) == "on"
    assert "queue.resumed" in [r[0] for r in con.execute("SELECT kind FROM events WHERE run_id=?", (run_id,))]


def test_auto_mode_stays_paused_until_explicit_resume(two_task_run):
    env, con, run_id = two_task_run
    queuecmd.pause(con, run_arg=run_id)
    assert queuecmd.auto_mode(con, run_id) == "paused"
    queuecmd.set_priority(con, None, "high", run_arg=run_id, task="T1")
    queuecmd.demote(con, None, run_arg=run_id, task="T2")
    assert queuecmd.auto_mode(con, run_id) == "paused"
    queuecmd.auto(con, "on", run_arg=run_id)
    assert queuecmd.auto_mode(con, run_id) == "on"


def test_pausing_the_last_queued_issue_pauses_global_auto(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    try:
        a = queuecmd.add(con, "#1").data["item"]["id"]
        b = queuecmd.add(con, "#2").data["item"]["id"]
        assert queuecmd.pause(con, item=a).data["auto_paused"] is None
        assert queuecmd.auto_mode(con) == "on"
        assert queuecmd.pause(con, item=b).data["auto_paused"] == "global"
        assert queuecmd.auto_mode(con) == "paused"
        assert queuecmd.auto_mode(con, "any-run") == "paused"  # global paused overrides runs
        queuecmd.resume(con, item=a)
        assert queuecmd.auto_mode(con) == "on"
    finally:
        con.close()


def test_resume_does_not_override_an_operator_auto_off(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    try:
        a = queuecmd.add(con, "#1").data["item"]["id"]
        queuecmd.auto(con, "off")
        assert queuecmd.pause(con, item=a).data["auto_paused"] is None
        assert queuecmd.auto_mode(con) == "off"
        queuecmd.resume(con, item=a)
        assert queuecmd.auto_mode(con) == "off"
    finally:
        con.close()


def test_add_is_idempotent_by_command_id(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    try:
        first = queuecmd.add(con, "#7", command_id="cmd-7")
        again = queuecmd.add(con, "#7", command_id="cmd-7")
        assert again.data["receipt"]["replayed"] is True
        assert first.data["item"]["id"] == again.data["item"]["id"]
        assert con.execute("SELECT COUNT(*) FROM sched_items").fetchone()[0] == 1
        assert first.data["receipt"]["status"] == "completed"
    finally:
        con.close()


# --- CLI and dispatch ---------------------------------------------------------

def test_queue_cli_projects_a_terminal_started_run_and_pauses_it(two_task_run):
    env, con, run_id = two_task_run
    code, out = env.ojson("queue", "list")
    assert code == 0, out
    projected = [e for e in out["data"]["entries"] if e.get("projected")]
    assert [e["id"] for e in projected] == [f"run:{run_id}"]
    assert projected[0]["decision"] == "admit" and projected[0]["reason"] == "protected active orchestrator"
    assert "recommend" not in repr(out)

    env.office("queue", "pause", "--run", run_id[:8], check=0)
    code, out = env.ojson("queue", "list")
    entry = {e["id"]: e for e in out["data"]["entries"]}[f"run:{run_id}"]
    assert not entry.get("projected") and entry["decision"] == "hold"
    code, auto = env.ojson("queue", "auto", "status", "--run", run_id[:8])
    assert auto["data"]["auto_mode"] == "paused"

    code, out = env.office("dispatch", "T1")
    assert code == 4, out
    assert "scheduler-paused" in out and f"office queue resume --run {run_id[:8]}" in out
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE run_id=? AND task_id='T1'", (run_id,)).fetchone()[0] == 0

    env.office("queue", "resume", "--run", run_id[:8], check=0)
    code, auto = env.ojson("queue", "auto", "--run", run_id[:8])
    assert auto["data"]["auto_mode"] == "on"


def test_dispatch_refuses_a_paused_task_only(two_task_run):
    env, con, run_id = two_task_run
    env.office("queue", "pause", "--run", run_id[:8], "--task", "T2", check=0)
    code, out = env.office("dispatch", "T2")
    assert code == 4 and "scheduler-paused" in out
    assert f"office queue resume --run {run_id[:8]} --task T2" in out
    assert queuecmd.paused_block(con, run_id, "T1") is None


def test_queue_cli_add_priority_demote(env):
    code, out = env.ojson("queue", "add", "#42", "--title", "fix it", "--priority", "high", "--command-id", "k1")
    assert code == 0, out
    item_id = out["data"]["item"]["id"]
    env.office("queue", "priority", item_id, "urgent", check=0)
    env.office("queue", "demote", item_id, check=0)
    con = db.connect()
    try:
        row = dict(con.execute("SELECT * FROM sched_items WHERE id=?", (item_id,)).fetchone())
    finally:
        con.close()
    assert row["priority"] == "urgent" and row["demoted_seq"] is not None and row["title"] == "fix it"
    code, out = env.office("queue", "priority", item_id, "sky-high")
    assert code != 0


def test_scheduler_and_intake_config_defaults_and_settable(env):
    from office import config as cfg, configcmd
    default = cfg.load_yaml(cfg.default_config_path())
    assert default["scheduler"]["auto_mode"] is True and default["scheduler"]["max_active_runs"] is None
    assert default["scheduler"]["orchestrator_route"] == "claude"
    assert default["intake"] == {"queue_issues": False, "authorization": "preview", "ready_to_land": False}
    configcmd.config(key="scheduler.max_active_runs", value="3", tier="user", cwd=env.repo)
    configcmd.config(key="intake.authorization", value="merge", tier="user", cwd=env.repo)
    effective, _ = cfg.resolve(None)
    assert effective["scheduler"]["max_active_runs"] == 3 and effective["intake"]["authorization"] == "merge"
    assert scheduler.settings(effective)["max_active_runs"] == 3
    with pytest.raises(Exception):
        configcmd.config(key="scheduler.cpu_pressure", value="high", tier="user", cwd=env.repo)
    assert yaml.safe_load(cfg.default_config_path().read_text())["scheduler"]["cpu_pressure"] == 0.9
