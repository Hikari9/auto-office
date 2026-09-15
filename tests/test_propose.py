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


class RecorderBackedDreamTest(unittest.TestCase):
    """The runs.db half of the compiler. Every other compile test passes db=None, so this path
    -- the one that reads real recorder rows -- was executed by no test at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "runs.db"
        rt.init_db(self.db)
        self.sd = write_state_dir(self.tmp.name, defects=[])
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO runs(id, family_id) VALUES('run-A','fam-A')")
        con.execute("INSERT INTO runs(id, family_id) VALUES('run-B','fam-B')")
        con.execute("INSERT INTO dispatches(id, run_id) VALUES('d-A','run-A')")
        con.execute("INSERT INTO dispatches(id, run_id) VALUES('d-B','run-B')")
        # Severities as review_finding.sh and office_scoring.py actually write them: the
        # `material` token lives in the STATUS column, never in severity.
        for fid, did, status, sev in [
            ("f-A1", "d-A", "accepted-material", "critical"),
            ("f-A2", "d-A", "accepted-material", "high"),
            ("f-A3", "d-A", "accepted-minor", "low"),
            ("f-B1", "d-B", "accepted-material", "high"),
            ("f-orphan", None, "accepted-material", "high"),
        ]:
            con.execute("INSERT INTO findings(id, dispatch_id, status, severity) VALUES(?,?,?,?)",
                        (fid, did, status, sev))
        con.commit()
        con.close()

    def test_material_findings_are_selected_by_status_not_severity(self):
        """Nothing in this repo writes severity == 'material'. Selecting on it made this
        branch dead against every recorder-written row."""
        dream = prop.compile_dream(str(self.db), self.sd, "r1", family_id="fam-A")
        patterns = {p["pattern_id"]: p for p in dream["patterns"]}
        self.assertIn("gate-satisfiable-by-excluded-evidence", patterns)
        self.assertEqual(patterns["gate-satisfiable-by-excluded-evidence"]["occurrences"], 2)
        self.assertEqual(patterns["gate-satisfiable-by-excluded-evidence"]["kinds"],
                         ["critical", "high"])

    def test_a_family_scoped_dream_excludes_other_families_and_orphans(self):
        """The published occurrence count is family-scoped evidence. A predicate admitting
        rows with no run row inflated it with another family's findings."""
        dream = prop.compile_dream(str(self.db), self.sd, "r1", family_id="fam-A")
        self.assertEqual(dream["source_counts"]["findings"], 3)  # f-A1, f-A2, f-A3 only

        other = prop.compile_dream(str(self.db), self.sd, "r1", family_id="fam-B")
        self.assertEqual(other["source_counts"]["findings"], 1)

        every = prop.compile_dream(str(self.db), self.sd, "r1", family_id=None)
        self.assertEqual(every["source_counts"]["findings"], 5)

    def test_a_family_with_no_material_findings_compiles_no_pattern(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE findings SET status='accepted-minor' WHERE dispatch_id='d-A'")
        con.commit(); con.close()
        dream = prop.compile_dream(str(self.db), self.sd, "r1", family_id="fam-A")
        self.assertEqual(dream["patterns"], [])


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
