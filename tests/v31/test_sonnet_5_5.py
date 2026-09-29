"""Sonnet 5.5 is a dispatchable executor route, and the `sonnet` alias follows it."""
import sqlite3

from office import candidates, config, scoring

INDEX = "Artificial Analysis Intelligence Index v4.3.2"


def _rows(model_id, effort="high"):
    return [r for r in candidates.catalog_rows()
            if r.get("model_id") == model_id and r.get("effort") == effort]


def test_sonnet_5_5_high_row_is_dispatchable_with_a_sourced_score():
    [row] = _rows("claude-sonnet-5-5")
    assert row["invocation_harness"] == "claude"
    assert row["invocation_model_id"] == "claude-sonnet-5-5"
    assert row.get("dispatchable") is not False
    assert str(row["invocation_source"]).startswith("local-evidence:")
    assert row["benchmark_indexes"][INDEX] == 47
    assert row["source_snapshot_metadata"]["url"].endswith("/claude-sonnet-5-5-high")


def test_sonnet_5_5_high_clears_the_executor_floor():
    floor = config.load_yaml(config.default_config_path())["roles"]["executor"]["floor"]
    [row] = _rows("claude-sonnet-5-5")
    ok, reason = scoring.evaluate_capability_floor(row, floor)
    assert ok, reason


def test_sonnet_alias_high_now_invokes_sonnet_5_5():
    [alias] = _rows("sonnet")
    assert alias["alias_resolved_to"] == "claude-sonnet-5-5"
    assert alias["invocation_model_id"] == "claude-sonnet-5-5"
    assert alias["benchmark_indexes"][INDEX] == 47


def test_sonnet_5_5_high_is_trusted_as_the_default_executor(tmp_path):
    # Maintainer grant 2026-09-29: Sonnet 5.5 high is the default executor.
    assert scoring.trust_baseline()["claude@2/claude-sonnet-5-5@high"] == "proven"
    con = sqlite3.connect(str(tmp_path / "runs.db"))
    con.execute("CREATE TABLE dispatches(id TEXT, attribution TEXT, triple TEXT, money_estimate REAL, money_actual REAL)")
    con.execute("CREATE TABLE outcome_labels(dispatch_id TEXT, label TEXT, primary_attribution TEXT, evidence_hash TEXT)")
    con.execute("CREATE TABLE findings(dispatch_id TEXT, status TEXT, severity TEXT, evidence_hash TEXT)")
    scoring.ensure_trust_schema(con)
    assert scoring.evaluate_trust_state(con, "claude@2.1.284/claude-sonnet-5-5@high") == (0, "proven")
    # Only high was granted.
    assert scoring.evaluate_trust_state(con, "claude@2.1.284/claude-sonnet-5-5@medium")[1] == "valid-unverified"
