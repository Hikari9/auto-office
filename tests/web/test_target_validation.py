"""Per-kind command target validation: the target shape each kind accepts, then what it must name."""
from __future__ import annotations

import pytest

from office import db
from office.web import server, synthetic
from office.web.service import KIND_TARGET, KINDS, TARGETS, Command, CommandRefused

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


def queue_issue_item(svc, item="issue:qf", number=8):
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
        "VALUES(?,'issue',?, 't', 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (item, f"{REPO}#{number}")))


def valid_target(kind, run_id="r-1"):
    return {"issue": {"repo": REPO, "issue": 3}, "run": {"run_id": run_id},
            "scheduler_item": {"run_id": run_id}, "scheduler_scope": {"run_id": run_id},
            "dispatch": {"run_id": run_id, "dispatch_id": "D1"},
            "orchestrator_session": {"run_id": run_id, "session": "session:s1"},
            "settings_tier": {"tier": "machine", "key": "scheduler.auto_mode"}}[KIND_TARGET[kind]]


def test_every_kind_declares_its_target():
    assert set(KIND_TARGET) == set(KINDS) and set(KIND_TARGET.values()) == set(TARGETS)


GOOD_TARGETS = [
    ("start_issue", {"repo": REPO, "issue": 3}), ("queue_issue", {"repo": REPO, "issue": 3}),
    ("resume_run", {"run_id": "r-1"}), ("attach_run", {"run_id": "r-1"}),
    ("pause", {"item": "issue:abc"}), ("pause", {"run_id": "r-1"}), ("resume", {"run_id": "r-1", "task_id": "T1"}),
    ("set_priority", {"item": "issue:abc"}), ("demote", {"run_id": "r-1"}),
    ("set_auto_mode", {}), ("set_auto_mode", {"run_id": "r-1"}),
    ("change_route", {"run_id": "r-1", "dispatch_id": "D1"}),
    ("chat_send", {"run_id": "r-1", "session": "session:s1"}),
    ("chat_send", {"run_id": "r-1", "session": "session:s1", "host": "host:h"}),
    ("settings_set", {"tier": "machine", "key": "a.b"}), ("settings_unset", {"tier": "repository", "key": "a.b", "repo": REPO}),
    ("settings_set", {"tier": "run", "key": "a.b", "run_id": "r-1"}),
]


@pytest.mark.parametrize("kind,target", GOOD_TARGETS)
def test_targets_of_the_declared_shape_parse(kind, target):
    assert Command.parse({"id": "cmd-target-01", "kind": kind, "target": target}).target == target


BAD_TARGETS = [
    ("start_issue", {}), ("start_issue", {"repo": REPO}), ("start_issue", {"repo": REPO, "issue": "3"}),
    ("start_issue", {"repo": REPO, "issue": 0}), ("start_issue", {"repo": REPO, "issue": True}),
    ("queue_issue", {"repo": REPO, "issue": 3, "run_id": "r-1"}),  # an issue target names no run
    ("resume_run", {}), ("resume_run", {"run_id": ""}), ("resume_run", {"run_id": "  "}), ("attach_run", {"run_id": 7}),
    ("attach_run", {"repo": REPO, "issue": 3}),  # an issue is not a run
    ("pause", {}), ("pause", {"item": "x", "run_id": "r-1"}), ("resume", {"task_id": "T1"}),
    ("set_priority", {"run_id": "r-1", "dispatch_id": "D1"}),
    ("set_auto_mode", {"item": "x"}), ("set_auto_mode", {"run_id": None}),
    ("change_route", {"run_id": "r-1"}), ("change_route", {"dispatch_id": "D1"}),
    ("chat_send", {"run_id": "r-1"}), ("chat_send", {"session": "session:s1"}),
    ("chat_send", {"run_id": "r-1", "session": "session:s1", "pane": "p1"}),  # never a raw pane
    ("settings_set", {"key": "a.b"}), ("settings_set", {"tier": "machine"}),
    ("settings_unset", {"tier": "machine", "key": "a.b", "item": "x"}),
    ("start_issue", {"repo": "x" * 600, "issue": 3}),
]


@pytest.mark.parametrize("kind,target", BAD_TARGETS)
def test_targets_outside_the_declared_shape_are_refused(kind, target):
    with pytest.raises(CommandRefused) as info:
        Command.parse({"id": "cmd-target-02", "kind": kind, "target": target})
    assert (info.value.reason, info.value.http) == ("bad-target", 400)
    assert KIND_TARGET[kind].replace("_", " ") in str(info.value)


def test_a_bad_target_over_http_records_no_receipt(svc):
    with pytest.raises(CommandRefused):
        svc.submit(cmd("cmd-target-03", "pause", {"run_id": "r-1", "pane": "p"}), wait=True)
    assert svc.writer(lambda con: con.execute("SELECT COUNT(*) FROM commands WHERE id='cmd-target-03'").fetchone()[0]) == 0


def test_readiness_is_still_checked_after_the_shape(svc):
    err = refused(svc, cmd("cmd-target-04", "start_issue", {"repo": "synth-org-0/not-ready", "issue": 1}))
    assert err.reason == "repo-not-ready"
    err = refused(svc, cmd("cmd-target-05", "resume_run", {"run_id": "no-such-run"}))
    assert err.reason == "run-missing"

@pytest.mark.parametrize("kind", ["merge", "land", "deploy", "shell", "exec", "", None])
def test_unknown_kinds_are_refused(kind):
    with pytest.raises(CommandRefused) as info:
        Command.parse({"id": "cmd-00000001", "kind": kind})
    assert info.value.reason == "unknown-kind" and info.value.http == 400




def test_queue_issue_refuses_a_duplicate_queue_item(svc):
    queue_issue_item(svc, number=4)
    err = refused(svc, cmd("cmd-tv-queue1", "queue_issue", {"repo": REPO, "issue": 4}))
    assert err.reason == "already-queued" and err.data["item"] == "issue:qf"
    assert svc.executor.calls == []
    svc.submit(cmd("cmd-tv-queue2", "queue_issue", {"repo": REPO, "issue": 5}), wait=True)  # another issue
    assert svc.executor.calls[-1]["args"][:3] == ["queue", "add", f"{REPO}#5"]


def test_a_url_form_queue_ref_counts_as_the_same_issue(svc):
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) VALUES('issue:url','issue',?,"
        "'t','normal','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')", (f"https://github.com/{REPO.upper()}/issues/6",)))
    assert refused(svc, cmd("cmd-tv-queue3", "queue_issue", {"repo": REPO, "issue": 6})).reason == "already-queued"


def test_start_issue_needs_an_exact_discovered_repository(svc):
    assert refused(svc, cmd("cmd-tv-start1", "start_issue", {"repo": "synth-org-0/nope", "issue": 1})).reason == "repo-unknown"


def test_start_issue_refuses_an_issue_with_a_live_run(svc):
    err = refused(svc, cmd("cmd-tv-start2", "start_issue", {"repo": REPO, "issue": 1}))
    assert err.reason == "issue-has-live-run"


def test_run_kinds_refuse_terminal_runs(svc):
    closed = runs(svc, liveness="terminal")[0]
    for i, kind in enumerate(("resume_run", "pause", "set_priority")):
        err = refused(svc, cmd(f"cmd-tv-term{i}", kind, {"run_id": closed["run_id"]}, {"level": "high"}))
        assert err.reason == "run-terminal", kind


def test_scheduler_kinds_need_an_existing_item_and_never_github_readiness(svc):
    assert refused(svc, cmd("cmd-tv-item1", "pause", {"item": "issue:none"})).reason == "item-missing"
    queue_issue_item(svc, item="issue:nr", number=1)
    write(svc, lambda con: con.execute("UPDATE sched_items SET ref='synth-org-0/not-ready#1' WHERE id='issue:nr'"))
    svc.submit(cmd("cmd-tv-item2", "pause", {"item": "issue:nr"}), wait=True)  # a not-ready repo still pauses
    assert svc.executor.calls[-1]["args"] == ["queue", "pause", "issue:nr"]


def test_task_must_belong_to_the_run(svc):
    run = live_run(svc)
    err = refused(svc, cmd("cmd-tv-task1", "pause", {"run_id": run["run_id"], "task_id": "T-none"}))
    assert err.reason == "task-missing"


def test_set_auto_mode_needs_only_a_live_office_source(svc):
    svc.submit(cmd("cmd-tv-auto1", "set_auto_mode", {}, {"mode": "off"}), wait=True)
    assert svc.executor.calls[-1]["args"] == ["queue", "auto", "off"]
    svc.last_ok -= svc.stale_after + 1
    assert refused(svc, cmd("cmd-tv-auto2", "set_auto_mode", {}, {"mode": "on"})).reason == "office-stale"


def test_change_route_needs_the_current_live_dispatch(svc):
    run = live_run(svc)
    err = refused(svc, cmd("cmd-tv-route1", "change_route", {"run_id": run["run_id"], "dispatch_id": "D-none"},
                           {"route": "claude/m@high", "quote": "x"}))
    assert err.reason == "dispatch-not-current"


def chat_run(svc):
    """A run whose orchestrator chat is allowed, and its first orchestrator session."""
    e = svc.snapshot()["entities"]
    a = next(a for a in e["agents"].values() if a["column"] == "orchestrators"
             and e["runs"][a["run"]]["controls"]["chat_send"]["allowed"])
    return e["runs"][a["run"]], a["id"]


@pytest.mark.parametrize("session,reason", [
    ("dispatch:D1", "not-orchestrator"),           # a worker or reviewer
    ("session:{run}/claude/gone", "binding-ended"),  # this run's shape, but not an active binding
    ("session:other-run/claude/s1", "bad-target"),  # another run's session
])
def test_chat_needs_the_active_orchestrator_binding(svc, session, reason):
    run, _ = chat_run(svc)
    target = {"run_id": run["run_id"], "session": session.format(run=run["run_id"])}
    assert refused(svc, cmd("cmd-tv-chat1", "chat_send", target, {"text": "hi"})).reason == reason
    assert svc.launcher.sent == []


@pytest.mark.parametrize("target,reason", [
    ({"tier": "run", "key": "scheduler.auto_mode", "run_id": "r-1"}, "tier-not-editable"),
    ({"tier": "machine", "key": "no.such.key"}, "unknown-setting"),
    ({"tier": "repository", "key": "scheduler.auto_mode", "repo": "synth-org-0/not-ready"}, "repo-not-ready"),
])
def test_settings_need_a_known_key_and_an_editable_tier(svc, target, reason):
    assert refused(svc, cmd(f"cmd-tv-set-{reason}", "settings_set", target, {"value": 1})).reason == reason
    assert svc.executor.calls == []


@pytest.mark.parametrize("key,known", [
    ("scheduler.auto_mode", True), ("web.checkouts.acme", True),
    ("scheduler", False),               # a whole section, never replaced by a scalar
    ("web.checkouts.acme.deep", False),  # one entry of an empty map, not a subtree
    ("scheduler.no_such_key", False),
])
def test_known_settings(key, known):
    from office.web.service import _known_setting
    assert _known_setting(key) is known


def test_a_known_key_on_the_machine_tier_runs_office_config(svc):
    svc.submit(cmd("cmd-tv-set-ok1", "settings_set", {"tier": "machine", "key": "scheduler.auto_mode"}, {"value": False}),
               wait=True)
    assert svc.executor.calls[-1]["args"] == ["config", "--user", "--", "scheduler.auto_mode", "false"]


def test_settings_accept_entries_of_a_shipped_empty_map(svc):
    svc.submit(cmd("cmd-tv-set-02", "settings_set", {"tier": "machine", "key": "web.checkouts.acme"}, {"value": "/x"}),
               wait=True)
    assert svc.executor.calls[-1]["args"][-2:] == ["web.checkouts.acme", "/x"]


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_is_refused_while_office_is_stale(svc, kind):
    svc.last_ok -= svc.stale_after + 1
    target = valid_target(kind, live_run(svc)["run_id"])
    assert refused(svc, cmd(f"cmd-tv-st-{kind}", kind, target, {"text": "hi", "mode": "on", "value": 1})).reason == "office-stale"
