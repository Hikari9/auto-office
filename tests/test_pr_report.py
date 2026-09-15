"""Tests for scripts/pr_report.py (docs/plans/v3-final-merge.md amendment v4).

The receipt is determinism: `python3 scripts/pr_report.py` run twice on
identical input must be byte-identical, asserted here rather than claimed in
prose. These tests also cover the before/after shape: a compact row per PR
version comparing it to the immediately previous version and to a fixed
baseline, with numbers only -- no agent-generated narrative.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("pr_report", ROOT / "scripts" / "pr_report.py")
pr_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_report)

SAMPLE_INPUT = {
    "baseline_version": "v1",
    "versions": [
        {
            "version": "v1",
            "pr": 99,
            "shipped_size_bytes": 12000,
            "estimated_loaded_tokens": 4200,
            "actual_loaded_tokens": None,
            "eval_score": 0.62,
            "reward": -0.1,
        },
        {
            "version": "v2",
            "pr": 99,
            "shipped_size_bytes": 12800,
            "estimated_loaded_tokens": 4300,
            "actual_loaded_tokens": 4550,
            "eval_score": 0.70,
            "reward": 0.2,
        },
        {
            "version": "v3",
            "pr": 99,
            "shipped_size_bytes": 12500,
            "estimated_loaded_tokens": 4250,
            "actual_loaded_tokens": 4300,
            "eval_score": 0.75,
            "reward": 0.35,
        },
    ],
}


class PrReportUnitTests(unittest.TestCase):
    def test_first_version_has_no_row(self):
        rows = pr_report.build_report(SAMPLE_INPUT)
        self.assertEqual([r["version"] for r in rows], ["v2", "v3"])

    def test_v2_row_compares_to_previous_and_baseline(self):
        rows = pr_report.build_report(SAMPLE_INPUT)
        v2 = rows[0]
        self.assertEqual(v2["previous_version"], "v1")
        self.assertEqual(v2["baseline_version"], "v1")
        self.assertEqual(v2["shipped_size_bytes"]["value"], 12800)
        self.assertEqual(v2["shipped_size_bytes"]["delta_vs_previous"], 800)
        self.assertEqual(v2["shipped_size_bytes"]["delta_vs_baseline"], 800)
        self.assertEqual(v2["eval_score"]["delta_vs_previous"], round(0.70 - 0.62, 6))

    def test_v3_baseline_delta_is_cumulative_not_previous(self):
        rows = pr_report.build_report(SAMPLE_INPUT)
        v3 = rows[1]
        # previous is v2 (12800), baseline is v1 (12000) -- these must differ.
        self.assertEqual(v3["shipped_size_bytes"]["delta_vs_previous"], 12500 - 12800)
        self.assertEqual(v3["shipped_size_bytes"]["delta_vs_baseline"], 12500 - 12000)

    def test_missing_actual_tokens_yields_null_delta_not_zero(self):
        rows = pr_report.build_report(SAMPLE_INPUT)
        v2 = rows[0]
        self.assertIsNone(v2["actual_loaded_tokens"]["delta_vs_previous"])
        self.assertIsNone(v2["actual_loaded_tokens"]["delta_vs_baseline"])
        self.assertEqual(v2["actual_loaded_tokens"]["value"], 4550)

    def test_single_version_produces_no_rows(self):
        rows = pr_report.build_report({"versions": [SAMPLE_INPUT["versions"][0]]})
        self.assertEqual(rows, [])

    def test_baseline_version_override(self):
        data = dict(SAMPLE_INPUT)
        data["baseline_version"] = "v2"
        rows = pr_report.build_report(data)
        v3 = rows[1]
        self.assertEqual(v3["baseline_version"], "v2")
        self.assertEqual(v3["shipped_size_bytes"]["delta_vs_baseline"], 12500 - 12800)

    def test_output_contains_no_narrative_text_field(self):
        rows = pr_report.build_report(SAMPLE_INPUT)
        for row in rows:
            for key, value in row.items():
                if key in ("version", "pr", "previous_version", "baseline_version"):
                    continue
                self.assertIsInstance(value, dict)
                self.assertEqual(set(value.keys()), {"value", "delta_vs_previous", "delta_vs_baseline"})


class PrReportCliDeterminismTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.input_path = Path(self._tmp.name) / "input.json"
        self.input_path.write_text(json.dumps(SAMPLE_INPUT), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pr_report.py"), "--input", str(self.input_path)],
            capture_output=True, text=True, check=True,
        )

    def test_two_runs_on_identical_input_are_byte_identical(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first.stdout, second.stdout)
        self.assertTrue(first.stdout)

    def test_cli_output_matches_library_call(self):
        result = self._run()
        rows = json.loads(result.stdout)
        self.assertEqual(rows, pr_report.build_report(SAMPLE_INPUT))

    def test_out_flag_writes_identical_bytes_to_stdout(self):
        out_path = Path(self._tmp.name) / "out.json"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pr_report.py"), "--input", str(self.input_path),
             "--out", str(out_path)],
            capture_output=True, text=True, check=True,
        )
        stdout_run = self._run()
        self.assertEqual(out_path.read_text(encoding="utf-8"), stdout_run.stdout)


if __name__ == "__main__":
    unittest.main()
