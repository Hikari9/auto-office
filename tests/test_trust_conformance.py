"""Normative trust contract for docs/v3-runtime-contracts.md section 7.1 (amendment v6).

THIS SUITE IS THE CONTRACT. Amendment v6 (docs/plans/v3-final-merge.md `version: 6`;
authority commit b8577a3 on auto-office-v3) removed the pinned SQL from section 7 after
four review rounds each found a real defect inside a correct-looking query body committed
to a prose document. Prose review can confirm a query *reads* correctly without ever
*executing* it against an adversarial fixture, which is how the same class survived four
correct fixes.

The invariant: A QUERY MAY LOWER ADAPTER TRUST AND MAY NEVER RAISE IT.

T2B implements `evaluate_trust_state(con, target_triple) -> (critical_failures, state)` in
scripts/office_scoring.py. T2B's completion criterion is this suite running un-skipped and
green. T2B must NOT edit this file: a case believed wrong is a defect reported against T0's
contract, not a test to change. A worker that edits this suite to go green has deleted the
only artifact proving the invariant.

Set OFFICE_TRUST_IMPL to a filesystem path to point the suite at an alternative
implementation. That hook exists so the suite can be proven REJECTING -- run it against a
deliberately wrong implementation and watch it fail -- because a suite that cannot fail is
not a contract.
"""
import importlib.util
import os
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMPL = ROOT / 'scripts' / 'office_scoring.py'
SKIP_REASON = ("trust conformance suite: scripts/office_scoring.py not implemented yet (T2B)")


def _load_impl():
    """Returns evaluate_trust_state, or None when T2B has not implemented it yet."""
    path = Path(os.environ.get('OFFICE_TRUST_IMPL', DEFAULT_IMPL))
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location('office_scoring_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'evaluate_trust_state', None)


class TrustConformance(unittest.TestCase):
    TRIPLE = "agy@local/gemini-3.8-flash@medium"
    VALID_HASH = "sha256:" + "a" * 64

    @classmethod
    def setUpClass(cls):
        cls.impl = _load_impl()

    def setUp(self):
        if self.impl is None:
            self.skipTest(SKIP_REASON)

    # ---- fixture helpers: the schema T2B's implementation must read ----

    def _con(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
        CREATE TABLE dispatches(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, holder_id TEXT, triple TEXT, invocation_model_id TEXT, selection_reason TEXT, task_shape TEXT, size_class TEXT, started_at TEXT, ended_at TEXT, money_estimate REAL, money_actual REAL, quota_estimate REAL, quota_delta REAL, wall_clock_seconds REAL, attribution TEXT, outcome TEXT);
        CREATE TABLE findings(id TEXT PRIMARY KEY, dispatch_id TEXT, reviewer_dispatch_id TEXT, status TEXT, severity TEXT, summary TEXT, evidence_hash TEXT, created_at TEXT);
        CREATE TABLE outcome_labels(id TEXT PRIMARY KEY, dispatch_id TEXT, label TEXT, primary_attribution TEXT, contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT);
        CREATE TABLE adapter_trust_acts(id TEXT PRIMARY KEY, triple TEXT, target_state TEXT, actor_id TEXT, reason TEXT, evidence_reference TEXT, recorded_at TEXT);
        """)
        return con

    def _run(self, con):
        return tuple(type(self).impl(con, self.TRIPLE))

    def _dispatch(self, con, did, task_shape="shapeA", attribution=None, run_id="run-1"):
        con.execute("INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, run_id, "executor", "h1", self.TRIPLE, "m1", "r", task_shape, "M",
                     "t", "t", 0, 0, 0, 0, 0, attribution, None))

    def _label(self, con, did, label, evidence_hash=None, primary_attribution="model",
               labeled_at="2026-09-15T00:00:00Z", label_id=None):
        con.execute("INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
                    (label_id or f"lab-{did}-{label}", did, label, primary_attribution, None,
                     labeled_at, evidence_hash))

    def _trust_act(self, con, triple=None, target_state="proven", actor_id="rico",
                   reason="manually reviewed and reinstated", evidence_reference=None,
                   recorded_at="2026-09-16T00:00:00Z", act_id=None):
        con.execute("INSERT INTO adapter_trust_acts VALUES (?,?,?,?,?,?,?)",
                    (act_id or f"act-{recorded_at}", triple or self.TRIPLE, target_state,
                     actor_id, reason, evidence_reference, recorded_at))

    def _seed_five_evidenced_successes(self, con):
        for i, shape in enumerate(["shapeA", "shapeA", "shapeA", "shapeB", "shapeB"]):
            did = f"disp-ok-{i}"
            self._dispatch(con, did, task_shape=shape)
            self._label(con, did, "verified_no_observed_failure", evidence_hash=self.VALID_HASH)

    # ---- case 1: evidence never promotes (round-5 review R1) ----

    def test_five_self_reported_labels_two_shapes_never_produce_proven(self):
        """The round-5 reviewer's exact counterexample. Five self-reported
        verified_no_observed_failure labels with well-formed hashes and no supporting
        artifacts returned 'proven' against the 8e90ef0 query. No accumulation of
        self-reported success may raise trust."""
        con = self._con()
        self._seed_five_evidenced_successes(con)
        state = self._run(con)[1]
        self.assertNotEqual(state, "proven")
        self.assertEqual(state, "valid-unverified")

    # ---- case 2: demotion stays automatic ----

    def test_qualifying_failure_quarantines_with_no_human_action(self):
        """Demotion must remain fully derived. One adapter-attributed evidence-backed
        recurrence_failure quarantines with no act, no lineage row, no validation row."""
        con = self._con()
        self._dispatch(con, "disp-bad", attribution="adapter")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self.assertEqual(self._run(con), (1, "quarantined"))

    # ---- case 3: THE LATCH (confirm-review of 1787d67) ----

    def test_appended_benign_label_cannot_retract_a_failure(self):
        """The defect that blocked 1787d67. `outcome_labels` is append-only, has no
        actor_id and requires no authority, so appending a benign label to a failed
        dispatch is a contract-legal write. Against an rn=1 implementation it moved the
        failure off the newest row: (1,'quarantined') became (0,'proven'). Failure
        evidence latches -- every qualifying label ever recorded counts, not the latest."""
        con = self._con()
        self._dispatch(con, "disp-bad", attribution="adapter")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    labeled_at="2026-09-15T00:00:00Z", label_id="lab-fail")
        self._trust_act(con, target_state="proven", reason="operator says fine")
        self._label(con, "disp-bad", "verified_no_observed_failure",
                    evidence_hash=self.VALID_HASH, labeled_at="2026-09-15T01:00:00Z",
                    label_id="lab-benign")
        self.assertEqual(self._run(con), (1, "quarantined"))

    # ---- case 4: the F12 branch launders the same way ----

    def test_appended_benign_label_cannot_retract_a_critical_finding(self):
        """Same laundering path through the finding branch, and worse: the
        accepted-material critical finding stays in `findings` untouched while an
        appended benign label plus a 'proven' act walked the state to (0,'proven')."""
        con = self._con()
        self._dispatch(con, "disp-bad-abandoned", attribution="adapter")
        self._label(con, "disp-bad-abandoned", "abandoned", evidence_hash=None,
                    label_id="lab-abandoned")
        con.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
                    ("f1", "disp-bad-abandoned", "rev-1", "accepted-material", "critical",
                     "broke prod", self.VALID_HASH, "2026-09-15T00:00:00Z"))
        self._label(con, "disp-bad-abandoned", "verified_no_observed_failure",
                    evidence_hash=self.VALID_HASH, labeled_at="2026-09-15T02:00:00Z",
                    label_id="lab-benign-2")
        self._trust_act(con, target_state="proven", reason="attempted clear",
                        recorded_at="2026-09-20T00:00:00Z")
        self.assertEqual(self._run(con)[1], "quarantined")

    # ---- cases 5-9: the explicit act is the only upward path, and it is bounded ----

    def test_explicit_trust_act_raises_a_clean_triple_to_proven(self):
        con = self._con()
        self._trust_act(con, target_state="proven", reason="operator-verified rollout")
        self.assertEqual(self._run(con), (0, "proven"))

    def test_explicit_trust_act_cannot_override_a_standing_quarantine(self):
        con = self._con()
        self._dispatch(con, "disp-bad", attribution="adapter")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self._trust_act(con, target_state="proven", reason="attempted override",
                        recorded_at="2026-09-20T00:00:00Z")
        self.assertEqual(self._run(con)[1], "quarantined")

    def test_most_recent_trust_act_wins_over_an_older_one(self):
        con = self._con()
        self._trust_act(con, target_state="proven", act_id="act-1",
                        recorded_at="2026-09-10T00:00:00Z")
        self._trust_act(con, target_state="valid-unverified", act_id="act-2",
                        recorded_at="2026-09-15T00:00:00Z")
        self.assertEqual(self._run(con)[1], "valid-unverified")

    def test_trust_act_for_a_different_triple_does_not_apply(self):
        con = self._con()
        self._trust_act(con, triple="other@local/other@medium", target_state="proven")
        self.assertEqual(self._run(con)[1], "valid-unverified")

    def test_no_evidence_and_no_act_floors_at_valid_unverified(self):
        con = self._con()
        self.assertEqual(self._run(con), (0, "valid-unverified"))

    # ---- evidence-validity checklist properties 1-4 (finding F21) ----

    def test_empty_string_evidence_hash_does_not_quarantine(self):
        con = self._con()
        self._dispatch(con, "disp-empty", attribution="adapter")
        self._label(con, "disp-empty", "recurrence_failure", evidence_hash="")
        self.assertEqual(self._run(con)[1], "valid-unverified")

    def test_non_hex_evidence_hash_does_not_quarantine(self):
        con = self._con()
        self._dispatch(con, "disp-nonhex", attribution="adapter")
        self._label(con, "disp-nonhex", "recurrence_failure",
                    evidence_hash="sha256:" + "z" * 64)
        self.assertEqual(self._run(con)[1], "valid-unverified")

    def test_uppercase_hex_evidence_hash_does_not_quarantine(self):
        con = self._con()
        self._dispatch(con, "disp-upper", attribution="adapter")
        self._label(con, "disp-upper", "recurrence_failure",
                    evidence_hash="sha256:" + "A" * 64)
        self.assertEqual(self._run(con)[1], "valid-unverified")

    def test_duplicate_label_does_not_multiply_the_failure_count(self):
        """Evidence property 5: de-duplication is by distinct dispatch, not by recency."""
        con = self._con()
        self._dispatch(con, "disp-bad", attribution="adapter")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    label_id="lab-1")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    labeled_at="2026-09-15T05:00:00Z", label_id="lab-2")
        self.assertEqual(self._run(con), (1, "quarantined"))


if __name__ == '__main__':
    unittest.main()
