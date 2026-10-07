"""POST /api/commands semantics: receipts, idempotency, re-validation and execution."""
from __future__ import annotations

import subprocess

import pytest

from office import db
from office.web import server, synthetic
from office.web.executor import Executor
from office.web.service import KINDS, Command, CommandRefused

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


def runs(svc, **match):
    return [r for r in svc.snapshot()["entities"]["runs"].values()
            if all(r.get(k) == v for k, v in match.items())]


def cmd(cid, kind, target=None, payload=None, expect=None):
    return Command.parse({"id": cid, "kind": kind, "target": target or {}, "payload": payload or {},
                          "expect": expect or {}})


def refused(svc, c) -> CommandRefused:
    with pytest.raises(CommandRefused) as info:
        svc.submit(c, wait=True)
    return info.value


def live_run(svc):
    return next(r for r in runs(svc, liveness="live") if r["office_version"] == synthetic.CURRENT_VERSION)


# ------------------------------------------------------------------ kinds

@pytest.mark.parametrize("kind", ["merge", "land", "deploy", "shell", "exec", "", None])
def test_unknown_kinds_are_refused(kind):
    with pytest.raises(CommandRefused) as info:
        Command.parse({"id": "cmd-00000001", "kind": kind})
    assert info.value.reason == "unknown-kind" and info.value.http == 400


def test_kind_list_has_no_landing_or_shell():
    assert not {"merge", "land", "deploy", "shell"} & set(KINDS)


@pytest.mark.parametrize("body", [[], {"id": "short", "kind": "pause"}, {"id": "x" * 200, "kind": "pause"},
                                  {"id": "cmd-00000001", "kind": "pause", "target": "run"}])
def test_malformed_commands_are_refused(body):
    with pytest.raises(CommandRefused) as info:
        Command.parse(body)
    assert info.value.http == 400


# ------------------------------------------------------------------ idempotency

def test_duplicate_id_never_executes_twice(svc):
    run = live_run(svc)
    c = cmd("cmd-dup-0001", "pause", {"run_id": run["run_id"]}, {"reason": "lunch"})
    first = svc.submit(c, wait=True)
    second = svc.submit(c, wait=True)
    assert first["status"] == "completed" and second["status"] == "completed"
    assert second["replayed"] is True and not first.get("replayed")
    assert len(svc.executor.calls) == 1
    assert svc.executor.calls[0]["args"] == ["queue", "pause", "--run", run["run_id"], "--reason", "lunch"]


def test_reused_id_for_another_request_conflicts(svc):
    run = live_run(svc)
    svc.submit(cmd("cmd-dup-0002", "pause", {"run_id": run["run_id"]}), wait=True)
    err = refused(svc, cmd("cmd-dup-0002", "demote", {"run_id": run["run_id"]}))
    assert err.reason == "idempotency-conflict"
    assert len(svc.executor.calls) == 1


def test_receipt_is_recorded_before_execution(svc):
    run = live_run(svc)
    seen = []

    class Spy:
        calls = []

        def run(self, args, cwd):
            seen.append(svc.command("cmd-order-001")["status"])
            from office.web.executor import Outcome
            return Outcome("completed", {})
    svc.executor = Spy()
    svc.submit(cmd("cmd-order-001", "demote", {"run_id": run["run_id"]}), wait=True)
    assert seen == ["running"]
    assert svc.command("cmd-order-001")["status"] == "completed"


# ------------------------------------------------------------------ re-validation

def test_missing_and_terminal_runs_fail_closed_with_a_receipt(svc):
    err = refused(svc, cmd("cmd-miss-001", "pause", {"run_id": "nope"}))
    assert err.reason == "run-missing" and err.receipt["status"] == "failed"
    terminal = runs(svc, liveness="terminal")[0]
    err = refused(svc, cmd("cmd-term-001", "pause", {"run_id": terminal["run_id"]}))
    assert err.reason == "run-terminal"
    assert svc.executor.calls == []


def test_legacy_run_is_read_only(svc):
    legacy = runs(svc, office_version=synthetic.LEGACY_VERSION)[0]
    err = refused(svc, cmd("cmd-legacy-01", "pause", {"run_id": legacy["run_id"]}))
    assert err.reason == "capability-missing" and "3.0" in str(err)


def test_expect_mismatch_is_refused(svc):
    run = live_run(svc)
    err = refused(svc, cmd("cmd-expect-01", "pause", {"run_id": run["run_id"]}, expect={"phase": "planning"}))
    assert err.reason == "expectation-failed" and err.data["actual"] == {"phase": "executing"}
    err = refused(svc, cmd("cmd-expect-02", "pause", {"run_id": run["run_id"]}, expect={"color": "red"}))
    assert err.reason == "bad-expect"


def test_task_must_belong_to_the_run(svc):
    run = live_run(svc)
    err = refused(svc, cmd("cmd-task-0001", "pause", {"run_id": run["run_id"], "task_id": "T99"}))
    assert err.reason == "task-missing"
    svc.submit(cmd("cmd-task-0002", "set_priority", {"run_id": run["run_id"], "task_id": "T1"}, {"level": "high"}),
               wait=True)
    assert svc.executor.calls[-1]["args"] == ["queue", "priority", "--run", run["run_id"], "--task", "T1",
                                              "--priority", "high"]


@pytest.mark.parametrize("kind", KINDS)
def test_stale_office_refuses_every_mutation(svc, kind):
    run = live_run(svc)
    svc.last_ok -= svc.stale_after + 1
    assert svc.office_freshness()["state"] == "stale"
    target = {"run_id": run["run_id"], "repo": REPO, "issue": 3, "tier": "machine", "key": "scheduler.auto_mode"}
    err = refused(svc, cmd(f"cmd-stale-{kind}", kind, target, {"text": "hi", "mode": "on", "value": 1}))
    assert err.reason == "office-stale"
    assert svc.executor.calls == [] and svc.launcher.launches == [] and svc.launcher.sent == []
    assert svc.writer(lambda con: con.execute("SELECT COUNT(*) FROM commands").fetchone()[0]) == 0


def test_concurrent_duplicate_that_misses_the_read_is_deduplicated_by_the_receipt(svc, monkeypatch):
    run = live_run(svc)
    c = cmd("cmd-race-0001", "pause", {"run_id": run["run_id"]})
    svc.submit(c, wait=True)
    real = svc.observer.read
    # The second POST raced the first: its pre-check saw no receipt.
    monkeypatch.setattr(svc.observer, "read", lambda fn: None if "_receipt" in fn.__code__.co_names else real(fn))
    again = svc.submit(c, wait=True)
    assert again["replayed"] is True and len(svc.executor.calls) == 1
    with pytest.raises(CommandRefused) as info:
        svc.submit(cmd("cmd-race-0001", "demote", {"run_id": run["run_id"]}), wait=True)
    assert info.value.reason == "idempotency-conflict" and len(svc.executor.calls) == 1


def test_validation_crash_fails_the_receipt_instead_of_leaving_it_accepted(svc, monkeypatch):
    run = live_run(svc)
    monkeypatch.setattr(svc, "_v_pause", lambda c: (_ for _ in ()).throw(KeyError("boom")))
    err = refused(svc, cmd("cmd-crash-0001", "pause", {"run_id": run["run_id"]}))
    assert err.reason == "validation-error" and err.receipt["status"] == "failed"
    assert svc.command("cmd-crash-0001")["status"] == "failed" and svc.executor.calls == []


# ------------------------------------------------------------------ start_issue

def test_start_issue_launches_an_orchestrator_with_issue_end_state_and_receipt(svc):
    out = svc.submit(cmd("cmd-start-001", "start_issue", {"repo": REPO, "issue": 3},
                         {"end_state": "merge", "title": "Issue 3"}), wait=True)
    assert out["status"] == "completed"
    [launch] = svc.launcher.launches
    assert f"https://github.com/{REPO}/issues/3" in launch["prompt"] and "end state `merge`" in launch["prompt"]
    assert "cmd-start-001" in launch["prompt"]
    assert out["result"]["pane"] == launch["pane"]


def test_start_issue_refused_with_a_live_run(svc):
    err = refused(svc, cmd("cmd-start-002", "start_issue", {"repo": REPO, "issue": 1}))
    assert err.reason == "issue-has-live-run" and err.data["next"] == "attach_run"
    assert svc.launcher.launches == []


def test_start_issue_with_a_resumable_run_needs_confirmation(svc):
    write(svc, lambda con: synthetic.insert_run(con, "RESUMABLE-1", git_common_dir="/synthetic/src/repo-00/.git",
                                                landing={"issue": 4}))
    err = refused(svc, cmd("cmd-start-003", "start_issue", {"repo": REPO, "issue": 4}))
    assert err.reason == "issue-has-resumable-run" and err.data["next"] == "resume_run"
    out = svc.submit(cmd("cmd-start-004", "start_issue", {"repo": REPO, "issue": 4}, {"new_run_confirmed": True}),
                     wait=True)
    assert out["status"] == "completed" and len(svc.launcher.launches) == 1


def test_start_issue_without_launcher_offers_the_copyable_command(svc):
    svc.launcher.reason = "Herdr is not available"
    err = refused(svc, cmd("cmd-start-005", "start_issue", {"repo": REPO, "issue": 3}))
    assert err.reason == "launcher-unavailable"
    assert err.data["command"].startswith(f"office start --issue https://github.com/{REPO}/issues/3 ")


def test_start_issue_refuses_a_repo_that_is_not_ready(svc):
    svc.readiness = lambda repo, checkout: {"ready": False, "failing": ["push_permission"]}
    err = refused(svc, cmd("cmd-start-006", "start_issue", {"repo": REPO, "issue": 3}))
    assert err.reason == "repo-not-ready" and err.data["failing"] == ["push_permission"]


def test_launched_run_links_back_by_issue_repo_and_pane(svc):
    svc.submit(cmd("cmd-start-007", "start_issue", {"repo": REPO, "issue": 5}), wait=True)
    pane = svc.launcher.launches[0]["pane"]

    def created(con):
        synthetic.insert_run(con, "LAUNCHED-1", git_common_dir="/synthetic/src/repo-00/.git", landing={"issue": 5})
        synthetic.insert_run(con, "OTHER-PANE", git_common_dir="/synthetic/src/repo-00/.git", landing={"issue": 5})
        synthetic.insert_binding(con, "LAUNCHED-1", "herdr", pane)
        synthetic.insert_binding(con, "OTHER-PANE", "herdr", "someone-elses-pane")
    write(svc, created)
    ent = svc.snapshot()["entities"]["runs"]
    assert ent["run:LAUNCHED-1"]["launch"] == {"command": "cmd-start-007", "pane": pane, "provenance": "web-launch"}
    assert ent["run:OTHER-PANE"]["launch"] is None


def test_queue_loop_does_not_block_the_poller_on_a_launch(svc):
    import threading
    release = threading.Event()
    real = svc.launcher.launch

    def slow(**kw):
        release.wait(10)
        return real(**kw)
    svc.launcher.launch = slow
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
        "VALUES('issue:q2','issue',?, 't', 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (f"{REPO}#7",)))
    assert svc.admit_queue() == ["issue:q2"]  # returned while the launch is still in flight
    assert svc.command("queue-admit:issue:q2")["status"] == "running"
    release.set()
    assert svc.wait("queue-admit:issue:q2")["status"] == "completed"


def test_queue_loop_admits_a_queued_issue_once(svc):
    def queued(con):
        con.execute("INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
                    "VALUES('issue:q1','issue',?,?, 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')",
                    (f"{REPO}#10", "Issue 10"))
    write(svc, queued)
    assert svc.admit_queue() == ["issue:q1"]
    svc.wait("queue-admit:issue:q1")
    svc.poll()
    assert svc.admit_queue() == []
    assert len(svc.launcher.launches) == 1
    assert svc.command("queue-admit:issue:q1")["status"] == "completed"


# ------------------------------------------------------------------ run commands

def test_resume_run_refuses_a_live_run_and_launches_a_resumable_one(svc, tmp_path):
    err = refused(svc, cmd("cmd-resume-01", "resume_run", {"run_id": live_run(svc)["run_id"]}))
    assert err.reason == "run-live"
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def resumable(con):
        synthetic.insert_run(con, "RESUME-ME", git_common_dir=str(checkout / ".git"))
    write(svc, resumable)
    out = svc.submit(cmd("cmd-resume-02", "resume_run", {"run_id": "RESUME-ME"}), wait=True)
    assert out["status"] == "completed"
    assert "office resume RESUME-ME" in svc.launcher.launches[0]["prompt"]
    assert svc.launcher.launches[0]["cwd"] == str(checkout)


def test_change_route_needs_a_current_dispatch_on_the_same_harness(svc):
    snap = svc.snapshot()
    run = live_run(svc)
    agents = [a for a in snap["entities"]["agents"].values() if a["run"] == run["id"] and a["kind"] == "dispatch"]
    live = next(a for a in agents if a["column"] == "executors" and a["state"]["process"] != "exited"
                and not a["state"]["complete"])
    done = next(a for a in agents if a["state"]["complete"])
    did, harness = live["id"].removeprefix("dispatch:"), live["harness"]
    err = refused(svc, cmd("cmd-route-001", "change_route", {"run_id": run["run_id"],
                                                             "dispatch_id": done["id"].removeprefix("dispatch:")},
                           {"route": f"{done['harness']}/m@high", "quote": "use m"}))
    assert err.reason == "dispatch-not-current"
    other = "agy" if harness != "agy" else "claude"
    err = refused(svc, cmd("cmd-route-002", "change_route", {"run_id": run["run_id"], "dispatch_id": did},
                           {"route": f"{other}/m@high", "quote": "use m"}))
    assert err.reason == "harness-mismatch"
    err = refused(svc, cmd("cmd-route-003", "change_route", {"run_id": run["run_id"], "dispatch_id": did},
                           {"route": f"{harness}/m@high"}))
    assert err.reason == "quote-required"
    svc.submit(cmd("cmd-route-004", "change_route", {"run_id": run["run_id"], "dispatch_id": did},
                   {"route": f"{harness}/m@high", "quote": "use m"}), wait=True)
    assert svc.executor.calls[-1]["args"] == ["--run", run["run_id"], "amend", "route", did, "--as",
                                              f"{harness}/m@high", "--quote", "use m", "--restart"]


def test_plan_approval_is_not_a_web_command(svc):
    assert "approve_plan" not in KINDS
    run = live_run(svc)
    with pytest.raises(CommandRefused) as info:
        svc.submit(cmd("cmd-plan-0001", "approve_plan", {"run_id": run["run_id"]}, {"quote": "yes, go ahead"}),
                   wait=True)
    assert info.value.reason == "unknown-kind"
    assert svc.executor.calls == []


def test_settings_commands_run_office_config(svc):
    svc.submit(cmd("cmd-set-00001", "settings_set", {"tier": "machine", "key": "scheduler.max_active_runs"},
                   {"value": 3}), wait=True)
    svc.submit(cmd("cmd-set-00002", "settings_unset", {"tier": "repository", "key": "scheduler.auto_mode",
                                                       "repo": REPO}), wait=True)
    assert [c["args"] for c in svc.executor.calls] == [
        ["config", "--user", "--", "scheduler.max_active_runs", "3"],
        ["config", "--repo", "--unset", "--", "scheduler.auto_mode"]]
    assert svc.executor.calls[1]["cwd"].endswith("synth-org-0__repo-00")
    err = refused(svc, cmd("cmd-set-00003", "settings_set", {"tier": "run-pinned", "key": "a"}, {"value": 1}))
    assert err.reason == "tier-not-editable"


# ------------------------------------------------------------------ exit mapping

@pytest.mark.parametrize("code,status", [(0, "completed"), (1, "failed"), (2, "failed"), (5, "failed"),
                                         (-9, "unknown"), (137, "unknown")])
def test_executor_maps_exit_codes(code, status):
    def runner(argv, **kw):
        assert argv[-2:] == ["queue", "list"] and "OFFICE_DISPATCH_ID" not in kw["env"]
        return subprocess.CompletedProcess(argv, code, "out", "")
    assert Executor(runner=runner).run(["queue", "list"], None).status == status


def test_executor_timeout_is_unknown():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    out = Executor(runner=runner).run(["queue", "list"], None)
    assert out.status == "unknown" and "did not finish" in out.error


def test_executor_crash_mid_command_leaves_unknown(svc):
    run = live_run(svc)

    class Boom:
        calls = []

        def run(self, args, cwd):
            raise RuntimeError("lost")
    svc.executor = Boom()
    out = svc.submit(cmd("cmd-boom-0001", "demote", {"run_id": run["run_id"]}), wait=True)
    assert out["status"] == "unknown"


def test_close_does_not_hang_on_a_stuck_command(tmp_path, monkeypatch):
    import threading
    import time
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx2").start()
    stuck = threading.Event()

    class Stuck:
        timeout, calls = 0.1, []

        def run(self, args, cwd):
            stuck.wait(30)
            from office.web.executor import Outcome
            return Outcome("completed", {})
    s.executor = Stuck()
    s.submit(cmd("cmd-stuck-001", "demote", {"run_id": live_run(s)["run_id"]}))
    thread = s.threads["cmd-stuck-001"]
    began = time.time()
    s.close()
    assert time.time() - began < 10
    con = db.connect(s.db_path)
    assert con.execute("SELECT status FROM commands WHERE id='cmd-stuck-001'").fetchone()[0] == "running"
    con.close()
    stuck.set()
    thread.join(10)  # a late outcome after close is logged, not raised
    assert not thread.is_alive()


# ------------------------------------------------------------------ per-kind validation (A11)

def _not_ready(svc):
    def boom(cmd):
        raise AssertionError("GitHub readiness consulted for a non-issue kind")
    svc._issue_target = boom


def test_issue_kinds_need_the_exact_issue(svc):
    for kind in ("start_issue", "queue_issue"):
        err = refused(svc, cmd(f"cmd-{kind[:5]}-nope1", kind, {"repo": REPO, "issue": 9999}))
        assert err.reason == "issue-unknown"
        err = refused(svc, cmd(f"cmd-{kind[:5]}-nope2", kind, {"repo": "synth-org-0/no-such", "issue": 1}))
        assert err.reason == "repo-unknown"
    assert svc.executor.calls == [] and svc.launcher.launches == []


def test_queue_issue_refuses_a_duplicate_queue_item(svc):
    out = svc.submit(cmd("cmd-queue-001", "queue_issue", {"repo": REPO, "issue": 9}), wait=True)
    assert out["status"] == "completed"
    assert svc.executor.calls[-1]["args"][:3] == ["queue", "add", f"{REPO}#9"]
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
        "VALUES('issue:dup','issue',?, 't', 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (f"{REPO}#9",)))
    err = refused(svc, cmd("cmd-queue-002", "queue_issue", {"repo": REPO, "issue": 9}))
    assert err.reason == "issue-already-queued" and err.data["item"] == "issue:dup"
    assert len(svc.executor.calls) == 1


def test_queue_issue_refuses_a_repo_that_is_not_ready(svc):
    svc.readiness = lambda repo, checkout: {"ready": False, "failing": ["checkout"]}
    assert refused(svc, cmd("cmd-queue-003", "queue_issue", {"repo": REPO, "issue": 9})).reason == "repo-not-ready"


def test_attach_run_needs_a_live_run(svc):
    run_id = live_run(svc)["run_id"]
    write(svc, lambda con: synthetic.insert_binding(con, run_id, "herdr", "pane-attach"))
    svc.launcher.live.add("pane-attach")
    out = svc.submit(cmd("cmd-attach-01", "attach_run", {"run_id": run_id}), wait=True)
    assert out["status"] == "completed" and out["result"]["pane"] == "pane-attach"
    write(svc, lambda con: synthetic.insert_run(con, "ATTACH-RESUMABLE", git_common_dir="/synthetic/src/repo-00/.git"))
    assert refused(svc, cmd("cmd-attach-02", "attach_run", {"run_id": "ATTACH-RESUMABLE"})).reason == "run-not-live"


@pytest.mark.parametrize("kind,payload", [("pause", {}), ("resume", {}), ("demote", {}),
                                          ("set_priority", {"level": "high"})])
def test_scheduler_kinds_check_item_or_run_never_github_readiness(svc, kind, payload):
    _not_ready(svc)
    write(svc, lambda con: con.execute(
        "INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
        "VALUES('issue:s1','issue',?, 't', 'normal', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')", (f"{REPO}#5",)))
    svc.submit(cmd(f"cmd-{kind[:6]}-item1", kind, {"item": "issue:s1"}, payload), wait=True)
    assert svc.executor.calls[-1]["args"][:3] == ["queue", "priority" if kind == "set_priority" else kind, "issue:s1"]
    svc.submit(cmd(f"cmd-{kind[:6]}-run01", kind, {"run_id": live_run(svc)["run_id"]}, payload), wait=True)
    assert svc.executor.calls[-1]["args"][2] == "--run"
    assert refused(svc, cmd(f"cmd-{kind[:6]}-item2", kind, {"item": "issue:nope"}, payload)).reason == "item-missing"
    assert refused(svc, cmd(f"cmd-{kind[:6]}-run02", kind, {"run_id": "NO-SUCH-RUN"}, payload)).reason == "run-missing"
    assert len(svc.executor.calls) == 2


def test_set_auto_mode_is_machine_level_without_a_run(svc):
    _not_ready(svc)
    svc.submit(cmd("cmd-auto-0001", "set_auto_mode", {}, {"mode": "on"}), wait=True)
    assert svc.executor.calls[-1]["args"] == ["queue", "auto", "on"]
    assert refused(svc, cmd("cmd-auto-0002", "set_auto_mode", {"run_id": "NO-SUCH-RUN"},
                            {"mode": "off"})).reason == "run-missing"


def test_settings_need_a_known_key(svc):
    err = refused(svc, cmd("cmd-set-00010", "settings_set", {"tier": "machine", "key": "no_such.key"}, {"value": 1}))
    assert err.reason == "unknown-key"
    err = refused(svc, cmd("cmd-set-00011", "settings_set", {"tier": "machine", "key": "scheduler.no_such_leaf"},
                           {"value": 1}))
    assert err.reason == "unknown-key"
    assert svc.executor.calls == []
