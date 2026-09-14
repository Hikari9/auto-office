import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts" / "office_runtime.py"


class RuntimeDogfoodTests(unittest.TestCase):
    def _run(self, env, *args):
        result = subprocess.run(
            [sys.executable, str(RUNTIME), *map(str, args)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"runtime emitted no JSON receipt: {exc}\n{result.stdout}\n{result.stderr}")
        return result.returncode, output

    def _ok(self, env, *args):
        code, output = self._run(env, *args)
        self.assertEqual(code, 0, output)
        return output

    @staticmethod
    def _state(state_dir):
        return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))

    def test_real_run_has_receipt_backed_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "dogfood@example.test"],
                ["git", "config", "user.name", "dogfood"],
                ["git", "commit", "--allow-empty", "-q", "-m", "init"],
            ):
                subprocess.run(command, cwd=repo, check=True, capture_output=True)

            env = os.environ.copy()
            env["XDG_STATE_HOME"] = str(tmp_path / "state-home")
            start = self._ok(
                env,
                "start",
                "--goal",
                "dogfood the lifecycle",
                "--playbook",
                "Change",
                "--gear",
                "direct",
                "--repo",
                repo,
            )
            state_dir = Path(start["state_dir"])
            state_path = state_dir / "state.json"
            self.assertTrue(state_dir.is_dir())
            initial = self._state(state_dir)
            self.assertEqual(initial["phase"], "intake")
            run_id, family_id = initial["run_id"], initial["family_id"]

            pointer = Path(start["pointer"])
            self.assertTrue(pointer.is_file())
            self.assertEqual(pointer.read_text(encoding="utf-8").strip(), str(state_dir.resolve()))
            gitignore = repo / ".gitignore"
            self.assertEqual(gitignore.read_text(encoding="utf-8").splitlines().count(".office/"), 1)

            envelope = state_dir / "envelope.json"
            self.assertTrue(envelope.is_file())
            validation = self._ok(env, "validate-packet", "--kind", "envelope", envelope)
            self.assertTrue(validation["valid"])
            self.assertEqual(validation["errors"], [])

            state_args = ("--state-dir", state_dir, "--run-id", run_id, "--family-id", family_id)

            def save(phase):
                saved = self._ok(env, "state-save", *state_args, "--phase", phase)
                self.assertEqual(saved["saved"], str(state_path))
                self.assertEqual(self._state(state_dir)["phase"], phase)

            save("planned")
            bypass_code, _ = self._run(env, "state-save", *state_args, "--phase", "approved")
            self.assertNotEqual(bypass_code, 0)
            self.assertEqual(self._state(state_dir)["phase"], "planned")

            quote = "I approve this plan for execution."
            approval = self._ok(env, "approve-plan", "--state-dir", state_dir,
                                "--approved-by", "user", "--quote", quote)
            self.assertEqual(approval["approval"]["quote"], quote)
            approved = self._state(state_dir)
            self.assertEqual(approved["phase"], "approved")
            self.assertEqual(approved["approval"]["quote"], quote)

            for phase in ("executing", "reviewed", "closed"):
                save(phase)

            closed = self._state(state_dir)
            self.assertEqual(closed["approval"]["quote"], quote)
            self.assertEqual(gitignore.read_text(encoding="utf-8").splitlines().count(".office/"), 1)
