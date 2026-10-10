"""Pure discovery status, user policy and additive schema contracts."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import jsonschema
import pytest
import yaml

from office import config, db, paths, route_policy as policy


def eligible(**extra):
    return {"model_id": "new-model", "invocation_model_id": "new-model", "invocation_harness": "codex",
            "effort": "high", "dispatchable": False, "discovery": "eligible",
            "discovery_reason": "exact effort pending", **extra}


@pytest.mark.parametrize("row,probe,status,can_probe", [
    ({}, None, "available", False),
    ({"dispatchable": True}, {"result": "fail"}, "available", False),
    (eligible(), None, "discovered-unconfirmed", True),
    (eligible(), {"result": "pending"}, "probe-pending", True),
    (eligible(), {"result": "pass"}, "probe-passed", True),
    (eligible(), {"result": "fail", "reason_class": "unsupported-model-effort"}, "confirmed-unsupported", True),
    (eligible(), {"result": "fail", "reason_class": "conformance-failed"}, "confirmed-unsupported", True),
    (eligible(), {"result": "fail", "reason_class": "auth-quota-blocked"}, "temporarily-unavailable", True),
    (eligible(), {"result": "fail", "reason_class": "transient"}, "temporarily-unavailable", True),
    (eligible(), {"result": "fail", "reason_class": "isolation-missing"}, "temporarily-unavailable", True),
    (eligible(), {"result": "unknown"}, "discovered-unconfirmed", True),
    (eligible(discovery="ineligible"), {"result": "pass"}, "discovered-unconfirmed", False),
    (eligible(invocation_model_id=None), {"result": "pass"}, "discovered-unconfirmed", False),
    ({"dispatchable": False, "invocation_source": "benchmark only"}, None, "discovered-unconfirmed", False),
])
def test_status_derivations(row, probe, status, can_probe):
    result = policy.row_status(row, probe)
    assert result["status"] == status and result["discovery_eligible"] is can_probe
    assert result["reason"]
    if probe and probe.get("reason_class") and can_probe:
        assert probe["reason_class"] in result["reason"]


@pytest.mark.parametrize("status", policy.ROUTE_STATUSES)
def test_alias_inherits_target_status_with_target_reason(status):
    target = eligible(route_status=status, status_reason="exact target status", dispatchable=status == "available")
    result = policy.alias_status({"model_id": "alias", "dispatchable": True}, target)
    assert result["status"] == status
    assert "target new-model" in result["reason"]
    assert result["discovery_eligible"] is (status != "available")


def test_alias_cannot_enable_disabled_target():
    result = policy.alias_status({"model_id": "alias"}, eligible(discovery="ineligible"))
    assert result["status"] == "discovered-unconfirmed"
    assert result["discovery_eligible"] is False
    assert "new-model" in result["reason"]


@pytest.mark.parametrize("spec,matches", [
    ("new-model", True), ("codex/new-model", True), ("codex/new-model@high", True),
    ("codex/new-model@low", False), ("claude/new-model", False), ("harness:codex", True),
    ("harness:claude", False), ("alias", True), ("other", False),
])
def test_denial_matches_exact_route_and_alias_target(spec, matches):
    rules = {"denied": [spec], "sources": {"denied_models": "user"}}
    cand = eligible(model_id="alias", alias_resolved_to="new-model")
    reason = policy.is_denied(cand, rules)
    assert bool(reason) is matches
    if reason:
        assert "user routing.user_policy.denied_models" in reason


def test_overkill_role_and_size_scope():
    rules = {"overkill": [{"route": "codex/new-model@high", "roles": ["worker"], "size_classes": ["S"]}],
             "sources": {"overkill_rules": "repo"}}
    assert "repo" in policy.is_overkill(eligible(), "worker", "S", rules)
    assert policy.is_overkill(eligible(), "executor", "S", rules) is None
    assert policy.is_overkill(eligible(), "worker", "M", rules) is None
    assert policy.is_overkill(eligible(), "worker", None, rules) is None
    assert policy.is_overkill(eligible(effort="low"), "worker", "S", rules) is None
    assert policy.is_denied(eligible(), rules) is None
    assert policy.is_overkill(eligible(), "worker", "S", {"overkill": [{"route": "new-model", "roles": []}]}) is None
    assert policy.is_overkill(eligible(), "executor", None, {"overkill": [{"route": "new-model"}]})


def test_shipped_discovery_catalog_exactly_matches_locked_choices():
    rows = yaml.safe_load((paths.resources_root() / "catalog/seed.yaml").read_text())["models"]
    actual = {(r["invocation_harness"], r["model_id"], r["effort"]) for r in rows if r.get("discovery") == "eligible"}
    expected = {(h, m, e) for h, m in (("codex", "gpt-6.1-sol"), ("claude", "claude-haiku-5-5"))
                for e in ("low", "medium", "high", "xhigh", "max")}
    expected.add(("agy", "gemini-3.8-flash", "high"))
    assert actual == expected
    for row in rows:
        if row.get("dispatchable") is False:
            assert policy.row_status(row)["status"] == "discovered-unconfirmed"
        if row.get("discovery") == "eligible":
            assert row["discovery_reason"] and row["invocation_model_id"]


def test_missing_invocation_evidence_is_unknown_and_not_probe_eligible():
    result = policy.row_status({"dispatchable": False})
    assert result == {"status": "discovered-unconfirmed",
                      "reason": "catalog invocation unconfirmed", "discovery_eligible": False}


def _columns(con):
    return {row[0]: list(con.execute(f"PRAGMA table_info({row[0]})"))
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize("historical", [False, True])
def test_additive_migration_fresh_and_prechange_copy(tmp_path, historical):
    con = sqlite3.connect(tmp_path / "fresh.db", isolation_level=None)
    old_columns = {}
    if historical:
        # Build the actual base schema in a separate database, then migrate its copy.
        original = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
        base = subprocess.check_output(["git", "show", "eaf155a:src/office/db.py"], text=True)
        namespace = {"__name__": "old_office_db"}
        exec(compile(base, "base_db.py", "exec"), namespace)
        namespace["migrate"](original)
        old_columns = _columns(original)
        original.backup(con)
        original.close()
    db.migrate(con)
    columns = _columns(con)
    # v12 (T1) appended three nullable evidence columns; every other column of a pre-existing table is unchanged.
    v12 = {"predecessor_dispatch_id", "first_executor_dispatch_id", "accepted_producer_dispatch_id"}
    assert all([col for col in columns[table] if col[1] not in v12] == info for table, info in old_columns.items())
    assert {"route_probes", "route_probe_reservations", "route_trials", "route_discovery_events"} <= columns.keys()
    expected = {
        "route_probes": "key harness harness_version adapter_hash profile invocation_model_id effort result reason_class detail probed_at run_id dispatch_id attempt_id",
        "route_probe_reservations": "id run_id probe_key dispatch_id task_id status claim_token reserved_at finished_at",
        "route_trials": "id run_id task_id dispatch_id role route probe_key fallback_route policy_digest reason status outcome created_at updated_at",
        "route_discovery_events": "seq id attempt_id kind origin run_id plan_version task_id dispatch_id role policy_digest policy_version probe_key fingerprint_json candidate_route primary_route fallback_route reason probe_freshness source_attempt_id allocation_json outcome reason_class detail created_at",
    }
    for table, names in expected.items():
        assert [col[1] for col in columns[table]] == names.split()
    db.migrate(con)
    assert _columns(con) == columns
    con.close()


def test_schema_rejects_concurrent_reservations_and_preserves_attempt_history(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    con.execute("INSERT INTO route_probe_reservations(id,run_id,probe_key,status) VALUES('1','run','key','reserved')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO route_probe_reservations(id,run_id,probe_key,status) VALUES('2','other','key','reserved')")
    con.execute("UPDATE route_probe_reservations SET status='expired' WHERE id='1'")
    con.execute("INSERT INTO route_probe_reservations(id,run_id,probe_key,status) VALUES('2','run','key','reserved')")
    assert con.execute("SELECT count(*) FROM route_probe_reservations WHERE run_id='run'").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO route_probes(key,result) VALUES('bad','maybe')")
    con.execute("INSERT INTO route_probes(key,result,reason_class) VALUES('ok','pass',NULL)")
    con.close()


def test_candidate_schema_accepts_discovery_fields():
    schema = json.loads((paths.resources_root() / "schemas/routing-candidate.schema.json").read_text())
    fields = {k: schema["properties"][k] for k in ("route_status", "status_reason", "discovery", "probe_key")}
    for status in policy.ROUTE_STATUSES:
        jsonschema.validate({"route_status": status, "status_reason": "exact", "discovery": True, "probe_key": None},
                            {"type": "object", "properties": fields})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"route_status": "trust-granted"}, {"properties": fields})


def test_migration_repairs_missing_reservation_constraint_at_current_version(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    con.execute("DROP INDEX route_probe_inflight")
    db.migrate(con)
    con.execute("INSERT INTO route_probe_reservations(id,probe_key,status) VALUES('1','key','reserved')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO route_probe_reservations(id,probe_key,status) VALUES('2','key','reserved')")
    con.close()


@pytest.mark.parametrize("annotation", policy.ROUTE_STATUSES)
def test_alias_cannot_launder_disabled_target_status_metadata(annotation):
    target = eligible(discovery="ineligible", route_status=annotation)
    result = policy.alias_status({"model_id": "alias"}, target)
    assert result["status"] == "discovered-unconfirmed"
    assert result["discovery_eligible"] is False
    target = eligible(route_status="available")
    assert policy.alias_status({"model_id": "alias"}, target)["status"] == "discovered-unconfirmed"


def event_fields(**extra):
    return {"kind": "probe-reserved", "attempt_id": policy.new_attempt_id(), "origin": "manual",
            "policy_digest": "sha256:policy", "probe_key": "exact-key", "reason": "manual isolated probe",
            "candidate_route": "codex/new-model@high", "fingerprint_json": {
                "harness": "codex", "harness_version": "1.0", "adapter_hash": "sha256:adapter",
                "profile": "isolated", "invocation_model_id": "new-model", "effort": "high"},
            "probe_freshness": "none", "allocation_json": {
                "probes": {"used": 0, "max": 2}, "trials": {"used": 0, "max": 1},
                "rolling": {"used": 0, "max": 15}}, **extra}


def test_discovery_events_are_append_only_and_survive_migration(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    fields = event_fields(run_id=None, plan_version=None, task_id=None, dispatch_id=None, role=None,
                          primary_route=None, fallback_route=None)
    with db.transaction(con):
        event_id = policy.record_event(con, **fields)
    before = dict(con.execute("SELECT * FROM route_discovery_events WHERE id=?", (event_id,)).fetchone())
    assert before["origin"] == "manual" and before["run_id"] is None and before["plan_version"] is None
    assert json.loads(before["fingerprint_json"])["effort"] == "high"
    assert before["policy_version"] == policy.POLICY_VERSION
    for statement in ("UPDATE route_discovery_events SET reason='edited'", "DELETE FROM route_discovery_events",
                      "INSERT OR REPLACE INTO route_discovery_events SELECT * FROM route_discovery_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute(statement)
    db.migrate(con)
    assert dict(con.execute("SELECT * FROM route_discovery_events WHERE id=?", (event_id,)).fetchone()) == before
    con.execute("DROP TRIGGER route_discovery_no_delete")
    db.migrate(con)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM route_discovery_events")
    con.close()


@pytest.mark.parametrize("key,value", [
    ("policy_digest", None), ("policy_digest", ""), ("probe_key", None), ("reason", " "),
    ("attempt_id", ""), ("kind", "unknown"), ("origin", "unknown"), ("candidate_route", None),
    ("fingerprint_json", None), ("fingerprint_json", {}), ("probe_freshness", "invented"),
    ("probe_freshness", None), ("allocation_json", None), ("allocation_json", {}), ("allocation_json", "{"),
    ("sql_column);DELETE FROM runs;--", "value"),
])
def test_discovery_event_rejects_missing_or_invalid_context(tmp_path, key, value):
    con = db.connect(tmp_path / "runs.db")
    fields = event_fields(**{key: value})
    with db.transaction(con), pytest.raises(ValueError):
        policy.record_event(con, **fields)
    assert con.execute("SELECT count(*) FROM route_discovery_events").fetchone()[0] == 0
    con.close()


def test_event_requires_transaction_and_rolls_back_with_state_change(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    fields = event_fields()
    with pytest.raises(ValueError, match="transaction"):
        policy.record_event(con, **fields)
    with pytest.raises(RuntimeError):
        with db.transaction(con):
            policy.record_event(con, **fields)
            con.execute("INSERT INTO route_probe_reservations(id,probe_key,status) VALUES(?,?,'reserved')",
                        (fields["attempt_id"], fields["probe_key"]))
            raise RuntimeError("rollback state and audit together")
    assert con.execute("SELECT count(*) FROM route_discovery_events").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM route_probe_reservations").fetchone()[0] == 0
    con.close()


def test_trial_terminal_outcome_is_unique_and_cache_hits_link_attempts(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    fields = event_fields(kind="trial-accepted", origin="job", outcome="accepted")
    with db.transaction(con):
        policy.record_event(con, **fields)
    with pytest.raises(sqlite3.IntegrityError), db.transaction(con):
        policy.record_event(con, **{**fields, "kind": "trial-rejected"})
    with db.transaction(con), pytest.raises(ValueError, match="source_attempt_id"):
        policy.record_event(con, **event_fields(kind="probe-cache-hit"))
    with db.transaction(con):
        cache_id = policy.record_event(con, **event_fields(kind="probe-cache-hit", source_attempt_id=fields["attempt_id"],
                                                          probe_freshness="cached-fresh", allocation_json={
                                                              "probes": {"used": 1, "max": 2}, "trials": {"used": 0, "max": 1},
                                                              "rolling": {"used": 0, "max": 15}}))
    cache = dict(con.execute("SELECT * FROM route_discovery_events WHERE id=?", (cache_id,)).fetchone())
    assert cache["source_attempt_id"] == fields["attempt_id"]
    assert json.loads(cache["allocation_json"])["probes"] == {"used": 1, "max": 2}
    con.close()


def test_event_validates_serialized_cap_snapshot_and_required_audit_context(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    with db.transaction(con):
        for allocation in ("{", "[]", "{}"):
            with pytest.raises(ValueError):
                policy.record_event(con, **event_fields(allocation_json=allocation))
        fields = event_fields()
        fields["allocation_json"] = json.dumps(fields["allocation_json"])
        assert policy.record_event(con, **fields)
    assert con.execute("SELECT count(*) FROM route_discovery_events").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("missing", ["allocation_json", "probe_freshness"])
def test_event_requires_cap_snapshot_and_freshness(tmp_path, missing):
    con = db.connect(tmp_path / "runs.db")
    fields = event_fields()
    del fields[missing]
    with db.transaction(con), pytest.raises(ValueError):
        policy.record_event(con, **fields)
    assert con.execute("SELECT count(*) FROM route_discovery_events").fetchone()[0] == 0
    con.close()
