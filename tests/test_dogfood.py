import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts" / "office_runtime.py"
PRE_TOOL_USE = ROOT / "scripts" / "hooks" / "pre_tool_use.py"
SPAWN = ROOT / "scripts" / "office_spawn.sh"
VERIFY = ROOT / "scripts" / "verify.sh"


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

    def _run_hook(self, env, repo, run_id, payload):
        hook_env = {**env, "OFFICE_STATE_DIR": str(repo / ".office"), "OFFICE_RUN_ID": run_id}
        return subprocess.run(
            [sys.executable, str(PRE_TOOL_USE)],
            cwd=repo,
            env=hook_env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )

    def _run_script_json(self, env, cwd, script, *args):
        result = subprocess.run(
            [str(script), *map(str, args)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"script emitted no JSON receipt (exit {result.returncode}): "
                f"{exc}\n{result.stdout}\n{result.stderr}"
            )
        self.assertEqual(result.returncode, 0, output)
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

            mutation = {"tool_name": "Write", "tool_input": {"file_path": "executor.receipt"}}
            blocked = self._run_hook(env, repo, run_id, mutation)
            self.assertEqual(blocked.returncode, 2)
            blocked_receipt = json.loads(blocked.stdout)
            self.assertEqual(blocked_receipt["decision"], "block")
            self.assertEqual(blocked_receipt["phase"], "intake")
            self.assertIn("approve-plan", blocked_receipt["required_command"])

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
            blocked = self._run_hook(env, repo, run_id, mutation)
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(json.loads(blocked.stdout)["phase"], "planned")
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
            allowed = self._run_hook(env, repo, run_id, mutation)
            self.assertEqual(allowed.returncode, 0)
            self.assertEqual(allowed.stdout, "")

            db = state_dir / "telemetry.db"
            self._ok(env, "init-db", "--db", db)
            save("executing")
            allowed = self._run_hook(env, repo, run_id, mutation)
            self.assertEqual(allowed.returncode, 0)
            self.assertEqual(allowed.stdout, "")

            dispatch_id = "dogfood-executor"
            adapter = tmp_path / "dogfood-adapter.yaml"
            executor_code = (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('executor ran'); time.sleep(2)"
            )
            adapter.write_text(
                "invocation:\n"
                f"  executable: {sys.executable}\n"
                "  argv:\n"
                "    - \"-c\"\n"
                f"    - {json.dumps(executor_code)}\n"
                "    - \"{cwd}/executor.receipt\"\n"
                "  prompt_transport: argv\n",
                encoding="utf-8",
            )
            brief = tmp_path / "dogfood-brief.txt"
            brief.write_text("Run the dogfood executor.\n", encoding="utf-8")
            spawned = self._run_script_json(
                env,
                ROOT,
                SPAWN,
                "--adapter",
                adapter,
                "--model",
                "dogfood-model",
                "--effort",
                "none",
                "--worktree",
                repo,
                "--brief",
                brief,
                "--dispatch-id",
                dispatch_id,
                "--run-id",
                run_id,
                "--state-dir",
                state_dir,
                "--timeout",
                "5",
            )
            self.assertEqual(spawned["dispatch_id"], dispatch_id)
            dispatch_dir = state_dir / "dispatches" / dispatch_id
            deadline = time.monotonic() + 5
            while not (dispatch_dir / "exit_code").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((dispatch_dir / "exit_code").is_file())
            self.assertTrue((repo / "executor.receipt").is_file())
            dispatch_meta = json.loads((dispatch_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(dispatch_meta["dispatch_id"], dispatch_id)
            self.assertEqual(dispatch_meta["worktree"], str(repo))
            self.assertEqual((dispatch_dir / "exit_code").read_text(encoding="utf-8").strip(), "0")
            self.assertTrue((dispatch_dir / "output.log").is_file())

            verification = self._run_script_json(
                env,
                ROOT,
                VERIFY,
                "--worktree",
                repo,
                "--dispatch-id",
                dispatch_id,
                "--state-dir",
                state_dir,
                "--db",
                db,
            )
            self.assertTrue(verification["passed"])
            self.assertEqual(len(verification["gates"]), 8)
            with sqlite3.connect(db) as connection:
                validation_count = connection.execute(
                    "SELECT COUNT(*) FROM validations WHERE dispatch_id = ?", (dispatch_id,)
                ).fetchone()[0]
            self.assertEqual(validation_count, 8)

            for phase in ("reviewed", "closed"):
                save(phase)
                allowed = self._run_hook(env, repo, run_id, mutation)
                self.assertEqual(allowed.returncode, 0)
                self.assertEqual(allowed.stdout, "")

            closed = self._state(state_dir)
            self.assertEqual(closed["approval"]["quote"], quote)
            self.assertEqual(gitignore.read_text(encoding="utf-8").splitlines().count(".office/"), 1)
