"""Unit tests for scripts/office_scoring.py's capability-floor, local-reward and
trust-act write-path pieces of the Task T2B contract (docs/v3-runtime-contracts.md
section 7, deliverables F2/F3/F4). The adapter-trust invariant itself (§7.1) is proven
by tests/test_trust_conformance.py, which this file does not duplicate.
"""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('office_scoring', ROOT / 'scripts' / 'office_scoring.py')
scoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring)

_rt_spec = importlib.util.spec_from_file_location('office_runtime', ROOT / 'scripts' / 'office_runtime.py')
office_runtime = importlib.util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(office_runtime)


def _con():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE dispatches(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, holder_id TEXT, triple TEXT, invocation_model_id TEXT, selection_reason TEXT, task_shape TEXT, size_class TEXT, started_at TEXT, ended_at TEXT, money_estimate REAL, money_actual REAL, quota_estimate REAL, quota_delta REAL, wall_clock_seconds REAL, attribution TEXT, outcome TEXT);
    CREATE TABLE findings(id TEXT PRIMARY KEY, dispatch_id TEXT, reviewer_dispatch_id TEXT, status TEXT, severity TEXT, summary TEXT, evidence_hash TEXT, created_at TEXT);
    CREATE TABLE outcome_labels(id TEXT PRIMARY KEY, dispatch_id TEXT, label TEXT, primary_attribution TEXT, contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT);
    """)
    return con


def _dispatch(con, did, triple="t@local/m@medium", money_estimate=10.0, money_actual=10.0):
    con.execute(
        "INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, "run-1", "executor", "h1", triple, "m1", "r", "shapeA", "M", "t", "t",
         money_estimate, money_actual, 0, 0, 0, None, None),
    )


def _label(con, did, label, contributing_attributions=None, label_id=None):
    con.execute(
        "INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
        (label_id or f"lab-{did}-{label}", did, label, "model", contributing_attributions,
         "2026-09-15T00:00:00Z", "sha256:" + "a" * 64),
    )


VALID_HASH = "sha256:" + "b" * 64


class CapabilityFloorTests(unittest.TestCase):
    def test_no_floor_always_passes(self):
        passed, reason = scoring.evaluate_capability_floor({"effort": "low"}, None)
        self.assertTrue(passed)
        self.assertIsNone(reason)

    def test_effort_meets_floor_passes(self):
        passed, reason = scoring.evaluate_capability_floor(
            {"effort": "high", "invocation_source": "local-evidence:x"},
            {"min_effort": "medium", "allowed_sources": ["local-evidence:"]},
        )
        self.assertTrue(passed)

    def test_effort_below_floor_rejects_with_reason(self):
        passed, reason = scoring.evaluate_capability_floor(
            {"effort": "low"}, {"min_effort": "high"},
        )
        self.assertFalse(passed)
        self.assertIn("effort below role floor", reason)
        self.assertIn("low", reason)
        self.assertIn("high", reason)

    def test_missing_effort_field_fails_closed_and_names_field(self):
        """§7.2.2 rule 4: unknown must never pass, and the reason must name the field."""
        passed, reason = scoring.evaluate_capability_floor(
            {}, {"min_effort": "low"},
        )
        self.assertFalse(passed)
        self.assertIn("effort", reason)

    def test_disallowed_invocation_source_rejects(self):
        passed, reason = scoring.evaluate_capability_floor(
            {"effort": "high", "invocation_source": "unverified:x"},
            {"allowed_sources": ["local-evidence:", "documented:"]},
        )
        self.assertFalse(passed)
        self.assertIn("invocation_source", reason)

    def test_missing_invocation_source_fails_closed(self):
        passed, reason = scoring.evaluate_capability_floor(
            {"effort": "high"}, {"allowed_sources": ["local-evidence:"]},
        )
        self.assertFalse(passed)

    def test_missing_benchmark_index_fails_closed_and_names_field(self):
        floor = {"min_benchmark_index": {"index_name": "AAII-v4.3", "min_score": 50}}
        passed, reason = scoring.evaluate_capability_floor({"effort": "high"}, floor)
        self.assertFalse(passed)
        self.assertIn("benchmark_indexes.AAII-v4.3", reason)

    def test_benchmark_index_below_score_rejects(self):
        floor = {"min_benchmark_index": {"index_name": "AAII-v4.3", "min_score": 50}}
        candidate = {"effort": "high", "benchmark_indexes": {"AAII-v4.3": 10}}
        passed, reason = scoring.evaluate_capability_floor(candidate, floor)
        self.assertFalse(passed)

    def test_benchmark_index_meeting_score_passes(self):
        floor = {"min_benchmark_index": {"index_name": "AAII-v4.3", "min_score": 50}}
        candidate = {"effort": "high", "benchmark_indexes": {"AAII-v4.3": 75}}
        passed, reason = scoring.evaluate_capability_floor(candidate, floor)
        self.assertTrue(passed)


class LocalRewardTests(unittest.TestCase):
    def test_no_dispatches_is_unknown_not_zero(self):
        con = _con()
        self.assertIsNone(scoring.compute_local_reward(con, "nope@local/m@medium"))

    def test_only_pending_label_is_unknown(self):
        con = _con()
        _dispatch(con, "d1")
        _label(con, "d1", "pending")
        self.assertIsNone(scoring.compute_local_reward(con, "t@local/m@medium"))

    def test_success_narrative_default_reconstructs_point_eight(self):
        con = _con()
        _dispatch(con, "d1", money_estimate=10.0, money_actual=10.0)
        _label(con, "d1", "verified_no_observed_failure")
        self.assertAlmostEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.8)

    def test_partial_success_narrative_reconstructs_point_four(self):
        con = _con()
        _dispatch(con, "d1", money_estimate=10.0, money_actual=10.0)
        _label(con, "d1", "verified_no_observed_failure", contributing_attributions='["narrative:partial_success"]')
        self.assertAlmostEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.4)

    def test_environment_failure_is_neutral_zero_when_actually_labeled(self):
        """A measured 0.0 (environment_failure) must be produced when evidence says so --
        distinguishing it from the unknown case is the entire point of §7.4.2."""
        con = _con()
        _dispatch(con, "d1", money_estimate=10.0, money_actual=10.0)
        _label(con, "d1", "environment_failure")
        self.assertEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.0)

    def test_revert_failure_is_unscored_not_a_measured_value(self):
        con = _con()
        _dispatch(con, "d1")
        _label(con, "d1", "revert_failure")
        self.assertIsNone(scoring.compute_local_reward(con, "t@local/m@medium"))

    def test_findings_penalty_reduces_reward(self):
        con = _con()
        _dispatch(con, "d1", money_estimate=10.0, money_actual=10.0)
        _label(con, "d1", "verified_no_observed_failure")
        con.execute(
            "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
            ("f1", "d1", "rev-1", "accepted-material", "high", "minor issue", VALID_HASH, "2026-09-15T00:00:00Z"),
        )
        # 0.8 base - 0.10 (one high finding) = 0.70
        self.assertAlmostEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.70)

    def test_efficiency_modifier_reflects_underspend(self):
        con = _con()
        _dispatch(con, "d1", money_estimate=100.0, money_actual=1.0)
        _label(con, "d1", "verified_no_observed_failure")
        # 0.8 base + 0.10 * (1 - 1/100) = 0.8 + 0.099 = 0.899
        self.assertAlmostEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.899)

    def test_efficiency_modifier_clamped_at_point_one(self):
        con = _con()
        _dispatch(con, "d1", money_estimate=100.0, money_actual=0.0)
        _label(con, "d1", "verified_no_observed_failure")
        # 0.10 * (1 - 0/100) = 0.10, already at the clamp boundary
        self.assertAlmostEqual(scoring.compute_local_reward(con, "t@local/m@medium"), 0.9)

    def test_unknown_reward_does_not_rank_as_measured_zero(self):
        self.assertNotEqual(scoring.reward_sort_key(None), scoring.reward_sort_key(0.0))
        # Unmeasured must rank ahead of (sort before) a measured neutral 0.0.
        self.assertLess(scoring.reward_sort_key(None), scoring.reward_sort_key(0.0))

    def test_reward_sort_key_tier_precedence(self):
        keys = [scoring.reward_sort_key(r) for r in (0.5, None, 0.0, -0.3)]
        self.assertEqual(sorted(keys), keys)  # already in tier order: positive, unmeasured, neutral, negative


class TrustActWritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        office_runtime.init_db(Path(self.db_path)).close()

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_record_trust_act_then_get_current_trust_state_proven(self):
        scoring.record_trust_act(self.db_path, "t@local/m@medium", "proven", "rico", "manually verified rollout")
        self.assertEqual(scoring.get_current_trust_state(self.db_path, "t@local/m@medium"), "proven")

    def test_record_trust_act_rejects_short_reason(self):
        with self.assertRaises(ValueError):
            scoring.record_trust_act(self.db_path, "t@local/m@medium", "proven", "rico", "short")

    def test_record_trust_act_rejects_empty_actor(self):
        with self.assertRaises(ValueError):
            scoring.record_trust_act(self.db_path, "t@local/m@medium", "proven", "", "a real justification here")

    def test_record_trust_act_rejects_illegal_target_state(self):
        with self.assertRaises(ValueError):
            scoring.record_trust_act(self.db_path, "t@local/m@medium", "invalid", "rico", "a real justification here")

    def test_act_is_append_only_and_latest_wins(self):
        scoring.record_trust_act(self.db_path, "t@local/m@medium", "proven", "rico", "first rollout decision")
        scoring.record_trust_act(self.db_path, "t@local/m@medium", "valid-unverified", "rico", "walked it back after review")
        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT target_state FROM adapter_trust_acts ORDER BY recorded_at").fetchall()
        con.close()
        self.assertEqual(len(rows), 2)  # append-only: both rows remain
        self.assertEqual(scoring.get_current_trust_state(self.db_path, "t@local/m@medium"), "valid-unverified")


if __name__ == '__main__':
    unittest.main()
