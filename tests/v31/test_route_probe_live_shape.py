"""`office doctor --probe-route` end to end through the CLI (#494 T2).

The harness is the scripted fake in tests/fixtures/route_probe, run as a real
subprocess under the probe's own process group, with isolated Office homes. No
real model is called.
"""
import json
import sys
from pathlib import Path

import pytest

from conftest import start_inline

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests" / "fixtures" / "route_probe" / "fake_harness.py"
ROUTE = "codex/gpt-6.1-sol@high"


@pytest.fixture
def probe_env(env, monkeypatch):
    """`env` with its `codex` replaced by the probe fake and a launch log."""
    codex = env.bin / "codex"
    codex.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
    codex.chmod(0o755)
    log = env.tmp / "launches.log"
    from office import route_probe
    if route_probe.write_boundary_reason() is not None:  # no OS boundary here: the launch path still runs
        monkeypatch.setattr(route_probe, "write_boundary_reason", lambda: None)
        monkeypatch.setattr(route_probe, "_boundary_argv", lambda ws: [])
    monkeypatch.setattr(route_probe, "_EXTRA_WRITABLE", (str(log.parent.resolve() / log.name),))
    env.log = log
    env.set = lambda **fields: monkeypatch.setenv("FAKE_PROBE", json.dumps({"count_file": str(log), **fields}))
    env.launches = lambda: log.read_text().count("launch ") if log.exists() else 0
    env.set()
    return env


def events(env, **where):
    con = env.con()
    try:
        sql = "SELECT * FROM route_discovery_events" + "".join(f" {'WHERE' if i == 0 else 'AND'} {k}=?"
                                                              for i, k in enumerate(where))
        return [dict(r) for r in con.execute(sql + " ORDER BY seq", tuple(where.values()))]
    finally:
        con.close()


def test_a_manual_probe_prints_the_record_and_audits_an_unbound_attempt(probe_env):
    code, out = probe_env.office("doctor", "--probe-route", ROUTE)
    assert code == 0, out
    assert "probe codex/gpt-6.1-sol@high: pass" in out and "fresh probe" in out and "no bound run" in out
    assert "not adapter trust" in out
    assert probe_env.launches() == 1
    rows = events(probe_env)
    assert [e["kind"] for e in rows] == ["probe-reserved", "probe-result"]
    for e in rows:
        assert e["origin"] == "manual" and e["reason"] == "manual: office doctor --probe-route"
        assert all(e[c] is None for c in ("run_id", "plan_version", "task_id", "dispatch_id", "primary_route",
                                          "fallback_route"))
        assert e["policy_digest"]
    assert len({e["attempt_id"] for e in rows}) == 1


def test_a_repeat_is_a_cached_record_and_a_cache_hit_event(probe_env):
    assert probe_env.office("doctor", "--probe-route", ROUTE)[0] == 0
    code, data = probe_env.ojson("doctor", "--probe-route", ROUTE)
    assert code == 0 and data["data"]["probe"]["cached"] is True
    assert probe_env.launches() == 1
    hit = events(probe_env, kind="probe-cache-hit")
    first = events(probe_env, kind="probe-reserved")
    assert len(hit) == 1 and hit[0]["source_attempt_id"] == first[0]["attempt_id"]


def test_a_failed_probe_exits_nonzero_with_its_reason_and_spares_sibling_efforts(probe_env):
    probe_env.set(modes={"max": "unsupported_effort", "*": "pass"})
    code, out = probe_env.office("doctor", "--probe-route", "codex/gpt-6.1-sol@max")
    assert code == 1 and "fail (unsupported-model-effort)" in out
    code, out = probe_env.office("doctor", "--probe-route", ROUTE)
    assert code == 0, out
    results = {e["candidate_route"].rsplit("@", 1)[1]: e["reason_class"] for e in events(probe_env, kind="probe-result")}
    assert results == {"max": "unsupported-model-effort", "high": None}


def test_a_user_denied_route_is_never_probed_through_the_cli(probe_env):
    (probe_env.tmp / "user-config.yaml").write_text(
        "routing:\n  user_policy:\n    denied_models: [codex/gpt-6.1-sol@high]\n")
    code, out = probe_env.office("doctor", "--probe-route", ROUTE)
    assert code == 1 and "refused (route-denied" in out and "nothing was launched" in out
    assert probe_env.launches() == 0
    assert [e["kind"] for e in events(probe_env)] == ["probe-refused"]
    assert probe_env.office("doctor", "--probe-route", "codex/gpt-6.1-sol@low")[0] == 0


def test_archived_unsupported_and_malformed_routes_are_refused_before_any_launch(probe_env):
    code, out = probe_env.office("doctor", "--probe-route", "claude/claude-fable-5-1@max")
    assert code == 2 and "archived" in out
    code, out = probe_env.office("doctor", "--probe-route", "codex/gpt-6-luna@none")
    assert code == 1 and "refused (not-eligible" in out
    code, out = probe_env.office("doctor", "--probe-route", "codex/gpt-6.1-sol")
    assert code == 2 and "expected <harness>/<model>@<effort>" in out
    assert probe_env.launches() == 0


def test_a_named_run_is_bound_and_its_probe_cap_holds(probe_env):
    start_inline(probe_env)
    con = probe_env.con()
    run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    plan_version = con.execute("SELECT plan_version FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    con.close()
    for effort in ("low", "medium"):
        code, out = probe_env.office("--run", run_id, "doctor", "--probe-route", f"codex/gpt-6.1-sol@{effort}")
        assert code == 0 and f"run {run_id[:8]}" in out, out
    code, out = probe_env.office("--run", run_id, "doctor", "--probe-route", "codex/gpt-6.1-sol@xhigh")
    assert code == 1 and "refused (probe-cap" in out
    assert probe_env.launches() == 2
    bound = events(probe_env, run_id=run_id, kind="probe-result")
    assert len(bound) == 2 and {e["plan_version"] for e in bound} == {plan_version}
    refused = events(probe_env, kind="probe-refused")[0]
    assert json.loads(refused["allocation_json"])["probes"]["used"] == 2


def test_an_unnamed_run_is_not_silently_bound(probe_env):
    start_inline(probe_env)
    assert probe_env.office("doctor", "--probe-route", ROUTE)[0] == 0
    assert events(probe_env, kind="probe-result")[0]["run_id"] is None


def test_the_next_office_command_expires_a_dead_owners_reservation(probe_env):
    from office import route_policy, route_probe
    cand = route_probe.candidate_from_spec(ROUTE)
    con = probe_env.con()
    held = route_probe.reserve(con, None, cand, attempt_id="A-dead", context={"origin": "manual", "reason": "test"})
    assert held["reserved"]
    route_probe._release("A-dead", forget=False)  # its owner died
    con.close()
    code, out = probe_env.office("list")
    assert code == 0, out
    con = probe_env.con()
    assert con.execute("SELECT status FROM route_probe_reservations WHERE id='A-dead'").fetchone()[0] == "expired"
    con.close()
    assert [e["kind"] for e in events(probe_env, attempt_id="A-dead")] == ["probe-reserved", "probe-expired"]
    assert probe_env.launches() == 0
    assert route_policy.POLICY_VERSION == events(probe_env, attempt_id="A-dead")[0]["policy_version"]


def test_a_probe_launch_error_never_leaves_a_pending_reservation(probe_env):
    probe_env.set(mode="hang")
    probe_env.office("config", "routing.discovery.probe_timeout_s", "1", "--user", check=0)
    code, out = probe_env.office("doctor", "--probe-route", ROUTE)
    assert code == 1 and "fail (transient)" in out
    con = probe_env.con()
    assert con.execute("SELECT COUNT(*) FROM route_probe_reservations WHERE status='reserved'").fetchone()[0] == 0
    con.close()
