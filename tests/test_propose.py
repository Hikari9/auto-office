"""Tests for the dream -> proposal -> standing-branch cycle (scripts/office_propose.py).

The cases that matter here are the rejecting ones. An append that succeeds proves only that
writing a file works; what the lifecycle depends on is that a replay writes nothing and that
private material never reaches the public branch.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import office_propose as prop  # noqa: E402
import office_runtime as rt  # noqa: E402


DEFECTS = [
    {"id": "aaaaaaaa-0000-0000-0000-000000000001", "kind": "invalid-invocation-slug",
     "harness": "claude", "attempted": "claude --model x", "observed_error": "banner said medium"},
    {"id": "aaaaaaaa-0000-0000-0000-000000000002", "kind": "unsupported-effort",
     "harness": "claude", "attempted": "claude --model y", "observed_error": "defaulted silently"},
]


def write_state_dir(root, defects=DEFECTS):
    sd = Path(root) / "state"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "route-defects.jsonl").write_text(
        "".join(json.dumps(d) + "\n" for d in defects), encoding="utf-8")
    return sd


class SanitizerTest(unittest.TestCase):
    def test_sanitizer_removes_every_class_the_privacy_lint_rejects(self):
        raw = ("failed at /Users/someone/Git/private-repo/scripts/x.py, reported by "
               "person@example.com via https://internal.example.com/issue/12 with "
               "api_key=abcdefghijkl commit 1a2b3c4d5e6f")
        cleaned = prop.sanitize(raw)
        self.assertEqual(rt.privacy_findings(cleaned), [],
                         f"sanitized text still trips privacy lint: {cleaned}")

    def test_sanitizer_is_deterministic(self):
        raw = "/Users/a/b and person@example.com"
        self.assertEqual(prop.sanitize(raw), prop.sanitize(raw))


class CompileDreamTest(unittest.TestCase):
    def test_dream_never_carries_the_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = write_state_dir(tmp)
            run_id = "0b3e4761-b757-4dc5-b100-a2dbfac05808"
            dream = prop.compile_dream(None, sd, run_id)
            self.assertNotIn(run_id, json.dumps(dream))
            # ...but the machine that holds the run id can still recognise its own proposal.
            self.assertEqual(dream["lineage_digest"], prop.digest("run", run_id))

    def test_a_single_defect_is_not_a_pattern(self):
        # One wrong slug is a typo. The pattern claim is about a harness accepting an
        # under-specified identity twice, so a one-row source must not produce it.
        with tempfile.TemporaryDirectory() as tmp:
            sd = write_state_dir(tmp, defects=DEFECTS[:1])
            dream = prop.compile_dream(None, sd, "r1")
            ids = [p["pattern_id"] for p in dream["patterns"]]
            self.assertNotIn("route-identity-silently-defaulted", ids)

    def test_two_defects_on_one_harness_produce_the_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = write_state_dir(tmp)
            dream = prop.compile_dream(None, sd, "r1")
            ids = [p["pattern_id"] for p in dream["patterns"]]
            self.assertIn("route-identity-silently-defaulted", ids)

    def test_rendered_proposal_passes_privacy_lint(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = write_state_dir(tmp)
            text = prop.render(prop.compile_dream(None, sd, "r1"))
            self.assertEqual(rt.privacy_findings(text), [])


class AppendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.branch = Path(self.tmp.name) / "branch"
        self.sd = write_state_dir(self.tmp.name)
        self.dream = prop.compile_dream(None, self.sd, "r1")
        self.text = prop.render(self.dream)

    def test_replaying_the_same_dream_appends_nothing(self):
        first = prop.append(self.branch, self.dream, self.text)
        self.assertTrue(first["appended"])
        index_after_first = (self.branch / "PROPOSALS.md").read_text()

        second = prop.append(self.branch, self.dream, self.text)
        self.assertFalse(second["appended"])
        self.assertEqual(second["reason"], "identity_already_present")
        self.assertEqual((self.branch / "PROPOSALS.md").read_text(), index_after_first)
        self.assertEqual(len(list((self.branch / "proposals").iterdir())), 1)

    def test_a_different_dream_does_append(self):
        # Guards the idempotence test above from passing for the wrong reason: if append
        # refused everything after the first write, the replay test would still be green.
        prop.append(self.branch, self.dream, self.text)
        other = dict(self.dream, lineage_digest=prop.digest("run", "another-run"))
        result = prop.append(self.branch, other, prop.render(other))
        self.assertTrue(result["appended"])
        self.assertEqual(len(list((self.branch / "proposals").iterdir())), 2)

    def test_private_material_is_refused_not_reported(self):
        leaky = self.text + "\n\nfailed at /Users/someone/Git/repo/x.py\n"
        result = prop.append(self.branch, self.dream, leaky)
        self.assertFalse(result["appended"])
        self.assertEqual(result["reason"], "privacy_lint_failed")
        self.assertIn("unix-absolute-path", result["findings"])
        self.assertFalse(self.branch.exists() and any(self.branch.rglob("*.md")),
                         "a proposal that failed privacy lint reached the public branch")

    def test_identity_ignores_rendering_and_tracks_content(self):
        # Reworded prose for the same dream must not create a second proposal.
        ident = prop.identity_hash(self.dream)
        self.assertEqual(ident, prop.identity_hash(json.loads(json.dumps(self.dream))))
        changed = dict(self.dream, source_counts={"route_defects": 99, "findings": 0})
        self.assertNotEqual(ident, prop.identity_hash(changed))

    def test_lineage_row_is_written_once_across_a_replay(self):
        db = Path(self.tmp.name) / "runs.db"
        rt.init_db(db)
        prop.append(self.branch, self.dream, self.text, db=str(db))
        prop.append(self.branch, self.dream, self.text, db=str(db))
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT component_kind, parent_id, event FROM lineage").fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "proposal")
        self.assertEqual(rows[0][1], self.dream["lineage_digest"])
        self.assertEqual(rows[0][2], "appended")


if __name__ == "__main__":
    unittest.main()
