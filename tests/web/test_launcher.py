"""Orchestrator launches: the configured route, policy fallback on known exhausted quota, and held launches."""
import json

import pytest

from office import db
from office.web import server, synthetic
from office.web.service import MACHINE_EVENTS, Command, CommandRefused, orchestrator_route

REPO = "synth-org-0/repo-00"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    yield s
    s.close()


def write(svc, fn):
    con = db.connect(svc.db_path)
    try:
        with db.transaction(con):
            fn(con)
    finally:
        con.close()
    svc.poll()


def cmd(cid, kind, target=None, payload=None, expect=None):
    return Command.parse({"id": cid, "kind": kind, "target": target or {}, "payload": payload or {},
                          "expect": expect or {}})


def refused(svc, c) -> CommandRefused:
    with pytest.raises(CommandRefused) as info:
        svc.submit(c, wait=True)
    return info.value


def runs(svc, **match):
    return [r for r in svc.snapshot()["entities"]["runs"].values() if all(r.get(k) == v for k, v in match.items())]


def live_run(svc):
    return next(r for r in runs(svc, liveness="live") if r["office_version"] == synthetic.CURRENT_VERSION)


def queue_issue_item(svc, item="issue:qf", number=7):
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
        "VALUES(?,'issue',?, 't', 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (item, f"{REPO}#{number}")))


POLICY = {"scheduler": {"orchestrator_route": "claude", "orchestrator_fallbacks": ["codex"]},
          "quota": {"reserve_percent": 5}}


@pytest.mark.parametrize("quota,fallbacks,harness,source", [
    ({}, ["codex"], "claude", None),  # unknown primary quota never falls back
    ({"claude": {"remaining_percent": 50}}, ["codex"], "claude", None),
    ({"claude": {"remaining_percent": 3}, "codex": {"remaining_percent": 60}}, ["codex"], "codex", "claude"),
    ({"claude": {"remaining_percent": 3}}, ["codex"], None, "claude"),  # fallback quota unknown: wait
    ({"claude": {"remaining_percent": 3}, "codex": {"remaining_percent": 2}}, ["codex"], None, "claude"),
    ({"claude": {"remaining_percent": 3}, "codex": {"remaining_percent": 60}}, [], None, "claude"),  # none allowed
    ({"claude": {"remaining_percent": 3}, "agy": {"remaining_percent": 1}, "codex": {"remaining_percent": 60}},
     ["agy", "codex"], "codex", "claude"),
])
def test_orchestrator_route_falls_back_only_on_known_quota(quota, fallbacks, harness, source):
    conf = {**POLICY, "scheduler": {**POLICY["scheduler"], "orchestrator_fallbacks": fallbacks}}
    route = orchestrator_route(conf, quota)
    assert (route["harness"], route["fallback_from"]) == (harness, source)


def test_default_config_allows_no_fallback():
    import yaml
    from office import config as cfg
    default = yaml.safe_load(cfg.default_config_path().read_text(encoding="utf-8"))
    assert default["scheduler"]["orchestrator_fallbacks"] == []


def machine_events(svc):
    return svc.writer(lambda con: [dict(r) for r in con.execute(
        "SELECT kind, payload_json FROM events WHERE run_id=? ORDER BY seq", (MACHINE_EVENTS,))])


def test_queued_launch_falls_back_with_a_durable_receipt_event_and_notice(svc):
    svc.config = lambda: POLICY
    svc.quota_probe = lambda: {"claude": {"remaining_percent": 1}, "codex": {"remaining_percent": 70}}
    queue_issue_item(svc)
    assert svc.admit_queue() == ["issue:qf"]
    receipt = svc.wait("queue-admit:issue:qf")
    assert receipt["status"] == "completed"
    assert receipt["result"]["orchestrator"] == {"harness": "codex", "fallback_from": "claude",
                                                  "reason": "claude quota is at or below the 5% reserve"}
    assert svc.launcher.launches[-1]["harness"] == "codex"
    events = machine_events(svc)
    assert [e["kind"] for e in events] == ["queue.orchestrator_fallback"]
    assert json.loads(events[0]["payload_json"])["command"] == "queue-admit:issue:qf"
    svc.poll(force=True)
    notices = svc.snapshot()["scalars"]["orchestrator_notices"]
    assert notices[0]["kind"] == "queue.orchestrator_fallback" and notices[0]["harness"] == "codex"


def test_queued_launch_waits_when_no_fallback_is_allowed(svc):
    svc.config = lambda: {**POLICY, "scheduler": {"orchestrator_route": "claude"}}
    svc.quota_probe = lambda: {"claude": {"remaining_percent": 1}, "codex": {"remaining_percent": 70}}
    queue_issue_item(svc)
    assert svc.admit_queue() == [] and svc.admit_queue() == []
    assert svc.command("queue-admit:issue:qf") is None and svc.launcher.launches == []
    assert [e["kind"] for e in machine_events(svc)] == ["queue.orchestrator_held"]  # one notice, not one per poll
    assert svc.writer(lambda con: con.execute("SELECT COUNT(*) FROM sched_items WHERE id='issue:qf'").fetchone()[0]) == 1
    svc.quota_probe = lambda: {"claude": {"remaining_percent": 40}}
    assert svc.admit_queue() == ["issue:qf"]
    assert svc.wait("queue-admit:issue:qf")["result"]["orchestrator"]["harness"] == "claude"


def test_unknown_quota_launches_the_configured_route(svc):
    svc.config = lambda: POLICY
    svc.quota_probe = dict
    queue_issue_item(svc)
    assert svc.admit_queue() == ["issue:qf"]
    assert svc.wait("queue-admit:issue:qf")["result"]["orchestrator"] == {"harness": "claude", "fallback_from": None,
                                                                          "reason": None}
    assert machine_events(svc) == []


def test_a_manual_start_without_an_allowed_route_is_refused(svc):
    svc.config = lambda: {**POLICY, "scheduler": {"orchestrator_route": "claude"}}
    svc.quota_probe = lambda: {"claude": {"remaining_percent": 0}}
    err = refused(svc, cmd("cmd-route-start1", "start_issue", {"repo": REPO, "issue": 9}))
    assert err.reason == "orchestrator-quota-exhausted" and svc.launcher.launches == []


def test_the_fake_launcher_takes_the_harness():
    from office.web.launcher import FakeLauncher
    fake = FakeLauncher()
    assert fake.launch(cwd="/tmp", prompt="p", label="l", harness="codex")["harness"] == "codex"
    assert fake.launches[-1]["harness"] == "codex"


@pytest.mark.parametrize("quota,harness", [
    ({"codex": {"remaining_percent": 70}}, "claude"),  # primary unknown: no fallback even with codex known
    ({"claude": {"remaining_percent": 4}, "codex": {"remaining_percent": 5}}, None),  # both at the reserve
])
def test_fallback_needs_known_exhausted_primary_and_known_healthy_fallback(quota, harness):
    assert orchestrator_route(POLICY, quota)["harness"] == harness


def test_an_uninstalled_fallback_is_skipped():
    conf = {**POLICY, "scheduler": {"orchestrator_route": "claude", "orchestrator_fallbacks": ["agy", "codex"]}}
    quota = {"claude": {"remaining_percent": 1}, "agy": {"remaining_percent": 90}, "codex": {"remaining_percent": 90}}
    assert orchestrator_route(conf, quota, available=lambda h: h != "agy")["harness"] == "codex"
    assert orchestrator_route(conf, quota, available=lambda h: False)["harness"] is None


def test_the_queue_loop_checks_the_route_before_recording_a_receipt(svc):
    svc.config = lambda: POLICY
    svc.quota_probe = dict  # unknown quota: the configured route, which is not installed yet
    real = svc.launcher.unavailable
    svc.launcher.unavailable = lambda harness=None: "not on PATH" if harness in (None, "claude") else real(harness)
    queue_issue_item(svc)
    assert svc.admit_queue() == []  # the item waits; a refused receipt would end it for good
    assert svc.command("queue-admit:issue:qf") is None
    svc.launcher.unavailable = real
    assert svc.admit_queue() == ["issue:qf"]  # once it is installed, the item launches
    assert svc.wait("queue-admit:issue:qf")["result"]["orchestrator"]["harness"] == "claude"


def test_a_held_launch_is_recorded_once_across_restarts(svc):
    svc.config = lambda: {**POLICY, "scheduler": {"orchestrator_route": "claude"}}
    svc.quota_probe = lambda: {"claude": {"remaining_percent": 1}}
    queue_issue_item(svc)
    svc.admit_queue()
    svc.held.clear()  # what a restarted service starts with
    svc.admit_queue()
    assert [e["kind"] for e in machine_events(svc)] == ["queue.orchestrator_held"]
