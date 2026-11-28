"""Offline contracts for the trusted-worker bridge; no cloud or model calls."""
import json
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from office import bridge, commands, db
from office.util import now_iso


def request(operation="start", mission=None, **arguments):
    if operation == "start":
        arguments = {"repository": "fixture", "goal": "Bounded bridge test", "gear": "S", **arguments}
    return {"protocol": bridge.PROTOCOL, "request_id": str(uuid.uuid4()),
            "mission_id": mission or str(uuid.uuid4()), "coordinator_id": "cloud:rico", "fence": 1,
            "operation": operation, "arguments": arguments}


def admit(req):
    return {"allowed": True, **{k: req[k] for k in ("mission_id", "request_id", "fence")},
            "request_digest": bridge.request_digest(req)}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        con = db.connect()
        try:
            if argv[0] == "start":
                rid = uuid.uuid4().hex
                con.execute("INSERT INTO runs(id,goal,phase,repo_root,git_common_dir,gear,office_version) VALUES(?,?,?,?,?,?,?)",
                            (rid, argv[-1].strip(), "planning", str(repo), str(repo / ".git"), "S", "test"))
                con.execute("INSERT INTO session_bindings VALUES(?,?,?,?,?,?)",
                            (bridge.PROTOCOL, argv[argv.index("--session") + 1], rid, now_iso(), "start", None,))
                return {"exit_code": 0, "result": {"ok": True, "data": {"run_id": rid}}}
            if "close" in argv:
                con.execute("UPDATE runs SET phase='abandoned' WHERE id=?", (argv[argv.index("--run") + 1],))
            return {"exit_code": 0, "result": {"ok": True, "data": {}}}
        finally:
            con.close()

    class Worker:
        registry = {"fixture": str(repo)}

        def run(self, req, authorize=admit, custom_runner=None):
            return bridge.execute(req, authorize=authorize, workspaces=self.registry, runner=custom_runner or runner)

    out = Worker()
    out.calls, out.runner, out.repo = calls, runner, repo
    return out


def test_start_and_replay_preserve_office_authority_and_routing(worker):
    req = request(goal="--set roles.executor=danger; $(false)")
    result = worker.run(req)
    assert result["ok"], result
    again = worker.run(req)
    assert again["replayed"] and again["receipt"] == result["receipt"]
    assert len(worker.calls) == 1
    argv = worker.calls[0]
    assert argv[-2:] == ["--", req["arguments"]["goal"]]
    assert argv[argv.index("--end-state") + 1] == "ask"
    assert "--route" not in argv and "--no-fastlane" not in argv and "--no-review" not in argv
    assert "--no-card" in argv


def test_new_request_cannot_duplicate_a_mission_start(worker):
    req = request()
    worker.run(req)
    with pytest.raises(bridge.BridgeError, match="already has a start"):
        worker.run(request(mission=req["mission_id"]))
    assert len(worker.calls) == 1


def test_concurrent_replay_has_one_launch(worker):
    req = request()
    entered, release = threading.Event(), threading.Event()

    def slow(argv, cwd, timeout):
        entered.set()
        assert release.wait(5)
        return worker.runner(argv, cwd, timeout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.run, req, custom_runner=slow)
        assert entered.wait(5)
        replay = worker.run(req)
        assert replay["replayed"] and replay["receipt"]["status"] == "running"
        release.set()
        assert first.result()["ok"]
    assert len(worker.calls) == 1


def test_changed_payload_or_fence_never_reuses_a_receipt(worker):
    req = request()
    worker.run(req)
    for mutation in ({"fence": 2}, {"mission_id": str(uuid.uuid4())}, {"arguments": {**req["arguments"], "goal": "Different"}}):
        with pytest.raises(Exception, match="already used for a different request"):
            worker.run({**req, **mutation})
    assert len(worker.calls) == 1


@pytest.mark.parametrize("field,value", [("fence", True), ("fence", 0), ("fence", 2**53),
                                        ("operation", "land"), ("operation", []), ("request_id", "../../run")])
def test_bad_envelopes_cannot_reach_admission(worker, field, value):
    req = {**request(), field: value}
    with pytest.raises(bridge.BridgeError):
        worker.run(req, authorize=lambda _: pytest.fail("invalid request reached admission"))
    assert not worker.calls


@pytest.mark.parametrize("extra", [{"route": "claude/opus"}, {"path": "/root"}, {"end_state": "merge"},
                                   {"environment": {"GH_TOKEN": "secret"}}, {"argv": ["sh", "-c", "bad"]}])
def test_start_rejects_permissions_paths_and_command_injection(worker, extra):
    with pytest.raises(bridge.BridgeError):
        worker.run(request(**extra))
    assert not worker.calls


@pytest.mark.parametrize("changed", [{"allowed": False}, {"fence": 2}, {"fence": True},
                                     {"request_digest": "0" * 64}, {"mission_id": str(uuid.uuid4())}])
def test_admission_must_bind_exact_payload_and_current_fence(worker, changed):
    with pytest.raises(bridge.BridgeError, match="exact request"):
        worker.run(request(), authorize=lambda req: {**admit(req), **changed})
    assert not worker.calls


def test_admission_outage_fails_closed_without_exposing_secrets(worker):
    def unavailable(_):
        raise RuntimeError("SECRET-CREDENTIAL")

    with pytest.raises(bridge.BridgeError) as error:
        worker.run(request(), authorize=unavailable)
    assert error.value.category == "admission-unavailable"
    assert "SECRET" not in str(error.value)
    assert not worker.calls


def test_digest_is_portable_unicode_canonical_json():
    req = request(goal="Manila — 서울")
    reordered = dict(reversed(list(req.items())))
    reordered["arguments"] = dict(reversed(list(req["arguments"].items())))
    assert bridge.request_digest(req) == bridge.request_digest(reordered)
    assert "서울" in bridge.canonical(req)
    assert "\\u" not in bridge.canonical(req)


def test_dispatch_is_bound_to_mission_and_not_a_caller_run(worker):
    req = request()
    start = worker.run(req)
    run_id = start["receipt"]["result"]["result"]["data"]["run_id"]
    worker.run(request("dispatch", req["mission_id"], tasks=["T1", "T2"], parallel=True))
    assert worker.calls[-1] == ["--json", "--run", run_id, "dispatch", "--parallel", "T1", "T2"]
    with pytest.raises(bridge.BridgeError, match="no Office run"):
        worker.run(request("dispatch", tasks=["T1"]))
    with pytest.raises(bridge.BridgeError):
        worker.run(request("dispatch", req["mission_id"], tasks=["T1"], run_id="unrelated"))


def test_inspection_does_not_start_jobs_or_read_raw_evidence(worker, monkeypatch):
    req = request()
    start = worker.run(req)
    run_id = start["receipt"]["result"]["result"]["data"]["run_id"]
    from office import jobs
    monkeypatch.setattr(jobs, "kick", lambda *_: pytest.fail("inspection kicked jobs"))
    con = db.connect()
    try:
        for n in range(3):
            con.execute("INSERT INTO evidence(id,run_id,kind,path,sha256,bytes,meta_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (f"e{n}", run_id, "checks", "/private/credentials", "sha256:abc", 9, '{"secret":"do-not-export"}', now_iso()))
    finally:
        con.close()
    status = worker.run(request("status", req["mission_id"]))
    assert status["data"]["run"]["id"] == run_id
    result = worker.run(request("evidence", req["mission_id"], limit=2))["data"]
    assert result["has_more"] and len(result["evidence"]) == 2 and not result["content_included"]
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
    second = worker.run(request("evidence", req["mission_id"], after=result["next_cursor"], limit=2))["data"]
    assert [e["id"] for e in second["evidence"]] == ["e2"] and not second["has_more"]
    assert len(worker.calls) == 1
    with pytest.raises(bridge.BridgeError):
        worker.run(request("status", req["mission_id"]), authorize=lambda _: {"allowed": False})


def test_lost_start_response_recovers_only_by_transactional_session_binding(worker):
    req = request()

    def lost(argv, cwd, timeout):
        worker.runner(argv, cwd, timeout)
        raise subprocess.TimeoutExpired("office", timeout)

    initial = worker.run(req, custom_runner=lost)
    assert initial["receipt"]["status"] == "unknown"
    assert worker.run(req)["replayed"]
    status = worker.run(request("status", req["mission_id"]))["data"]
    assert status["run"] is None and status["recovery_candidate"]
    resumed = worker.run(request("resume", req["mission_id"]))
    assert resumed["ok"]
    assert worker.calls[-1][1:3] == ["--run", status["recovery_candidate"]]
    assert worker.run(request("status", req["mission_id"]))["data"]["run"]["id"] == status["recovery_candidate"]
    assert len([a for a in worker.calls if a[0] == "start"]) == 1


def test_timeout_before_any_run_does_not_guess_or_restart(worker):
    req = request()

    def lost(*_):
        raise subprocess.TimeoutExpired("office", 1)

    assert worker.run(req, custom_runner=lost)["receipt"]["status"] == "unknown"
    with pytest.raises(bridge.BridgeError, match="no confirmed run"):
        worker.run(request("resume", req["mission_id"]))
    with pytest.raises(bridge.BridgeError, match="already has a start"):
        worker.run(request(mission=req["mission_id"]))


def test_cancel_does_not_claim_untracked_workers_or_external_effects_stopped(worker):
    req = request()
    rid = worker.run(req)["receipt"]["result"]["result"]["data"]["run_id"]
    con = db.connect()
    try:
        con.execute("INSERT INTO dispatches(id,run_id,status,launcher) VALUES(?,?,'running','external')", ("d1", rid))
    finally:
        con.close()
    out = worker.run(request("cancel", req["mission_id"], reason="Reassigned"))
    cancellation = out["receipt"]["result"]["cancellation"]
    assert out["ok"] and not cancellation["known_workers_stopped"]
    assert cancellation["pending_workers"] == [{"id": "d1", "state": "unverified"}]
    assert not cancellation["external_effects_verified"]


@pytest.mark.parametrize("launch_during_close", [False, True])
def test_cancel_tracks_claimed_jobs_and_workers_started_during_close(worker, monkeypatch, launch_during_close):
    req = request()
    rid = worker.run(req)["receipt"]["result"]["result"]["data"]["run_id"]
    identities = []

    def add_job(status):
        con = db.connect()
        try:
            con.execute("INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,attempts,claimed_pid,claimed_by,created_at) "
                        "VALUES('job1',?,'checks','unique-job','{}','test',?,1,123456,'worker:123456@known-start',?)",
                        (rid, status, now_iso()))
        finally:
            con.close()

    if not launch_during_close:
        add_job("claimed")
    monkeypatch.setattr(bridge, "pid_gone", lambda _: False)

    def liveness(pid, identity):
        identities.append((pid, identity))
        return "unknown", "could not verify"

    monkeypatch.setattr(bridge, "claim_liveness", liveness)

    def close(argv, cwd, timeout):
        out = worker.runner(argv, cwd, timeout)
        if launch_during_close:
            add_job("cancelled")
        return out

    out = worker.run(request("cancel", req["mission_id"], reason="Stop"), custom_runner=close)
    cancellation = out["receipt"]["result"]["cancellation"]
    assert not cancellation["known_workers_stopped"]
    assert cancellation["pending_workers"] == [{"id": "job1", "state": "alive-or-unknown"}]
    assert identities == [(123456, "worker:123456@known-start")]


def test_mutation_replay_and_status_export_only_bounded_safe_metadata(worker):
    req = request()
    secret = "sk-sensitive-secret-token /private/unreviewed/transcript.txt"

    def leaking(argv, cwd, timeout):
        result = worker.runner(argv, cwd, timeout)
        result["result"].update({"lines": [secret], "notices": [secret], "next": secret})
        result["result"]["data"]["executor_handoffs"] = [{"error": secret}]
        return result

    started = worker.run(req, custom_runner=leaking)
    assert started["ok"] and secret not in json.dumps(started)
    rid = started["receipt"]["result"]["result"]["data"]["run_id"]
    con = db.connect()
    try:
        # Old receipts must also be projected, not trusted as pre-sanitized.
        raw = {"exit_code": 0, "result": {"ok": True, "data": {"run_id": rid, "executor_handoffs": [{"error": secret}]}, "lines": [secret]}}
        con.execute("UPDATE commands SET result_json=? WHERE id=?", (json.dumps(raw), bridge._command_id(req)))
        con.execute("UPDATE runs SET terminal_reason=? WHERE id=?", (secret, rid))
        con.execute("INSERT INTO tasks(run_id,id,title,role,status,scope_json,depends_json,interfaces_json,accept_json,checks_json,"
                    "introduced_plan_version,contract_version,acceptance_version,created_at,updated_at,pause_reason) "
                    "VALUES(?,? ,?,'executor','paused','[]','[]','[]','[]','[]',1,1,1,?,?,?)", (rid, "T1", secret, now_iso(), now_iso(), secret))
    finally:
        con.close()
    assert secret not in json.dumps(worker.run(req))
    assert secret not in json.dumps(worker.run(request("status", req["mission_id"])))


def test_web_restart_recovery_does_not_change_bridge_receipts(worker):
    con = db.connect()
    try:
        for cid, origin in (("web-job", "web"), ("queue-job", "web-queue"), ("bridge-job", "rico-office-v1:mission")):
            commands.record(con, command_id=cid, kind="test", target=None, payload={}, origin=origin)
            commands.transition(con, cid, "running", pid=1)
        recovered = commands.recover_interrupted(con, (), origins=("web", "web-queue"))
        assert sorted(recovered) == ["queue-job", "web-job"]
        assert commands.get(con, "bridge-job")["status"] == "running"
    finally:
        con.close()


def test_later_passive_status_observes_drained_workers_without_exporting_private_identity(worker, monkeypatch):
    req = request()
    rid = worker.run(req)["receipt"]["result"]["result"]["data"]["run_id"]
    con = db.connect()
    try:
        con.execute("INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,attempts,claimed_pid,claimed_by,created_at) "
                    "VALUES('job1',?,'checks','unique-job','{}','test','claimed',1,123456,'private-host:123456@private-start',?)", (rid, now_iso()))
    finally:
        con.close()
    living = [True]
    monkeypatch.setattr(bridge, "pid_gone", lambda _: not living[0])
    monkeypatch.setattr(bridge, "claim_liveness", lambda *_: ("alive" if living[0] else "dead", "test"))

    def close(argv, cwd, timeout):
        out = worker.runner(argv, cwd, timeout)
        con = db.connect()
        try:
            con.execute("UPDATE outbox SET status='cancelled',claimed_pid=NULL,claimed_by=NULL WHERE id='job1'")
        finally:
            con.close()
        return out

    cancelled = worker.run(request("cancel", req["mission_id"], reason="Stop"), custom_runner=close)
    assert not cancelled["receipt"]["result"]["cancellation"]["known_workers_stopped"]
    waiting = worker.run(request("status", req["mission_id"]))
    assert not waiting["data"]["known_workers_stopped"]
    # A second cancellation must not replace the retained identity with the
    # lifecycle's now-cleared PID and make eventual drain impossible to prove.
    repeated = worker.run(request("cancel", req["mission_id"], reason="Confirm stop"), custom_runner=close)
    assert not repeated["receipt"]["result"]["cancellation"]["known_workers_stopped"]
    living[0] = False
    stopped = worker.run(request("status", req["mission_id"]))
    assert stopped["data"]["known_workers_stopped"] and stopped["data"]["pending_worker_count"] == 0
    assert not stopped["data"]["external_effects_verified"]
    for response in (cancelled, repeated, waiting, stopped):
        assert all(s not in json.dumps(response) for s in ("private_worker_observations", "private-host", "private-start", '"pid":', "claimed_by"))
    assert len(worker.calls) == 3  # Both status calls were passive.


@pytest.mark.parametrize("clear_new_identity", [False, True])
def test_cancellation_retains_new_claimant_for_same_job_id(worker, monkeypatch, clear_new_identity):
    req = request()
    rid = worker.run(req)["receipt"]["result"]["result"]["data"]["run_id"]
    con = db.connect()
    try:
        con.execute("INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,attempts,claimed_pid,claimed_by,created_at) "
                    "VALUES('job1',?,'checks','unique-job','{}','test','claimed',1,123456,'worker:123456@old',?)", (rid, now_iso()))
    finally:
        con.close()
    monkeypatch.setattr(bridge, "pid_gone", lambda pid: pid == 123456)
    identities = []

    def liveness(pid, identity):
        identities.append((pid, identity))
        return ("dead", "old exited") if pid == 123456 else ("alive", "new still running")

    monkeypatch.setattr(bridge, "claim_liveness", liveness)

    def close(argv, cwd, timeout):
        out = worker.runner(argv, cwd, timeout)
        con = db.connect()
        try:
            con.execute("UPDATE outbox SET status='cancelled',attempts=2,claimed_pid=?,claimed_by=? WHERE id='job1'",
                        (None, None) if clear_new_identity else (234567, "worker:234567@new"))
        finally:
            con.close()
        return out

    out = worker.run(request("cancel", req["mission_id"], reason="Stop"), custom_runner=close)
    assert not out["receipt"]["result"]["cancellation"]["known_workers_stopped"]
    if not clear_new_identity:
        assert (234567, "worker:234567@new") in identities
    else:
        assert out["receipt"]["result"]["cancellation"]["pending_workers"] == [{"id": "job1", "state": "unverified"}]
    assert worker.run(request("status", req["mission_id"]))["data"]["pending_worker_count"] == 1


def test_stdio_requires_operator_configuration_and_never_echoes_hook_secrets(worker, monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_BRIDGE_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(bridge.BridgeError, match="cannot read"):
        bridge._load_config()
    config = tmp_path / "bridge.json"
    config.write_text(json.dumps({"workspaces": worker.registry, "admission_argv": ["sh", "-c", "echo yes"]}))
    monkeypatch.setenv("OFFICE_BRIDGE_CONFIG", str(config))
    with pytest.raises(bridge.BridgeError, match="absolute operator executable"):
        bridge._load_config()
    config.write_text(json.dumps({"workspaces": worker.registry, "admission_argv": ["/usr/bin/node", "/srv/admit.mjs"]}))
    config.chmod(0o666)
    with pytest.raises(bridge.BridgeError, match="not writable"):
        bridge._load_config()


def test_real_office_lifecycle_gate_monitor_and_cancel(env):
    """The seam exercises real Office lifecycle code with isolated fake harnesses."""
    req = request()
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        code, out = env.office(*argv, cwd=cwd)
        return {"exit_code": code, "result": json.loads(out[out.index("{"):])}

    def run(r):
        return bridge.execute(r, authorize=admit, workspaces={"fixture": str(env.repo)}, runner=runner)

    started = run(req)
    assert started["ok"], started
    rid = started["receipt"]["result"]["result"]["data"]["run_id"]
    dispatched = run(request("dispatch", req["mission_id"], tasks=["T99"]))
    assert not dispatched["ok"]  # The bridge cannot bypass the actual Office plan/task gate.
    snapshot = run(request("status", req["mission_id"]))
    assert snapshot["data"]["run"]["id"] == rid
    cancelled = run(request("cancel", req["mission_id"], reason="Bounded offline test complete"))
    assert cancelled["ok"], cancelled
    assert run(request("status", req["mission_id"]))["data"]["terminal"]
