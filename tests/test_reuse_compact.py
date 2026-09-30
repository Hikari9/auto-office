"""Reuse-vs-compact dispatch plan tests (issue #161)."""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load("office_runtime", "scripts/office_runtime.py")


class TestReuseDispatchPlan(unittest.TestCase):
    def _plan(self, **overrides):
        kwargs = dict(
            role="executor",
            context_tokens=300_000,
            herdr_available=True,
            compact_supported=True,
            target="worker-1",
            brief_path="/tmp/brief-T1.md",
        )
        kwargs.update(overrides)
        return rt.reuse_dispatch_plan(**kwargs)

    def test_exactly_at_threshold_is_normal_reuse(self):
        plan = self._plan(context_tokens=rt.REUSE_COMPACT_THRESHOLD)
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "at_or_below_threshold")

    def test_over_threshold_is_compact_then_queue(self):
        plan = self._plan(context_tokens=rt.REUSE_COMPACT_THRESHOLD + 1)
        self.assertEqual(plan["mode"], "compact_then_queue")
        self.assertEqual(plan["reason"], "eligible_over_threshold")

    def test_unknown_context_tokens_is_normal_reuse(self):
        plan = self._plan(context_tokens=None)
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "context_unknown")

    def test_executor_role_qualifies(self):
        plan = self._plan(role="executor")
        self.assertEqual(plan["mode"], "compact_then_queue")
        self.assertEqual(plan["reason"], "eligible_over_threshold")
        self.assertEqual(plan["commands"][1][-1], "Read and carry out the brief at /tmp/brief-T1.md exactly.")

    def test_plan_reviewer_role_qualifies(self):
        plan = self._plan(role="plan_reviewer")
        self.assertEqual(plan["mode"], "compact_then_queue")
        self.assertEqual(plan["reason"], "eligible_over_threshold")
        self.assertEqual(plan["commands"][1][-1], "Read and carry out the brief at /tmp/brief-T1.md exactly.")

    def test_reviewer_role_is_not_eligible(self):
        plan = self._plan(role="reviewer")
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "role_not_eligible")

    def test_worker_role_is_not_eligible(self):
        plan = self._plan(role="worker")
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "role_not_eligible")

    def test_herdr_unavailable_is_normal_reuse(self):
        plan = self._plan(herdr_available=False)
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "herdr_unavailable")

    def test_compact_unsupported_is_normal_reuse(self):
        plan = self._plan(compact_supported=False)
        self.assertEqual(plan["mode"], "normal_reuse")
        self.assertEqual(plan["reason"], "compact_unsupported")

    def test_compact_command_precedes_brief_command(self):
        plan = self._plan()
        self.assertEqual(len(plan["commands"]), 2)
        self.assertEqual(plan["commands"][0], ["herdr", "agent", "prompt", "worker-1", "/compact"])
        self.assertEqual(
            plan["commands"][1],
            ["herdr", "agent", "prompt", "worker-1",
             "Read and carry out the brief at /tmp/brief-T1.md exactly."],
        )

    def test_normal_reuse_has_only_brief_command(self):
        plan = self._plan(context_tokens=100)
        self.assertEqual(
            plan["commands"],
            [["herdr", "agent", "prompt", "worker-1",
              "Read and carry out the brief at /tmp/brief-T1.md exactly."]],
        )

    def test_no_command_waits_or_polls(self):
        for plan_kwargs in ({}, {"context_tokens": 100}):
            plan = self._plan(**plan_kwargs)
            self.assertFalse(plan["await_compaction"])
            for command in plan["commands"]:
                joined = " ".join(command)
                self.assertNotIn("--wait", joined)
                self.assertNotIn("wait", command)

    def test_no_command_spawns_or_starts_a_worker(self):
        for plan_kwargs in ({}, {"context_tokens": 100}, {"role": "reviewer"}):
            plan = self._plan(**plan_kwargs)
            for command in plan["commands"]:
                self.assertNotIn("spawn", command)
                self.assertNotIn("start", command)


class TestReusePlanCli(unittest.TestCase):
    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "office_runtime.py"), "reuse-plan", *args],
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_matches_pure_function(self):
        expected = rt.reuse_dispatch_plan(
            role="executor",
            context_tokens=rt.REUSE_COMPACT_THRESHOLD + 1,
            herdr_available=True,
            compact_supported=True,
            target="worker-1",
            brief_path="/tmp/brief-T1.md",
        )
        actual = self._run(
            "--role", "executor",
            "--context-tokens", str(rt.REUSE_COMPACT_THRESHOLD + 1),
            "--herdr-available",
            "--compact-supported",
            "--target", "worker-1",
            "--brief-path", "/tmp/brief-T1.md",
        )
        self.assertEqual(actual, expected)

    def test_cli_defaults_herdr_unavailable_and_compact_unsupported(self):
        actual = self._run(
            "--role", "executor",
            "--context-tokens", str(rt.REUSE_COMPACT_THRESHOLD + 1),
            "--target", "worker-1",
            "--brief-path", "/tmp/brief-T1.md",
        )
        self.assertEqual(actual["mode"], "normal_reuse")
        self.assertEqual(actual["reason"], "herdr_unavailable")


if __name__ == "__main__":
    unittest.main()
