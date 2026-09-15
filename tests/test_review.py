#!/usr/bin/env python3
import hashlib, importlib.util, json, os, sqlite3, subprocess, sys, tempfile, unittest, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fam = _load("office_family", "scripts/office_family.py")
rt = _load("office_runtime", "scripts/office_runtime.py")

FIXED_EMPTY_HASH = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestReview(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='office-review-test-')
        self.repo = Path(self.tmpdir)
        subprocess.run(['git', 'init'], cwd=self.repo, capture_output=True)
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'], cwd=self.repo, capture_output=True)
        self.state_dir = self.repo / '.office'
        self.state_dir.mkdir()
        self.db = self.state_dir / 'telemetry.db'
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'office_runtime.py'), 'init-db', '--db', str(self.db)], capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _head_sha(self):
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, capture_output=True,
                               text=True, check=True).stdout.strip()

    def _write_review(self, **overrides):
        review = {
            "review_id": "rev-res-001",
            "dispatch_id": "disp-001",
            "producer_id": "disp-001",
            "reviewer_id": "rev-001",
            "reviewer_triple": "codex@local/gpt-5.6-luna@xhigh",
            "review_mode": "independent_adversary",
            "reviewed_head_sha": self._head_sha(),
            "requirements_version": 1,
            "plan_version": 1,
            "routing_version": 1,
            "review_scope": ["T4"],
            "disposition_owner": "executor",
            "overall_status": "PASS",
            "findings": [],
            "evidence": "Full test suite passed cleanly under independent review.",
            "evidence_hash": "sha256:" + hashlib.sha256(b"independent review evidence").hexdigest(),
            "created_at": "2026-09-15T00:00:00Z",
        }
        review.update(overrides)
        path = self.state_dir / f"review-{review['review_id']}.json"
        path.write_text(json.dumps(review), encoding='utf-8')
        return path

    def _write_packet(self, commands=("true",)):
        """A packet carrying real validation commands.

        The fixture repo has no package.json/Cargo.toml/pyproject.toml, so verify.sh cannot
        infer a command for it. Without a packet nothing executes, and a verification that
        executed nothing is now reported as failed rather than passed -- so every loop test
        that wants to reach the review gate has to actually verify something first.
        """
        packet = {"validation_commands": list(commands)}
        path = self.state_dir / 'packet.json'
        path.write_text(json.dumps(packet), encoding='utf-8')
        return path

    def _run_loop(self, extra_args=(), env=None, packet=True):
        loop_script = ROOT / 'scripts' / 'review_loop.sh'
        full_env = {**os.environ}
        if env:
            full_env.update(env)
        args = [
            str(loop_script),
            '--state-dir', str(self.state_dir),
            '--dispatch-id', 'disp-001',
            '--reviewer-dispatch-id', 'rev-001',
            '--worktree', str(self.repo),
            '--db', str(self.db),
        ]
        if packet:
            args += ['--packet', str(self._write_packet())]
        return subprocess.run([*args, *extra_args],
                              cwd=self.repo, capture_output=True, text=True, env=full_env)

    def test_review_finding_persist(self):
        script = ROOT / 'scripts' / 'review_finding.sh'
        r = subprocess.run([
            str(script),
            '--dispatch-id', 'disp-001',
            '--reviewer-dispatch-id', 'rev-001',
            '--status', 'IMPLEMENTATION_DEFECT',
            '--summary', 'Found a bug',
            '--state-dir', str(self.repo),
            '--db', str(self.db)
        ], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        finding_id = r.stdout.strip()
        self.assertTrue(finding_id)
        finding_file = self.repo / '.office' / 'findings' / f'{finding_id}.json'
        self.assertTrue(finding_file.exists())

    def test_review_loop_self_approval_rejected(self):
        loop_script = ROOT / 'scripts' / 'review_loop.sh'
        r = subprocess.run([
            str(loop_script),
            '--state-dir', str(self.state_dir),
            '--dispatch-id', 'same-dispatch',
            '--reviewer-dispatch-id', 'same-dispatch',
            '--worktree', str(self.repo),
            '--db', str(self.db)
        ], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        self.assertIn('Self-approval rejection', r.stdout + r.stderr)

    def test_verify_script(self):
        verify_script = ROOT / 'scripts' / 'verify.sh'
        r = subprocess.run([
            str(verify_script),
            '--worktree', str(self.repo),
            '--dispatch-id', 'disp-001',
            '--state-dir', str(self.state_dir),
            '--db', str(self.db)
        ], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertIn('passed', out)
        self.assertIn('gates', out)

    # ---- amendment v2 finding F3: positive-path provenance ----

    def test_review_loop_unset_review_source_is_unavailable(self):
        """No --review-file at all: PASS must be unavailable, never granted."""
        r = self._run_loop()
        self.assertEqual(r.returncode, 4)
        self.assertIn('unset_review_source', r.stdout + r.stderr)
        self.assertNotIn('"PASS"', r.stdout + r.stderr)

    def test_review_loop_review_status_env_var_is_ignored(self):
        """The exact original defect: REVIEW_STATUS=PASS must have no effect
        now that run_review() no longer reads any environment variable."""
        r = self._run_loop(env={'REVIEW_STATUS': 'PASS'})
        self.assertEqual(r.returncode, 4)
        self.assertIn('unset_review_source', r.stdout + r.stderr)

    def test_review_loop_reviewer_identity_mismatch_is_unavailable(self):
        review_path = self._write_review(reviewer_id='someone-else')
        r = self._run_loop(extra_args=('--review-file', str(review_path)))
        self.assertEqual(r.returncode, 4)
        self.assertIn('reviewer_identity_mismatch', r.stdout + r.stderr)

    def test_review_loop_stale_tree_sha_is_rejected(self):
        review_path = self._write_review(reviewed_head_sha='0' * 40)
        r = self._run_loop(extra_args=('--review-file', str(review_path)))
        self.assertEqual(r.returncode, 4)
        self.assertIn('stale_tree_sha', r.stdout + r.stderr)

    def test_review_loop_mismatched_version_is_rejected(self):
        fam.register_family(self.state_dir, 'sess-001', 'fam-t4', 'acme/repo', 1,
                             requirements_version=1, plan_version=1, routing_version=1)
        review_path = self._write_review(family_id='fam-t4', plan_version=2)
        r = self._run_loop(extra_args=('--review-file', str(review_path), '--review-scope', 'T4'))
        self.assertEqual(r.returncode, 4)
        self.assertIn('version_mismatch', r.stdout + r.stderr)

    def test_review_loop_scope_mismatch_is_rejected(self):
        review_path = self._write_review()
        r = self._run_loop(extra_args=('--review-file', str(review_path), '--review-scope', 'T99'))
        self.assertEqual(r.returncode, 4)
        self.assertIn('scope_mismatch', r.stdout + r.stderr)

    # ---- F1: a verification that executed nothing is not a pass ----

    def test_verify_reports_failure_when_no_gate_executed(self):
        """The fixture repo has no project markers and no packet, so nothing can run.

        Reporting that as a pass is how an unknown project type earned a full green without
        executing a single command -- four gates had a permanently empty command and could
        never fail.
        """
        verify = ROOT / 'scripts' / 'verify.sh'
        r = subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'v-1',
                            '--state-dir', str(self.state_dir), '--db', str(self.db)],
                           capture_output=True, text=True)
        out = json.loads(r.stdout)
        self.assertFalse(out['passed'])
        self.assertEqual(out['reason'], 'no_gate_executed')
        self.assertEqual(out['executed'], 0)
        self.assertEqual(out['skipped'], 8)

    def test_a_skipped_gate_records_no_validation_row_and_no_evidence(self):
        """sha256("") is not proof. A gate that ran nothing produces no receipt at all."""
        verify = ROOT / 'scripts' / 'verify.sh'
        subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'v-2',
                        '--state-dir', str(self.state_dir), '--db', str(self.db)],
                       capture_output=True, text=True)
        with sqlite3.connect(self.db) as con:
            rows = con.execute("SELECT COUNT(*) FROM validations WHERE dispatch_id = 'v-2'").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_a_packet_command_that_fails_fails_the_verification(self):
        """The control for the two tests above: with a real command, the gate can fail.

        Without this, `passed: false` might mean the gate merely never runs.
        """
        verify = ROOT / 'scripts' / 'verify.sh'
        packet = self._write_packet(commands=("exit 1",))
        r = subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'v-3',
                            '--state-dir', str(self.state_dir), '--db', str(self.db),
                            '--packet', str(packet)], capture_output=True, text=True)
        out = json.loads(r.stdout)
        self.assertFalse(out['passed'])
        self.assertEqual(out['reason'], 'a gate failed')
        self.assertEqual(out['executed'], 1)
        targeted = [g for g in out['gates'] if g['name'] == 'targeted_tests'][0]
        self.assertFalse(targeted['passed'])

        ok_packet = self._write_packet(commands=("true",))
        r2 = subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'v-4',
                             '--state-dir', str(self.state_dir), '--db', str(self.db),
                             '--packet', str(ok_packet)], capture_output=True, text=True)
        self.assertTrue(json.loads(r2.stdout)['passed'])

    def test_a_quoted_packet_command_still_records_its_validation_row(self):
        """R1: the validation row was hand-written JSON with $cmd interpolated raw.

        A packet command containing a double quote -- python3 -c "...", pytest -k "a or b" --
        produced malformed JSON, record-validation failed, and `|| true` swallowed it. The gate
        reported a pass with no row in runs.db, which is exactly the row record_landing now
        requires before a landing can be recorded.
        """
        verify = ROOT / 'scripts' / 'verify.sh'
        packet = self._write_packet(commands=('python3 -c "print(1)"',))
        r = subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'q-1',
                            '--state-dir', str(self.state_dir), '--db', str(self.db),
                            '--packet', str(packet)], capture_output=True, text=True)
        out = json.loads(r.stdout)
        self.assertTrue(out['passed'], r.stdout + r.stderr)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT command FROM validations WHERE dispatch_id = 'q-1'").fetchall()
        self.assertEqual(len(rows), 1, "gate passed but recorded no validation row")
        self.assertEqual(rows[0][0], 'python3 -c "print(1)"')

    def test_no_gate_executed_is_unverifiable_not_an_implementation_defect(self):
        """R2: the loop turned "nothing ran" into an IMPLEMENTATION_DEFECT finding and an
        `abandoned` outcome label attributed to the producer, which lowers that harness
        triple's derived reward. A forgotten --packet must not degrade a harness's routing
        score for a run in which it was never measured.
        """
        review_path = self._write_review()
        r = self._run_loop(packet=False,
                           extra_args=('--max-iterations', '1', '--review-file', str(review_path)))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn('UNVERIFIABLE', r.stdout + r.stderr)
        self.assertNotIn('MAX_ITERATIONS', r.stdout + r.stderr)
        with sqlite3.connect(self.db) as con:
            labels = con.execute("SELECT label FROM outcome_labels").fetchall()
            findings = con.execute("SELECT id FROM findings").fetchall()
        self.assertEqual(labels, [], "an unverifiable run was labelled anyway")
        self.assertEqual(findings, [], "an unverifiable run was recorded as a defect")

    def test_known_bad_proven_is_zero_when_no_control_was_declared(self):
        """R3: `known_bad_proven` asserts the declared control ran and passed -- nothing more,
        and nothing at all when the packet declares no control."""
        verify = ROOT / 'scripts' / 'verify.sh'
        packet = self._write_packet(commands=("true",))
        subprocess.run([str(verify), '--worktree', str(self.repo), '--dispatch-id', 'kb-1',
                        '--state-dir', str(self.state_dir), '--db', str(self.db),
                        '--packet', str(packet)], capture_output=True, text=True)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT kind, known_bad_proven FROM validations WHERE dispatch_id = 'kb-1'").fetchall()
        self.assertNotIn('known_bad_controls', [k for k, _ in rows])
        self.assertTrue(all(v == 0 for _, v in rows), rows)

    def test_self_verification_failure_reaches_the_defect_exit(self):
        """Previously unreachable: every loop test ran against a repo whose verification was
        a vacuous green, so the IMPLEMENTATION_DEFECT branch could not be entered at all."""
        failing = self._write_packet(commands=("exit 1",))
        review_path = self._write_review()
        r = self._run_loop(packet=False,
                           extra_args=('--packet', str(failing), '--max-iterations', '1',
                                       '--review-file', str(review_path)))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('MAX_ITERATIONS reached during self-verification', r.stdout + r.stderr)

    def test_review_loop_valid_reviewer_dispatch_passes_and_labels_dispatch(self):
        """The success path: a validated reviewer dispatch and readback bound to
        distinct producer/reviewer identity, current tree SHA, all three
        versions, declared scope and non-empty evidence produces a real PASS
        -- and amendment v3 closeout records exactly one outcome label citing
        that same evidence, never a fixed/empty-string hash."""
        fam.register_family(self.state_dir, 'sess-001', 'fam-t4', 'acme/repo', 1,
                             requirements_version=1, plan_version=1, routing_version=1)
        review_path = self._write_review(family_id='fam-t4')
        r = self._run_loop(extra_args=('--review-file', str(review_path), '--review-scope', 'T4'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT dispatch_id, label, evidence_hash FROM outcome_labels"
        ).fetchall()
        con.close()
        self.assertEqual(len(rows), 1)
        dispatch_id, label, evidence_hash = rows[0]
        self.assertEqual(dispatch_id, 'disp-001')
        self.assertEqual(label, 'verified_no_observed_failure')
        self.assertTrue(evidence_hash)
        self.assertNotEqual(evidence_hash, FIXED_EMPTY_HASH)
        self.assertRegex(evidence_hash, r'^sha256:[0-9a-f]{64}$')

    # ---- office_readback.sh: process diagnostics + semantic landing evidence ----

    def _readback_dispatch_dir(self, dispatch_id, exit_code=0, log_text="all good\n"):
        d = self.state_dir / 'dispatches' / dispatch_id
        d.mkdir(parents=True)
        (d / 'meta.json').write_text(json.dumps({'adapter': 'adapters/seed/claude.yaml'}), encoding='utf-8')
        (d / 'output.log').write_text(log_text, encoding='utf-8')
        (d / 'exit_code').write_text(str(exit_code), encoding='utf-8')
        return d

    def _run_readback(self, dispatch_id, landing_file=None):
        script = ROOT / 'scripts' / 'office_readback.sh'
        args = [str(script), '--dispatch-id', dispatch_id, '--state-dir', str(self.state_dir)]
        if landing_file:
            args += ['--landing-file', str(landing_file)]
        return subprocess.run(args, capture_output=True, text=True)

    def test_office_readback_classifies_success_without_crashing(self):
        """Regression: every check_signature() chain used to leave a non-zero
        exit status behind when no failure signature matched (the common
        case), which set -e turned into a full script abort before
        classification ever ran."""
        self._readback_dispatch_dir('disp-ok')
        r = self._run_readback('disp-ok')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['classification'], 'SUCCESS')
        self.assertIsNone(out['landing_verified'])

    def _write_landing(self, dispatch_id, head_sha):
        landing = {
            'landing_id': 'land-001',
            'family_id': 'fam-t4',
            'producer': {'dispatch_id': dispatch_id, 'holder_id': 'h1', 'role': 'executor',
                         'triple': 'agy@local/m@medium'},
            'scope': 'T4',
            'requirements_version': 1,
            'plan_version': 1,
            'routing_version': 1,
            'base_sha': 'a' * 7,
            'head_sha': head_sha,
            'diff_stat': '1 file changed',
            'completed_tasks': ['T4'],
            'decisions': [],
            'changes_and_interfaces': [],
            'validation_evidence': {'commands': ['echo ok'], 'passed': True,
                                     'evidence_hash': 'sha256:' + 'a' * 64},
            'review': {'mode': 'exempt', 'round': 1, 'dispositions': []},
            'deviations': [],
            'dependencies_and_artifacts': [],
            'blockers': [],
            'created_at': '2026-09-15T00:00:00Z',
        }
        path = self.state_dir / 'landing.json'
        path.write_text(json.dumps(landing), encoding='utf-8')
        return path

    def test_office_readback_validates_semantic_landing_evidence(self):
        self._readback_dispatch_dir('disp-ok')
        landing_path = self._write_landing('disp-ok', rt._start_base_sha(ROOT))
        r = self._run_readback('disp-ok', landing_file=landing_path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['classification'], 'SUCCESS')
        self.assertTrue(out['landing_verified'])

    def test_office_readback_flags_unverifiable_landing_without_hiding_success(self):
        self._readback_dispatch_dir('disp-ok')
        landing_path = self._write_landing('disp-ok', 'stale' + '0' * 34)
        r = self._run_readback('disp-ok', landing_file=landing_path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        # process diagnostics are retained even though the landing is bad evidence
        self.assertEqual(out['classification'], 'SUCCESS')
        self.assertFalse(out['landing_verified'])
        self.assertTrue(out['landing_reason'])

    # ---- office_spawn.sh: optional start-receipt wiring ----

    def test_office_spawn_without_receipt_flags_is_unchanged(self):
        """Backward compatibility: omitting the new optional flags must not
        attempt to record a start receipt (existing callers, e.g.
        tests/test_dogfood.py, never pass them)."""
        adapter = self.repo / 'adapter.yaml'
        adapter.write_text(
            "invocation:\n  executable: /bin/sleep\n  argv:\n    - \"2\"\n  prompt_transport: argv\n",
            encoding='utf-8',
        )
        r = subprocess.run([
            str(ROOT / 'scripts' / 'office_spawn.sh'),
            '--adapter', str(adapter), '--model', 'm', '--effort', 'low',
            '--worktree', str(self.repo), '--dispatch-id', 'disp-nospawn',
            '--run-id', 'run-1', '--state-dir', str(self.state_dir), '--timeout', '5',
        ], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.state_dir / 'dispatches' / 'disp-nospawn' / 'start_receipt.json').exists())

    def test_office_spawn_with_full_disclosure_records_start_receipt(self):
        adapter = self.repo / 'adapter.yaml'
        adapter.write_text(
            "invocation:\n  executable: /bin/sleep\n  argv:\n    - \"2\"\n  prompt_transport: argv\n",
            encoding='utf-8',
        )
        disclosure = json.dumps({
            "role": "executor", "triple": "agy@local/gemini@medium",
            "invocation_model_id": "gemini", "model_id": "gemini", "effort": "medium",
            "harness": "agy", "harness_version": "local", "reason": "test",
        })
        r = subprocess.run([
            str(ROOT / 'scripts' / 'office_spawn.sh'),
            '--adapter', str(adapter), '--model', 'gemini', '--effort', 'medium',
            '--worktree', str(self.repo), '--dispatch-id', 'disp-recpt', '--run-id', 'run-1',
            '--state-dir', str(self.state_dir), '--timeout', '5',
            '--session-id', 'sess-1', '--family-id', 'fam-1',
            '--requirements-version', '1', '--plan-version', '1', '--routing-version', '1',
            '--effective-config-hash', 'sha256:' + ('a' * 64),
            '--selection-disclosure', disclosure,
        ], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        receipt = json.loads((self.state_dir / 'dispatches' / 'disp-recpt' / 'start_receipt.json').read_text())
        self.assertEqual(receipt['dispatch_id'], 'disp-recpt')
        self.assertEqual(receipt['session_id'], 'sess-1')
        self.assertEqual(receipt['family_id'], 'fam-1')
        self.assertEqual(receipt['selection_disclosure']['harness'], 'agy')

if __name__ == '__main__':
    unittest.main()
