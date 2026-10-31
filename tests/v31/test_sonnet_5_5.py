"""Sonnet 5.5 high is the maintainer-granted default executor in the trust baseline."""
import sqlite3

from office import scoring


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
