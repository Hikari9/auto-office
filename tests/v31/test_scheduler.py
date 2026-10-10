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

_PRIORITIES = st.sampled_from(["urgent", "high", "normal", "low", None, "unlisted"])
# Most items are plain, so a slate often holds several that are free to run: the capacity rules need that to show.
_items = st.lists(
    st.fixed_dictionaries({
        "priority": _PRIORITIES, "hours": st.integers(min_value=0, max_value=200), "blocks": st.integers(min_value=0, max_value=3),
        "protected": st.sampled_from([False] * 6 + [True]), "paused": st.sampled_from([False] * 5 + [True]),
        "demoted_seq": st.sampled_from([None] * 4 + [1, 2, 3]),
        "auto_mode": st.sampled_from(["on"] * 4 + ["paused", "off"]), "provider": st.sampled_from([None, "codex", "claude"]),
    }), max_size=7).map(lambda rows: [item(f"i{n}", **row) for n, row in enumerate(rows)])
# A sample only counts when it was taken (`ok`) and carries a value: an unavailable probe that still reports a number is no pressure.
_sample = st.one_of(st.just({"status": "unavailable", "value": None}), st.just({"status": "unavailable", "value": 0.99}),
                    st.just({"status": "ok", "value": None}), st.just(None), st.just({"status": "ok", "value": 0.9}),
                    st.builds(lambda v: {"status": "ok", "value": v}, st.floats(min_value=0, max_value=1)))
_calm = st.builds(lambda v: {"status": "ok", "value": v}, st.floats(min_value=0, max_value=0.89))
_hosts = st.fixed_dictionaries({"cpu": _sample, "ram": _sample})
_quotas = st.dictionaries(st.sampled_from(["codex", "claude"]),
                          st.builds(lambda r: {"remaining_percent": r}, st.one_of(st.just(5), st.integers(min_value=0, max_value=100))))
_caps = st.one_of(st.none(), st.integers(min_value=0, max_value=4))
_active = st.lists(st.sampled_from(["running", "idle", "paused"]), max_size=4).map(
    lambda states: [{"id": f"a{n}", "state": state} for n, state in enumerate(states)])


def _conf(cap, reserve=5):
    return {"scheduler": {**CONFIG["scheduler"], "max_active_runs": cap}, "quota": {"reserve_percent": reserve}}


def _admitted(res):
    return [e for e in res["entries"] if e["decision"] == "admit"]


def _exhausted(entry, quota, reserve=5):
    return (quota.get(entry.get("provider")) or {}).get("remaining_percent", 100) <= reserve


def _pressured(host):
    return any(h and h["status"] == "ok" and h["value"] is not None and h["value"] >= 0.9 for h in host.values())


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps)
def test_every_item_gets_one_decision_and_a_reason_and_none_is_lost(items, active, host, quota, cap):
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap))
    assert sorted(e["id"] for e in res["entries"]) == sorted(i["id"] for i in items)
    assert all(e["decision"] in ("admit", "hold") and e["reason"] for e in res["entries"])


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=st.integers(min_value=0, max_value=4))
def test_admission_fills_max_active_runs_exactly_and_never_past_it(items, active, host, quota, cap):
    """Of the ordinary work free to run, as much is admitted as the cap leaves room for: no more (safety), no less (liveness).
    Only running agents hold capacity, and the protected orchestrator is outside the cap."""
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap))
    running = sum(1 for a in active if a["state"] == "running")
    pressure = _pressured(host)
    free = [e for e in res["entries"] if not e.get("paused") and not e.get("protected") and e.get("auto_mode", "on") == "on"
            and not pressure and not _exhausted(e, quota)]
    ordinary = [e for e in _admitted(res) if not e.get("protected")]
    assert ordinary == [e for e in ordinary if e in free]
    assert len(ordinary) == min(len(free), max(0, cap - running))


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps)
def test_a_paused_item_is_never_admitted_and_a_protected_one_always_is(items, active, host, quota, cap):
    for e in plan(items, active=active, host=host, quota=quota, config=_conf(cap))["entries"]:
        if e.get("paused"):
            assert e["decision"] == "hold" and e["reason"] == "paused by operator"
        elif e.get("protected"):
            assert e["decision"] == "admit"


@given(items=_items, active=_active, host=_hosts, quota=_quotas, cap=_caps, reserve=st.sampled_from([5, 5, 10, 20]))
def test_measured_pressure_auto_mode_and_exhausted_quota_each_hold_ordinary_work(items, active, host, quota, cap, reserve):
    """The reserve is the configured one: a provider at or below it is held."""
    res = plan(items, active=active, host=host, quota=quota, config=_conf(cap, reserve))
    pressure = _pressured(host)
    for e in res["entries"]:
        if e.get("paused") or e.get("protected"):
            continue
        if pressure or _exhausted(e, quota, reserve) or e.get("auto_mode", "on") != "on":
            assert e["decision"] == "hold", e


@given(items=_items)
def test_unmeasured_host_and_unknown_quota_never_gate_ordinary_work(items):
    """Absent evidence is reported as absent; it does not stop work the operator has not held."""
    res = plan(items, host=UNMEASURED, quota={}, config=_conf(None))
    for e in res["entries"]:
        if not e.get("paused") and e.get("auto_mode", "on") == "on":
            assert e["decision"] == "admit", e
        assert e["quota"] == "unknown"


@pytest.mark.parametrize("host, quota, held_because", [
    ({"cpu": {"status": "ok", "value": 0.95}, "ram": OK}, {}, "cpu pressure"),
    ({"cpu": OK, "ram": {"status": "ok", "value": 0.95}}, {}, "ram pressure"),
    ({"cpu": {"status": "ok", "value": 0.9}, "ram": OK}, {}, "cpu pressure"),                   # at the limit gates
    (HOST_OK, {"codex": {"remaining_percent": 5}}, "provider quota at or below the 5% reserve"),  # at the reserve gates
])
def test_a_gated_item_names_what_gates_it(host, quota, held_because):
    e = by_id(plan([item("a", provider="codex")], host=host, quota=quota))["a"]
    assert e["decision"] == "hold" and held_because in e["reason"]


def test_just_inside_the_limits_admits():
    res = plan([item("a", provider="codex")], host={"cpu": {"status": "ok", "value": 0.89}, "ram": OK},
               quota={"codex": {"remaining_percent": 6}})
    assert by_id(res)["a"]["decision"] == "admit"


def test_a_configured_reserve_replaces_the_default():
    for remaining, decision in ((20, "hold"), (21, "admit")):
        e = by_id(plan([item("a", provider="codex")], quota={"codex": {"remaining_percent": remaining}}, config=_conf(None, 20)))["a"]
        assert e["decision"] == decision, remaining


def test_a_quota_reserve_left_unconfigured_is_five_percent():
    for remaining, decision in ((5, "hold"), (6, "admit")):
        e = by_id(plan([item("a", provider="codex")], quota={"codex": {"remaining_percent": remaining}}, config={}))["a"]
        assert e["decision"] == decision, remaining


def test_a_paused_auto_mode_holds_and_says_so():
    for mode in ("paused", "off"):
        e = by_id(plan([item("a", auto_mode=mode)]))["a"]
        assert e["decision"] == "hold" and e["reason"] == f"auto mode {mode}"


def test_an_item_with_no_or_an_unlisted_priority_scores_as_normal():
    for priority in (None, "unlisted"):
        assert by_id(plan([item("a", priority=priority)]))["a"]["score"]["components"]["priority"] == 20.0


@given(items=_items)
def test_ready_order_puts_paused_last_then_demoted_and_otherwise_the_highest_score_first(items):
    entries = plan(items)["entries"]
    klass = [2 if e.get("paused") else 1 if e.get("demoted_seq") is not None else 0 for e in entries]
    assert klass == sorted(klass)
    plain = [e["score"]["total"] for e in entries if not e.get("paused") and e.get("demoted_seq") is None]
    assert plain == sorted(plain, reverse=True)


@given(items=_items)
def test_within_a_class_the_demoted_run_in_demotion_order_and_equal_scores_run_oldest_first(items):
    """Same score, same class: the one enqueued first goes first. Demoted work keeps the order it was demoted in."""
    flat = {**CONFIG, "scheduler": {**CONFIG["scheduler"], "aging_per_hour": 0.0}}
    entries = plan([{**i, "priority": "normal", "blocks": 0, "protected": False} for i in items], config=flat)["entries"]
    demoted = [e["demoted_seq"] for e in entries if not e.get("paused") and e.get("demoted_seq") is not None]
    assert demoted == sorted(demoted)
    for klass in (lambda e: e.get("paused"), lambda e: not e.get("paused") and e.get("demoted_seq") is None):
        same = [e["enqueued_at"] for e in entries if klass(e) and e.get("demoted_seq") is None]
        assert same == sorted(same)


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
