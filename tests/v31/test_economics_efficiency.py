"""Integration tests for #502 efficiency inspect, default-off and missing data."""
from conftest import approved_run
from office import economics, version


def _usage(env, *, turn, role, attribution="office", cost_kind="unknown", cost=None,
           read=None, write=None, fresh=None, output=None):
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        economics.ensure_schema(con)
        con.execute(
            "INSERT INTO usage_events(harness,session_id,turn_id,event_id,run_id,"
            "role,attribution,source,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,"
            "cost_usd,cost_kind,ledger_version,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("codex", "s-" + turn, turn, turn, run_id, role, attribution,
             "fixture", fresh, output, read, write, cost, cost_kind, 1, "2030-01-01T00:00:00Z"))
    finally:
        con.close()


def test_default_off_inspect_has_no_collection_side_effect(env):
    approved_run(env)
    con = env.con()
    try:
        before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert not economics.has_table(con)
    finally:
        con.close()
    code, data = env.ojson("inspect", "efficiency")
    assert code == 0
    assert data["data"]["enabled"] is False
    assert data["data"]["summary"]["rows"] == 0
    assert "cost collection disabled" in " ".join(data["lines"])
    assert "no measured Office-attributed turns" in " ".join(data["lines"])
    con = env.con()
    try:
        assert not economics.has_table(con)
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    finally:
        con.close()


def test_partial_usage_keeps_unknown_and_excludes_outside(env):
    approved_run(env)
    env.office("config", "economics.collect", "true", check=0)
    _usage(env, turn="root", role="root", cost_kind="actual", cost=1.25,
           fresh=100, output=20, read=5, write=0)
    _usage(env, turn="worker", role="executor", cost_kind="estimated-nominal", cost=0.50,
           fresh=None, output=10, read=None, write=None)
    _usage(env, turn="outside", role="root", attribution="outside", cost_kind="actual",
           cost=100, fresh=10000, output=2000, read=0, write=0)
    code, data = env.ojson("inspect", "efficiency")
    assert code == 0
    summary = data["data"]["summary"]
    assert summary["rows"] == 2 and summary["outside_rows"] == 1
    assert summary["actual_usd_observed"] == 1.25
    assert summary["actual_usd_coverage"] == "1/2"
    assert summary["root_share_of_actual_usd"] is None
    assert summary["token_coverage"] == "partial-or-unmeasured"
    assert summary["cache_coverage"] == {"cache_read_tokens": "1/2", "cache_write_tokens": "1/2"}
    assert "subscription quota are not billed dollars" in " ".join(data["lines"])


def test_non_object_shadow_event_payload_does_not_break_efficiency_inspect(env):
    approved_run(env)
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        for payload in ("null", "[]"):
            con.execute("INSERT INTO events(run_id,kind,audience,summary,payload_json,office_version,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (run_id, "delegation.shadow", "orchestrator", "test", payload,
                         version.current(), "2030-01-01T00:00:00Z"))
    finally:
        con.close()
    code, data = env.ojson("inspect", "efficiency")
    assert code == 0
    assert data["data"]["delegation_shadow"] == []
    assert "no fan-out observation recorded" in " ".join(data["lines"])
