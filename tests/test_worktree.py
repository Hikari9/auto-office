import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKTREE_SH = ROOT / "scripts" / "office_worktree.sh"


class WorktreeScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(self.repo), check=True)
        (self.repo / "README.md").write_text("# Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo), check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_cleanup_with_custom_path(self):
        wt_path = self.tmp / "custom_wt"
        res = subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "fam1",
            "--run-id", "run1",
            "--dispatch-id", "disp1",
            "--worktree-path", str(wt_path)
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(wt_path.exists())

        # Check clean
        res_check = subprocess.run([
            str(WORKTREE_SH), "check", "--worktree", str(wt_path)
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res_check.returncode, 0)
        status = json.loads(res_check.stdout)
        self.assertFalse(status["dirty"])

        # Merge branch so git branch -d will succeed
        branch_name = "office/fam1/run1/disp1"
        subprocess.run(["git", "merge", "--no-ff", "-m", "merge disp1", branch_name],
                       cwd=str(self.repo), check=True, capture_output=True)

        # Cleanup
        res_clean = subprocess.run([
            str(WORKTREE_SH), "cleanup", "--worktree", str(wt_path)
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res_clean.returncode, 0, res_clean.stderr)
        self.assertFalse(wt_path.exists())

        # Branch should have been deleted
        branches = subprocess.run(["git", "branch", "--list", branch_name],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertNotIn(branch_name, branches)

    def test_cleanup_run(self):
        wt_path1 = self.tmp / "wt1"
        wt_path2 = self.tmp / "wt2"
        # Create worktree 1
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famA",
            "--run-id", "runX",
            "--dispatch-id", "dispA",
            "--worktree-path", str(wt_path1)
        ], cwd=str(self.repo), check=True)

        # Create worktree 2
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famA",
            "--run-id", "runX",
            "--dispatch-id", "dispB",
            "--worktree-path", str(wt_path2)
        ], cwd=str(self.repo), check=True)

        state_dir = self.tmp / "state"
        disp_dir1 = state_dir / "dispatches" / "dispA"
        disp_dir2 = state_dir / "dispatches" / "dispB"
        disp_dir1.mkdir(parents=True, exist_ok=True)
        disp_dir2.mkdir(parents=True, exist_ok=True)
        (disp_dir1 / "meta.json").write_text(json.dumps({"worktree": str(wt_path1)}), encoding="utf-8")
        (disp_dir2 / "meta.json").write_text(json.dumps({"worktree": str(wt_path2)}), encoding="utf-8")
        (state_dir / "state.json").write_text(json.dumps({"run_id": "runX"}), encoding="utf-8")

        # Merge branches
        subprocess.run(["git", "merge", "--no-ff", "-m", "merge dispA", "office/famA/runX/dispA"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "merge", "--no-ff", "-m", "merge dispB", "office/famA/runX/dispB"],
                       cwd=str(self.repo), check=True, capture_output=True)

        # Run cleanup-run
        res = subprocess.run([
            str(WORKTREE_SH), "cleanup-run",
            "--state-dir", str(state_dir),
            "--run-id", "runX"
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(wt_path1.exists())
        self.assertFalse(wt_path2.exists())

        branches = subprocess.run(["git", "branch", "--list", "office/*"],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertNotIn("office/famA/runX/dispA", branches)
        self.assertNotIn("office/famA/runX/dispB", branches)

    def test_cleanup_run_cross_run_isolation(self):
        wt_path_a = self.tmp / "wtA"
        wt_path_b = self.tmp / "wtB"

        # Create Run A worktree
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "fam1",
            "--run-id", "runA",
            "--dispatch-id", "dispA",
            "--worktree-path", str(wt_path_a)
        ], cwd=str(self.repo), check=True)

        # Create Run B worktree
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "fam1",
            "--run-id", "runB",
            "--dispatch-id", "dispB",
            "--worktree-path", str(wt_path_b)
        ], cwd=str(self.repo), check=True)

        # Merge Run B
        subprocess.run(["git", "merge", "--no-ff", "-m", "merge dispB", "office/fam1/runB/dispB"],
                       cwd=str(self.repo), check=True, capture_output=True)

        state_dir_b = self.tmp / "stateB"
        disp_dir_b = state_dir_b / "dispatches" / "dispB"
        disp_dir_b.mkdir(parents=True, exist_ok=True)
        (disp_dir_b / "meta.json").write_text(json.dumps({"worktree": str(wt_path_b)}), encoding="utf-8")
        (state_dir_b / "state.json").write_text(json.dumps({"run_id": "runB"}), encoding="utf-8")

        # Cleanup Run B only
        res = subprocess.run([
            str(WORKTREE_SH), "cleanup-run",
            "--state-dir", str(state_dir_b),
            "--run-id", "runB"
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Run B worktree should be gone, but Run A worktree MUST be preserved!
        self.assertFalse(wt_path_b.exists())
        self.assertTrue(wt_path_a.exists())

        branches = subprocess.run(["git", "branch", "--list", "office/*"],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertNotIn("office/fam1/runB/dispB", branches)
        self.assertIn("office/fam1/runA/dispA", branches)

    def test_check_reports_commits_ahead_of_base_ref(self):
        # An executor that commits its own checkpoint (per skills/auto-loop)
        # leaves a *clean* tree with real, unmerged work on the branch --
        # `check` without --base-ref can't see it (dirty/uncommitted both
        # false), so --base-ref must surface it separately.
        wt_path = self.tmp / "wt_committed"
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famE", "--run-id", "runE", "--dispatch-id", "dispE",
            "--worktree-path", str(wt_path), "--base-ref", base_ref,
        ], cwd=str(self.repo), check=True)

        (wt_path / "new_file.txt").write_text("executor's checkpointed work\n")
        subprocess.run(["git", "add", "new_file.txt"], cwd=str(wt_path), check=True)
        subprocess.run(["git", "commit", "-m", "executor checkpoint"], cwd=str(wt_path), check=True, capture_output=True)

        # Without --base-ref: tree is clean, committed work is invisible.
        res_plain = subprocess.run(
            [str(WORKTREE_SH), "check", "--worktree", str(wt_path)],
            cwd=str(self.repo), capture_output=True, text=True,
        )
        self.assertEqual(res_plain.returncode, 0)
        status_plain = json.loads(res_plain.stdout)
        self.assertFalse(status_plain["dirty"])
        self.assertNotIn("commits_ahead_of_base", status_plain)

        # With --base-ref: the checkpoint commit is visible.
        res_base = subprocess.run(
            [str(WORKTREE_SH), "check", "--worktree", str(wt_path), "--base-ref", base_ref],
            cwd=str(self.repo), capture_output=True, text=True,
        )
        self.assertEqual(res_base.returncode, 0)
        status_base = json.loads(res_base.stdout)
        self.assertFalse(status_base["dirty"])
        self.assertEqual(status_base["commits_ahead_of_base"], 1)

    def test_snapshot_diff_with_base_ref_sees_committed_checkpoint(self):
        wt_path = self.tmp / "wt_snapshot"
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famF", "--run-id", "runF", "--dispatch-id", "dispF",
            "--worktree-path", str(wt_path), "--base-ref", base_ref,
        ], cwd=str(self.repo), check=True)

        (wt_path / "checkpointed.txt").write_text("committed contribution\n")
        subprocess.run(["git", "add", "checkpointed.txt"], cwd=str(wt_path), check=True)
        subprocess.run(["git", "commit", "-m", "checkpoint"], cwd=str(wt_path), check=True, capture_output=True)

        out_plain = self.tmp / "plain.diff"
        subprocess.run(
            [str(WORKTREE_SH), "snapshot-diff", "--worktree", str(wt_path), "--output", str(out_plain)],
            cwd=str(self.repo), check=True,
        )
        # Old behavior (no --base-ref): diff against HEAD of the branch itself
        # is empty once the work is committed.
        self.assertEqual(out_plain.read_text().strip(), "")

        out_base = self.tmp / "base.diff"
        subprocess.run(
            [str(WORKTREE_SH), "snapshot-diff", "--worktree", str(wt_path), "--output", str(out_base),
             "--base-ref", base_ref],
            cwd=str(self.repo), check=True,
        )
        self.assertIn("checkpointed.txt", out_base.read_text())

    def test_cleanup_warns_instead_of_silently_dropping_unmerged_branch(self):
        # Worktree tree is clean (executor committed its checkpoint), so
        # `git worktree remove` succeeds without --force; `git branch -d`
        # must then refuse (branch unmerged) and that refusal must be
        # reported, not swallowed.
        wt_path = self.tmp / "wt_unmerged"
        res_create = subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famG", "--run-id", "runG", "--dispatch-id", "dispG",
            "--worktree-path", str(wt_path),
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res_create.returncode, 0, res_create.stderr)

        (wt_path / "unmerged.txt").write_text("checkpoint, never merged\n")
        subprocess.run(["git", "add", "unmerged.txt"], cwd=str(wt_path), check=True)
        subprocess.run(["git", "commit", "-m", "checkpoint"], cwd=str(wt_path), check=True, capture_output=True)

        res_clean = subprocess.run(
            [str(WORKTREE_SH), "cleanup", "--worktree", str(wt_path)],
            cwd=str(self.repo), capture_output=True, text=True,
        )
        self.assertEqual(res_clean.returncode, 0, res_clean.stderr)
        self.assertFalse(wt_path.exists())  # worktree dir is gone
        self.assertIn("warning:", res_clean.stderr)
        self.assertIn("delete refused", res_clean.stderr)

        # The branch and its commit must still exist -- nothing was lost.
        branches = subprocess.run(["git", "branch", "--list", "office/famG/runG/dispG"],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertIn("office/famG/runG/dispG", branches)

    def test_cleanup_run_skips_dirty_worktree_unless_forced(self):
        wt_path = self.tmp / "wt_dirty"
        subprocess.run([
            str(WORKTREE_SH), "create",
            "--family-id", "famD",
            "--run-id", "runD",
            "--dispatch-id", "dispD",
            "--worktree-path", str(wt_path)
        ], cwd=str(self.repo), check=True)

        state_dir = self.tmp / "stateD"
        disp_dir = state_dir / "dispatches" / "dispD"
        disp_dir.mkdir(parents=True, exist_ok=True)
        (disp_dir / "meta.json").write_text(json.dumps({"worktree": str(wt_path)}), encoding="utf-8")
        (state_dir / "state.json").write_text(json.dumps({"run_id": "runD"}), encoding="utf-8")

        # Make dirty
        (wt_path / "dirty.txt").write_text("uncommitted changes")

        # Cleanup without force
        res = subprocess.run([
            str(WORKTREE_SH), "cleanup-run",
            "--state-dir", str(state_dir),
            "--run-id", "runD"
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(wt_path.exists())

        # Cleanup with force
        res2 = subprocess.run([
            str(WORKTREE_SH), "cleanup-run",
            "--state-dir", str(state_dir),
            "--run-id", "runD",
            "--force"
        ], cwd=str(self.repo), capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0)
        self.assertFalse(wt_path.exists())
