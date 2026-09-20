import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts" / "office_runtime.py"
HOOK = ROOT / "scripts" / "hooks" / "session_end.sh"


class SessionEndHookTests(unittest.TestCase):
    def test_pointer_resolves_external_state_and_does_not_create_fake_dispatch_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True)

            db = root / "shared" / "runs.db"
            env = {**os.environ, "AUTO_OFFICE_RUNS_DB": str(db), "XDG_STATE_HOME": str(root / "state")}
            started = subprocess.run(
                [
                    sys.executable, str(RUNTIME), "start", "--goal", "hook test",
                    "--playbook", "Change", "--gear", "direct", "--repo", str(repo),
                ], cwd=ROOT, env=env, capture_output=True, text=True, check=True,
            )
            kickoff = json.loads(started.stdout)
            run_id = kickoff["run_id"]

            hook_env = {**env, "OFFICE_STATE_DIR": str(repo / ".office"), "OFFICE_RUN_ID": run_id}
            result = subprocess.run([str(HOOK)], cwd=repo, env=hook_env, capture_output=True, text=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((repo / ".office" / "telemetry.db").exists())
            self.assertFalse((repo / ".office" / "session_end_dispatch.json").exists())
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM runs WHERE id=?", (run_id,)).fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
