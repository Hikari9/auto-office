"""Exact route conformance probe and fingerprint cache (#494 T2).

Every probe here launches a scripted fake harness (tests/fixtures/route_probe).
No real model is called.
"""
import copy
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import adapters, config as cfg, db, route_policy, route_probe  # noqa: E402
from office.route_probe import Refused  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "route_probe" / "fake_harness.py"
ROUTE = "codex/gpt-6.1-sol@high"


class World:
    """An isolated Office home, a PATH holding one fake `codex`, and one runs.db."""

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (tmp / "data").mkdir()
        self.count = tmp / "launches.log"
        self.pids = tmp / "pids.log"
        self.db_path = tmp / "data" / "runs.db"
        monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp / "data"))
        monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp / "state"))
        monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp / "user-config.yaml"))
        monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
        system = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.bin)] + system))
        for name in ("codex", "claude"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
            script.chmod(0o755)
        self.monkeypatch = monkeypatch
        self.script()

    def script(self, **fields):
        fields.setdefault("count_file", str(self.count))
        fields.setdefault("pidfile", str(self.pids))
        self.monkeypatch.setenv("FAKE_PROBE", json.dumps(fields))

    def con(self):
        return db.connect(self.db_path)

    def launches(self) -> list[str]:
        return [l for l in self.count.read_text().splitlines() if l.startswith("launch")] if self.count.exists() else []

    def config(self, *, enabled=True, **discovery):
        config = copy.deepcopy(cfg.resolve(None)[0])
        config["routing"]["discovery"].update({"enabled": enabled, **discovery})
        config[route_policy.DIGEST_KEY] = route_policy.policy_digest(config)
        return config

    def run(self, run_id="run-A", plan_version=3, **discovery):
        return {"id": run_id, "plan_version": plan_version, "policy": self.config(**discovery)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def cand(spec=ROUTE):
    return route_probe.candidate_from_spec(spec)


def ctx(world, **extra):
    return {"origin": "preflight", "reason": "test: exact route probe", "task_id": "T1", "role": "executor",
            "primary_route": "codex@0/gpt-6.1-sol@low", "fallback_route": "claude@2/claude-haiku-5-5@high",
            **extra}


def ensure(world, con, run, spec=ROUTE, attempt=None, **kw):
    return route_probe.ensure(con, run, cand(spec), attempt_id=attempt or route_policy.new_attempt_id(),
                              context=kw.pop("context", None) or ctx(world), **kw)


def events(con, **where):
    sql = "SELECT * FROM route_discovery_events"
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    return [dict(r) for r in con.execute(sql + " ORDER BY seq", tuple(where.values()))]


def kinds(con, attempt):
    return [e["kind"] for e in events(con, attempt_id=attempt)]


# ------------------------------------------------------------------ fingerprint and cache

def test_key_names_every_part_of_the_fingerprint(world):
    c = cand()
    adapter = adapters.load_all()["codex"]
    base = route_probe.key(c, adapter)
    assert base.split("|")[:4] == ["codex", c["harness_version"], "gpt-6.1-sol", "high"]
    assert route_probe.key({**c, "harness_version": "9.9.9"}, adapter) != base
    assert route_probe.key({**c, "effort": "xhigh"}, adapter) != base
    assert route_probe.key({**c, "invocation_model_id": "gpt-6-luna"}, adapter) != base
    assert route_probe.key(c, {**adapter, "source_notes": "edited"}) != base
    assert route_probe.key(c, adapter, "reviewer") != base


def test_pass_qualifies_only_the_exact_effort_model_and_harness(world):
    con = world.con()
    rec = ensure(world, con, world.run())
    assert rec["result"] == "pass" and rec["fresh"] and rec["freshness"] == "fresh-run"
    assert route_probe.status(con, cand())["result"] == "pass"
    for sibling in ("codex/gpt-6.1-sol@xhigh", "codex/gpt-6.1-sol@low", "claude/claude-haiku-5-5@high"):
        assert route_probe.status(con, cand(sibling)) is None
    # a changed harness version or adapter invalidates the cached pass
    assert route_probe.status(con, {**cand(), "harness_version": "0.163.0"}) is None
    assert route_probe.status(con, cand(), adapter={**adapters.load_all()["codex"], "source_notes": "x"}) is None


def test_records_expire_after_the_ttl(world):
    con = world.con()
    ensure(world, con, world.run())
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    con.execute("UPDATE route_probes SET probed_at=?", (old,))
    assert route_probe.status(con, cand()) is None
    assert route_probe.status(con, cand(), ttl_days=30)["result"] == "pass"


def test_an_unsupported_effort_negative_is_not_resurrected_by_ttl_expiry(world):
    con = world.con()
    world.script(mode="unsupported_effort")
    run = world.run(max_probes_per_run=4)
    first = ensure(world, con, run, attempt="neg-1")
    assert first["reason_class"] == "unsupported-model-effort"
    con.execute("UPDATE route_probes SET probed_at=?", ((datetime.now(timezone.utc) - timedelta(days=900)).isoformat(),))
    assert route_probe.status(con, cand())["reason_class"] == "unsupported-model-effort"
    world.script(mode="pass")  # a harness that would now pass is never asked: the negative still blocks
    again = ensure(world, con, run, attempt="neg-2")
    assert again["cached"] and again["result"] == "fail" and again["source_attempt_id"] == "neg-1"
    assert len(world.launches()) == 1
    assert kinds(con, "neg-2") == ["probe-cache-hit"]
    # only a material fingerprint change is a new key and may be probed
    changed = route_probe.ensure(con, run, {**cand(), "harness_version": "9.9.9"}, attempt_id="neg-3", context=ctx(world))
    assert changed["result"] == "pass" and len(world.launches()) == 2
    # the sibling effort was never blocked
    assert ensure(world, con, run, "codex/gpt-6.1-sol@low")["result"] == "pass"


@pytest.mark.parametrize("mode,reason_class,expires", [
    ("transient", "transient", True), ("auth", "auth-quota-blocked", True), ("pass", None, True),
    ("no_write", "isolation-missing", True), ("malformed", "conformance-failed", True),
    ("unsupported_effort", "unsupported-model-effort", False)])
def test_only_the_unsupported_negative_survives_expiry_and_classes_stay_distinct(world, mode, reason_class, expires):
    con = world.con()
    world.script(mode=mode)
    rec = ensure(world, con, world.run())
    assert rec["reason_class"] == reason_class
    con.execute("UPDATE route_probes SET probed_at=?", ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),))
    survivor = route_probe.status(con, cand())
    assert (survivor is None) is expires
    if survivor:
        assert survivor["reason_class"] == reason_class


def test_transient_failures_are_retried_sooner_than_the_ttl(world):
    con = world.con()
    world.script(mode="transient")
    rec = ensure(world, con, world.run())
    assert rec["reason_class"] == "transient"
    assert route_probe.status(con, cand())["result"] == "fail"
    con.execute("UPDATE route_probes SET probed_at=?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    assert route_probe.status(con, cand()) is None


# ------------------------------------------------------------------ classification

@pytest.mark.parametrize("mode,reason_class", [
    ("unsupported_effort", "unsupported-model-effort"),
    ("auth", "auth-quota-blocked"),
    ("transient", "transient"),
    ("no_write", "isolation-missing"),
    ("write_outside", "isolation-missing"),
    ("wrong_model", "conformance-failed"),
    ("no_model", "conformance-failed"),
    ("malformed", "conformance-failed"),
    ("echo_prompt", "conformance-failed"),
    ("nonzero", "conformance-failed"),
    ("header_model_mismatch", "conformance-failed"),
    ("header_effort_mismatch", "unsupported-model-effort"),
])
def test_each_failure_class_is_recorded_precisely(world, mode, reason_class):
    con = world.con()
    world.script(mode=mode)
    rec = ensure(world, con, world.run())
    assert (rec["result"], rec["reason_class"]) == ("fail", reason_class), rec["detail"]
    assert route_probe.status(con, cand())["reason_class"] == reason_class


def test_an_unsupported_effort_leaves_the_models_other_effort_routable(world):
    con = world.con()
    world.script(modes={"max": "unsupported_effort", "*": "pass"})
    run = world.run()
    bad = ensure(world, con, run, "codex/gpt-6.1-sol@max")
    good = ensure(world, con, run, "codex/gpt-6.1-sol@high")
    assert bad["reason_class"] == "unsupported-model-effort" and good["result"] == "pass"
    assert route_probe.status(con, cand("codex/gpt-6.1-sol@max"))["result"] == "fail"
    assert route_probe.status(con, cand("codex/gpt-6.1-sol@high"))["result"] == "pass"
    assert route_policy.row_status({"dispatchable": False, "discovery": "eligible", "invocation_model_id": "x"},
                                   route_probe.status(con, cand("codex/gpt-6.1-sol@max")))["status"] \
        == "confirmed-unsupported"


def test_harness_header_readback_is_authoritative_and_accepted(world):
    con = world.con()
    world.script(header=True)
    rec = ensure(world, con, world.run())
    assert rec["result"] == "pass" and "harness header" in rec["detail"]


def test_the_probe_prompt_names_no_reply_and_triggers_no_failure_signature():
    prompt = route_probe.probe_prompt()
    assert route_probe.classify_text(prompt) is None
    assert "PROBE-OK followed by" in prompt and "PROBE-OK " + "a" not in prompt


# ------------------------------------------------------------------ isolation

def test_probe_runs_in_a_disposable_git_worktree_and_removes_it(world, monkeypatch):
    import subprocess
    seen = {}
    real = route_probe._spawn

    def spy(argv, cwd, env, stdin):
        git = lambda *a: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True).stdout.strip()
        seen.update(cwd=cwd, inside=git("rev-parse", "--is-inside-work-tree"), top=git("rev-parse", "--show-toplevel"),
                    linked=git("rev-parse", "--git-common-dir") != git("rev-parse", "--git-dir"))
        return real(argv, cwd, env, stdin)
    monkeypatch.setattr(route_probe, "_spawn", spy)
    assert ensure(world, world.con(), world.run())["result"] == "pass"
    assert seen["inside"] == "true" and seen["linked"] and Path(seen["top"]).resolve() == Path(seen["cwd"]).resolve()
    assert not Path(seen["cwd"]).exists() and not Path(seen["cwd"]).parent.exists()


def test_a_workspace_that_cannot_be_built_fails_closed_without_launching(world, monkeypatch):
    con = world.con()

    def broken(self):
        raise OSError("no git here")
    monkeypatch.setattr(route_probe._Workspace, "_setup", broken)
    rec = ensure(world, con, world.run())
    assert (rec["result"], rec["reason_class"]) == ("fail", "isolation-missing")
    assert world.launches() == []


def test_a_surviving_child_is_terminated_after_a_normal_exit(world):
    con = world.con()
    world.script(mode="child_lingers")
    rec = ensure(world, con, world.run())
    assert rec["result"] == "pass"
    pids = [int(p) for p in world.pids.read_text().split()]
    time.sleep(0.2)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_timeout_kills_the_process_group_and_closes_the_reservation(world):
    con = world.con()
    world.script(mode="hang")
    run = world.run(probe_timeout_s=1)
    started = time.time()
    rec = ensure(world, con, run, attempt="A-timeout")
    assert time.time() - started < 20
    assert (rec["result"], rec["reason_class"]) == ("fail", "transient") and "timed out" in rec["detail"]
    assert con.execute("SELECT status FROM route_probe_reservations WHERE id='A-timeout'").fetchone()[0] == "failed"
    time.sleep(0.2)
    for pid in [int(p) for p in world.pids.read_text().split()]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert kinds(con, "A-timeout") == ["probe-reserved", "probe-result"]


# ------------------------------------------------------------------ atomic allocation

def test_the_reservation_exists_before_the_harness_starts(world):
    con = world.con()
    world.script(db=str(world.db_path))
    assert ensure(world, con, world.run())["result"] == "pass"
    assert "reserved-ok" in world.count.read_text() and "reserved-missing" not in world.count.read_text()


def _threads(db_path, n, target):
    results, errors = [None] * n, []

    def work(i):
        con = db.connect(db_path)
        try:
            results[i] = target(i, con)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            con.close()
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not errors, errors
    return results



def test_concurrent_distinct_fingerprints_never_exceed_the_run_cap(world):
    world.script(delay=0.4)
    run = world.run(max_probes_per_run=2)
    efforts = ["low", "medium", "high", "xhigh"]
    results = _threads(world.db_path, 4, lambda i, con: ensure(world, con, run, f"codex/gpt-6.1-sol@{efforts[i]}"))
    refused = [r for r in results if isinstance(r, Refused)]
    assert len(world.launches()) == 2
    assert len(refused) == 2 and all(r == Refused("probe-cap") for r in refused)
    con = world.con()
    assert con.execute("SELECT COUNT(*) FROM route_probe_reservations WHERE run_id='run-A'").fetchone()[0] == 2
    assert sorted(e["kind"] for e in events(con) if e["kind"] == "probe-refused") == ["probe-refused"] * 2


def test_concurrent_callers_of_one_fingerprint_share_one_launch(world):
    world.script(delay=0.5)
    run = world.run()
    results = _threads(world.db_path, 3, lambda i, con: ensure(world, con, run, attempt=f"A{i}"))
    assert len(world.launches()) == 1
    assert all(isinstance(r, dict) and r["result"] == "pass" for r in results)
    con = world.con()
    hits = events(con, kind="probe-cache-hit")
    winner = next(r["attempt_id"] for r in results if not r["cached"])
    assert len(hits) == 2 and {h["source_attempt_id"] for h in hits} == {winner}
    assert con.execute("SELECT COUNT(*) FROM route_probe_reservations WHERE status='reserved'").fetchone()[0] == 0


def test_failed_and_abandoned_attempts_both_consume_the_cap(world):
    con = world.con()
    run = world.run(max_probes_per_run=2)
    world.script(mode="unsupported_effort")
    assert ensure(world, con, run, "codex/gpt-6.1-sol@low")["result"] == "fail"
    held = route_probe.reserve(con, run, cand("codex/gpt-6.1-sol@medium"), attempt_id="A-abandon", context=ctx(world))
    assert held["reserved"]
    assert route_probe.abandon(con, "A-abandon")
    third = ensure(world, con, run, "codex/gpt-6.1-sol@xhigh")
    assert third == Refused("probe-cap") and third.allocation["probes"]["used"] == 2
    assert len(world.launches()) == 1
    assert con.execute("SELECT status FROM route_probe_reservations WHERE id='A-abandon'").fetchone()[0] == "abandoned"


def test_cached_results_do_not_consume_the_cap(world):
    con = world.con()
    run = world.run(max_probes_per_run=1)
    assert ensure(world, con, run)["result"] == "pass"
    again = ensure(world, con, run)
    assert again["cached"] and again["freshness"] == "cached-fresh"
    assert len(world.launches()) == 1


def test_stale_pending_reservation_of_a_dead_owner_is_expired_without_a_second_probe(world):
    con = world.con()
    run = world.run(max_probes_per_run=1)
    held = route_probe.reserve(con, run, cand(), attempt_id="A-dead", context=ctx(world))
    assert held["reserved"]
    assert route_probe.expire_stale(con) == []  # the owner (this process) is alive
    route_probe._release("A-dead", forget=False)  # the owner dies: the OS drops its lock
    assert route_probe.expire_stale(con) == ["A-dead"]
    assert world.launches() == []
    row = con.execute("SELECT status, finished_at FROM route_probe_reservations WHERE id='A-dead'").fetchone()
    assert row[0] == "expired" and row[1]
    assert con.execute("SELECT COUNT(*) FROM route_probes").fetchone()[0] == 0
    expired = events(con, attempt_id="A-dead", kind="probe-expired")
    assert len(expired) == 1 and expired[0]["origin"] == "preflight" and expired[0]["task_id"] == "T1"
    assert expired[0]["allocation_json"] and json.loads(expired[0]["allocation_json"])["probes"]["used"] == 1
    # it still counts against the run's cap
    assert ensure(world, con, run, "codex/gpt-6.1-sol@low") == Refused("probe-cap")


def test_an_overdue_reservation_is_expired_even_if_its_owner_is_alive(world):
    con = world.con()
    route_probe.reserve(con, world.run(), cand(), attempt_id="A-slow", context=ctx(world))
    later = datetime.now(timezone.utc) + timedelta(seconds=route_policy.DISCOVERY_DEFAULTS["probe_timeout_s"]
                                                    + route_probe.GRACE_S + 5)
    assert route_probe.expire_stale(con, now=later) == ["A-slow"]
    route_probe._release("A-slow")


def test_sweep_expires_stale_reservations_for_any_command(world):
    con = world.con()
    route_probe.reserve(con, world.run(), cand(), attempt_id="A-sweep", context=ctx(world))
    route_probe._release("A-sweep", forget=False)
    con.close()
    assert route_probe.sweep() == ["A-sweep"]
    assert route_probe.sweep() == []


def test_sweep_does_not_create_a_database(world):
    assert not world.db_path.exists()
    assert route_probe.sweep() == []
    assert not world.db_path.exists()


def test_an_interrupted_probe_is_abandoned_and_still_counted(world, monkeypatch):
    con = world.con()

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(route_probe, "_run_probe", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ensure(world, con, world.run(), attempt="A-int")
    assert con.execute("SELECT status FROM route_probe_reservations WHERE id='A-int'").fetchone()[0] == "abandoned"
    assert kinds(con, "A-int") == ["probe-reserved", "probe-abandoned"]
    assert con.execute("SELECT COUNT(*) FROM route_probes").fetchone()[0] == 0


# ------------------------------------------------------------------ refusals

def test_denied_routes_are_never_probed(world):
    con = world.con()
    run = world.run()
    run["policy"]["routing"]["user_policy"]["denied_models"] = ["codex/gpt-6.1-sol@high"]
    refused = ensure(world, con, run)
    assert refused == Refused("route-denied") and "denied" in refused.detail
    assert world.launches() == []
    # only that exact route: its sibling effort is still probe-able
    assert ensure(world, con, run, "codex/gpt-6.1-sol@low")["result"] == "pass"


def test_denying_a_harness_blocks_every_route_on_it(world):
    con = world.con()
    run = world.run()
    run["policy"]["routing"]["user_policy"]["denied_models"] = ["harness:codex"]
    assert ensure(world, con, run) == Refused("route-denied")


def test_archived_and_unsupported_rows_are_never_probed(world):
    con = world.con()
    run = world.run()
    archived = {**cand(), "model_id": "claude-fable-5-1", "invocation_model_id": "claude-fable-5-1",
                "harness": "claude", "adapter_id": "claude", "effort": "max"}
    assert route_probe.ensure(con, run, archived, attempt_id="A-arch", context=ctx(world)) == Refused("archived")
    luna = cand("codex/gpt-6-luna@none")
    assert route_probe.ensure(con, run, luna, attempt_id="A-luna", context=ctx(world)) == Refused("not-eligible")
    from office import candidates
    row = next(r for r in candidates.catalog_rows() if r.get("model_id") == "claude-sonnet-5-5"
               and r.get("dispatchable") is False)  # benchmark-only: no invocation id, never probe-able
    sonnet = {**cand("claude/claude-haiku-5-5@high"), "model_id": row["model_id"], "invocation_model_id": None,
              "effort": row["effort"]}
    refused = route_probe.ensure(con, run, sonnet, attempt_id="A-son", context=ctx(world))
    assert refused == Refused("not-eligible") and "not explicitly discovery-eligible" in refused.detail
    assert world.launches() == []
    assert [e["kind"] for e in events(con)] == ["probe-refused"] * 3


def test_a_forged_candidate_cannot_widen_what_the_catalog_allows(world):
    con = world.con()
    forged = {**cand("codex/gpt-6-luna@none"), "discovery": True, "route_status": "discovered-unconfirmed"}
    assert route_probe.ensure(con, world.run(), forged, attempt_id="A-forge", context=ctx(world)) == Refused("not-eligible")
    assert world.launches() == []


def test_unknown_routes_cannot_be_named_on_the_command_line(world):
    from office.state import Usage
    for spec in ("codex/not-a-model@high", "claude/claude-fable-5-1@max", "codex/gpt-6.1-sol", "gpt-6.1-sol@high"):
        with pytest.raises(Usage):
            route_probe.candidate_from_spec(spec)


def test_a_probe_that_would_cross_the_protected_quota_reserve_is_refused(world):
    con = world.con()
    c = cand()
    c["quota"] = {"status": "ok", "tightest_remaining_percent": 6.0, "projected_burn_percent": 2.0}
    refused = route_probe.ensure(con, world.run(), c, attempt_id="A-quota", context=ctx(world))
    assert refused == Refused("quota-reserve")
    assert world.launches() == []
    c["quota"] = {"status": "unknown", "tightest_remaining_percent": None}
    assert route_probe.ensure(con, world.run(), c, attempt_id="A-quota2", context=ctx(world))["result"] == "pass"


def test_discovery_must_be_enabled_for_automatic_probes_but_not_manual_ones(world):
    con = world.con()
    off = world.run(enabled=False)
    assert ensure(world, con, off) == Refused("discovery-disabled")
    manual = ensure(world, con, None, context={"config": world.config(enabled=False)})
    assert manual["result"] == "pass"


def test_planner_and_reviewer_roles_never_get_automatic_probes(world):
    con = world.con()
    for role in ("planner", "code_reviewer", "integration_reviewer"):
        assert ensure(world, con, world.run(), context=ctx(world, role=role)) == Refused("role-not-eligible")
    assert world.launches() == []


def test_a_route_with_an_unreadable_harness_version_is_refused_not_guessed(world):
    con = world.con()
    unknown = {**cand(), "harness_version": "unknown"}
    assert route_probe.ensure(con, world.run(), unknown, attempt_id="A-unk", context=ctx(world)) == Refused("no-fingerprint")


def test_a_missing_harness_is_refused(world, monkeypatch):
    con = world.con()
    c = cand()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert route_probe.ensure(con, world.run(), c, attempt_id="A-gone", context=ctx(world)) == Refused("not-installed")


def test_available_routes_are_not_probed_automatically_but_can_be_probed_by_hand(world):
    con = world.con()
    from office import candidates
    row = next(r for r in candidates.catalog_rows() if r.get("invocation_harness") == "codex"
               and r.get("dispatchable") is not False)
    spec = f"codex/{row['model_id']}@{row['effort']}"
    assert ensure(world, con, world.run(), spec) == Refused("already-available")
    assert ensure(world, con, None, spec, context={"origin": "manual", "config": world.config(enabled=False)})["result"] == "pass"


# ------------------------------------------------------------------ immutable attempt evidence

def test_every_outcome_appends_its_event_under_the_callers_attempt(world):
    con = world.con()
    run = world.run()
    ensure(world, con, run, attempt="A1")
    assert kinds(con, "A1") == ["probe-reserved", "probe-result"]
    ensure(world, con, run, attempt="A2")
    assert kinds(con, "A2") == ["probe-cache-hit"]
    run["policy"]["routing"]["user_policy"]["denied_models"] = ["harness:codex"]
    ensure(world, con, run, attempt="A3")
    assert kinds(con, "A3") == ["probe-refused"]
    first = events(con, attempt_id="A1")
    result = next(e for e in first if e["kind"] == "probe-result")
    assert result["probe_freshness"] == "fresh-run" and result["outcome"] == "pass"
    assert result["run_id"] == "run-A" and result["plan_version"] == 3 and result["task_id"] == "T1"
    assert result["primary_route"] == "codex@0/gpt-6.1-sol@low" and result["fallback_route"].startswith("claude@")
    assert json.loads(result["fingerprint_json"])["effort"] == "high"
    assert result["policy_digest"] == run["policy"][route_policy.DIGEST_KEY]
    assert result["reason"] == "test: exact route probe"
    allocation = json.loads(result["allocation_json"])
    assert allocation["probes"] == {"used": 1, "max": 2}
    hit = events(con, attempt_id="A2")[0]
    assert hit["source_attempt_id"] == "A1" and hit["probe_freshness"] == "cached-fresh"
    assert events(con, attempt_id="A1") == first  # the producing attempt's events are untouched


@pytest.mark.parametrize("mode,reason_class", [("pass", None), ("unsupported_effort", "unsupported-model-effort"),
                                               ("auth", "auth-quota-blocked"), ("transient", "transient"),
                                               ("no_write", "isolation-missing"), ("malformed", "conformance-failed")])
def test_probe_result_events_carry_the_outcome_and_reason_class(world, mode, reason_class):
    con = world.con()
    world.script(mode=mode)
    ensure(world, con, world.run(), attempt="A-out")
    result = events(con, attempt_id="A-out", kind="probe-result")[0]
    assert result["reason_class"] == reason_class and result["outcome"] == ("pass" if mode == "pass" else "fail")


def test_the_same_key_probed_twice_keeps_both_results(world):
    con = world.con()
    run = world.run()
    ensure(world, con, run, attempt="first")
    con.execute("UPDATE route_probes SET probed_at=?", ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),))
    world.script(mode="unsupported_effort")
    ensure(world, con, run, attempt="second", context=ctx(world, plan_version=4))
    results = events(con, kind="probe-result")
    assert [(e["attempt_id"], e["outcome"]) for e in results] == [("first", "pass"), ("second", "fail")]
    assert results[0]["created_at"] < results[1]["created_at"]
    cache = con.execute("SELECT attempt_id, result FROM route_probes").fetchall()
    assert [tuple(r) for r in cache] == [("second", "fail")]  # the cache row is overwritten, the events are not
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE route_discovery_events SET outcome='pass' WHERE attempt_id='second'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM route_discovery_events WHERE attempt_id='first'")


def test_a_standalone_manual_probe_is_audited_with_explicit_nulls(world):
    con = world.con()
    config = world.config(enabled=False)
    rec = route_probe.ensure(con, None, cand(), attempt_id="M1",
                             context={"origin": "manual", "reason": route_probe.MANUAL_REASON, "config": config})
    assert rec["result"] == "pass"
    for e in events(con, attempt_id="M1"):
        assert e["origin"] == "manual" and e["reason"] == "manual: office doctor --probe-route"
        for column in ("run_id", "plan_version", "task_id", "dispatch_id", "primary_route", "fallback_route"):
            assert e[column] is None, column
        assert e["policy_digest"] == route_policy.policy_digest(config)
        assert json.loads(e["fingerprint_json"])["invocation_model_id"] == "gpt-6.1-sol"
    assert kinds(con, "M1") == ["probe-reserved", "probe-result"]
    # a second manual probe of the same key is a cache hit event, not a launch
    hit = route_probe.ensure(con, None, cand(), attempt_id="M2", context={"origin": "manual", "config": config})
    assert hit["cached"] and len(world.launches()) == 1
    assert kinds(con, "M2") == ["probe-cache-hit"]
    assert events(con, attempt_id="M2")[0]["source_attempt_id"] == "M1"
    assert con.execute("SELECT run_id FROM route_probe_reservations WHERE id='M1'").fetchone()[0] is None


def test_a_bound_manual_probe_records_the_run_and_consumes_its_cap(world):
    con = world.con()
    run = world.run(plan_version=7, max_probes_per_run=1)
    ensure(world, con, run, attempt="B1", context={"origin": "manual", "reason": route_probe.MANUAL_REASON,
                                                   "config": run["policy"]})
    e = events(con, attempt_id="B1", kind="probe-result")[0]
    assert (e["origin"], e["run_id"], e["plan_version"]) == ("manual", "run-A", 7)
    refused = ensure(world, con, run, "codex/gpt-6.1-sol@low", context={"origin": "manual", "config": run["policy"]})
    assert refused == Refused("probe-cap")


def test_no_probe_path_writes_trust_or_overrides(world):
    from office import routing, scoring
    con = world.con()
    scoring.ensure_trust_schema(con)
    routing.ensure_override_schema(con)
    before = (con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0],
              con.execute("SELECT COUNT(*) FROM recorded_overrides").fetchone()[0])
    run = world.run()
    ensure(world, con, run)
    world.script(mode="unsupported_effort")
    ensure(world, con, run, "codex/gpt-6.1-sol@low")
    after = (con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM recorded_overrides").fetchone()[0])
    assert before == after == (0, 0)
    _, state = scoring.evaluate_trust_state(con, routing.candidate_id(cand()))
    assert state == "valid-unverified"


def test_the_probe_child_carries_no_office_identity(world, monkeypatch):
    seen = {}
    real = route_probe._spawn

    def spy(argv, cwd, env, stdin):
        seen.update(env)
        return real(argv, cwd, env, stdin)
    monkeypatch.setattr(route_probe, "_spawn", spy)
    monkeypatch.setenv("OFFICE_RUN_ID", "someones-run")
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "someones-dispatch")
    con = world.con()
    ensure(world, con, world.run(), attempt="A-env")
    assert "OFFICE_RUN_ID" not in seen and "OFFICE_DISPATCH_ID" not in seen
    assert seen["OFFICE_PROBE_ATTEMPT_ID"] == "A-env"


def test_the_probe_uses_the_adapters_own_worker_profile_unchanged(world, monkeypatch):
    captured = {}
    real = route_probe._spawn

    def spy(argv, cwd, env, stdin):
        captured.update(argv=argv, cwd=cwd)
        return real(argv, cwd, env, stdin)
    monkeypatch.setattr(route_probe, "_spawn", spy)
    ensure(world, world.con(), world.run())
    expected, _ = adapters.build_argv(adapters.load_all()["codex"], "worker", model="gpt-6.1-sol", effort="high",
                                      cwd=Path(captured["cwd"]))
    assert captured["argv"] == expected
    assert "-m" in expected and "gpt-6.1-sol" in expected and 'model_reasoning_effort="high"' in expected


# ------------------------------------------------------------------ cap arithmetic

def _audit(con, n):
    from office import route_learning
    route_learning.ensure_schema(con)
    for i in range(n):
        con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, disclosure_json, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"a{i:03d}", "r", f"T{i}", "executor" if i % 2 else "worker", "dispatch", "{}",
                     f"2026-10-01T00:00:{i:02d}+00:00"))


def _trial(con, i, role="executor"):
    con.execute("INSERT INTO route_trials(id, run_id, task_id, role, route, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (f"t{i}", "r", f"T{i}", role, "x", "launched",
                                            f"2026-10-01T00:00:{i:02d}+00:00", "x"))


def test_rolling_cap_cold_start_blocks_trials_until_enough_real_decisions_exist(world):
    con = world.con()
    s = route_policy.discovery_settings(world.config())
    cold = route_probe.rolling(con, s)  # no route_audit table at all: still a warm-up, not an error
    assert cold == {"used": 0, "max": 0, "window": 1, "percent": 15.0, "warmup": True}
    _audit(con, 0)
    for decisions, cap in ((5, 0), (6, 1), (12, 1), (13, 2), (18, 2), (19, 3), (20, 3), (40, 3)):
        con.execute("DELETE FROM route_audit")
        _audit(con, decisions)
        r = route_probe.rolling(con, s)
        assert r["max"] == cap and r["window"] == min(20, decisions + 1), (decisions, r)
        assert r["warmup"] is (cap == 0)
    # history is never forged: the empty table still reports the same warmup
    con.execute("DELETE FROM route_audit")
    assert route_probe.rolling(con, s)["warmup"] is True


def test_rolling_counts_only_trials_inside_the_window_and_only_builder_roles(world):
    con = world.con()
    s = route_policy.discovery_settings(world.config())
    _audit(con, 20)
    _trial(con, 1)       # inside the window (>= the oldest of the last 20)
    _trial(con, 2, role="planner")
    assert route_probe.rolling(con, s)["used"] == 1
    con.execute("DELETE FROM route_audit")
    _audit(con, 30)
    con.execute("UPDATE route_trials SET created_at='2026-09-01T00:00:00+00:00' WHERE id='t1'")
    assert route_probe.rolling(con, s)["used"] == 0  # older than the last 20 decisions


def test_allocation_reports_per_run_probe_and_trial_use(world):
    con = world.con()
    run = world.run()
    ensure(world, con, run)
    con.execute("INSERT INTO route_trials(id, run_id, role, route, status, created_at, updated_at) "
                "VALUES('t','run-A','executor','x','reserved','2026-10-01T00:00:00+00:00','x')")
    a = route_probe.allocation(con, "run-A", route_policy.discovery_settings(run["policy"]))
    assert a["probes"] == {"used": 1, "max": 2} and a["trials"] == {"used": 1, "max": 1}
    assert route_probe.allocation(con, None, route_policy.discovery_settings(run["policy"]))["probes"]["used"] == 0
