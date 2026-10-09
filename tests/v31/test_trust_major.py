"""Trust keys on the harness major version, and the shipped baseline seeds fresh installs."""
import sqlite3

from office import routing, scoring


def _db(tmp_path):
    con = sqlite3.connect(str(tmp_path / "runs.db"))
    con.execute("CREATE TABLE dispatches(id TEXT, attribution TEXT, triple TEXT, money_estimate REAL, money_actual REAL)")
    con.execute("CREATE TABLE outcome_labels(dispatch_id TEXT, label TEXT, primary_attribution TEXT, evidence_hash TEXT)")
    con.execute("CREATE TABLE findings(dispatch_id TEXT, status TEXT, severity TEXT, evidence_hash TEXT)")
    scoring.ensure_trust_schema(con)
    return con


def test_harness_major():
    assert scoring.harness_major("1.2.12") == "1"
    assert scoring.harness_major("0.157.1") == "0"
    assert scoring.harness_major("v2.1.283") == "2"
    assert scoring.harness_major("local") == "local"
    assert scoring.harness_major(None) == "unknown"


def test_candidate_id_uses_major():
    c = {"harness": "agy", "harness_version": "1.2.12", "model_id": "gemini-3.8-flash", "effort": "medium"}
    assert routing.candidate_id(c) == "agy@1/gemini-3.8-flash@medium"


def test_act_under_old_point_release_carries_to_new_one(tmp_path):
    con = _db(tmp_path)
    con.execute("INSERT INTO adapter_trust_acts VALUES ('a','x@7.0.1/m@high','proven','rico','granted in run r1','r1','2026-01-01T00:00:00+00:00')")
    assert scoring.evaluate_trust_state(con, "x@7.4.0/m@high") == (0, "proven")
    assert scoring.evaluate_trust_state(con, "x@8.0.0/m@high") == (0, "valid-unverified")


def test_non_numeric_label_does_not_inherit(tmp_path):
    con = _db(tmp_path)
    con.execute("INSERT INTO adapter_trust_acts VALUES ('a','x@cli/m@high','proven','rico','granted in run r1','r1','2026-01-01T00:00:00+00:00')")
    assert scoring.evaluate_trust_state(con, "x@7/m@high")[1] == "valid-unverified"


def test_baseline_seeds_proven_and_local_act_wins(tmp_path):
    # Derive from the shipped baseline so catalog model-id changes do not break this test.
    triple = next(t for t, state in sorted(scoring.trust_baseline().items()) if t.startswith("claude@2/") and state == "proven")
    assert scoring.trust_baseline()[triple] == "proven"
    con = _db(tmp_path)
    assert scoring.evaluate_trust_state(con, triple.replace("claude@2/", "claude@2.9.0/")) == (0, "proven")
    con.execute("INSERT INTO adapter_trust_acts VALUES ('a',?,'valid-unverified','rico','demoted locally after review','r1','2026-10-01T00:00:00+00:00')", (triple,))
    assert scoring.evaluate_trust_state(con, triple) == (0, "valid-unverified")


def test_standing_failure_beats_baseline(tmp_path):
    con = _db(tmp_path)
    con.execute("INSERT INTO dispatches VALUES ('d1','adapter','claude@2.1.300/claude-sonnet-5@high',0,0)")
    con.execute("INSERT INTO outcome_labels VALUES ('d1','recurrence_failure','adapter',?)", ("sha256:" + "a" * 64,))
    assert scoring.evaluate_trust_state(con, "claude@2/claude-sonnet-5@high") == (1, "quarantined")
