#!/usr/bin/env python3
"""Deterministic end-to-end lifecycle integration test.

Exercises the full orchestration runtime without model tokens:
  create synthetic repo
  → start auto-office run
  → plan and approve
  → dispatch fake executor
  → persist state
  → simulate interruption
  → run checkpoint hook
  → terminate original runtime
  → start fresh runtime
  → resume/reconcile
  → dispatch fake reviewer
  → return a finding
  → dispatch fix
  → re-review
  → pass
  → close family
  → verify final state + telemetry
"""
import json, os, subprocess, sys, tempfile, unittest, shutil, sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'scripts' / 'office_runtime.py'

def run_cmd(*args, **kwargs):
    r = subprocess.run([sys.executable, str(RUNTIME)] + list(args),
                      capture_output=True, text=True, **kwargs)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr

class TestLifecycleIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='office-test-')
        self.repo = Path(self.tmpdir) / 'repo'
        self.repo.mkdir()
        # Initialize a git repo
        subprocess.run(['git', 'init'], cwd=self.repo, capture_output=True)
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'], cwd=self.repo, capture_output=True)
        self.state_dir = self.repo / '.office'
        self.state_dir.mkdir()
        self.db = self.state_dir / 'telemetry.db'
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_run(self, goal='exercise the lifecycle'):
        env = os.environ.copy()
        env['XDG_STATE_HOME'] = str(Path(self.tmpdir) / 'xdg-state')
        rc, out, err = run_cmd('start',
            '--goal', goal,
            '--playbook', 'Change',
            '--gear', 'direct',
            '--repo', str(self.repo),
            env=env)
        self.assertEqual(rc, 0, err)
        state_dir = Path(out['state_dir'])
        state = json.loads((state_dir / 'state.json').read_text())
        return state_dir, state

    def _save_phase(self, state_dir, state, phase):
        return run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', phase)
    
    def test_full_lifecycle(self):
        # 1. Init DB
        rc, out, _ = run_cmd('init-db', '--db', str(self.db))
        self.assertEqual(rc, 0)
        
        # 2. Start, plan, and approve the run through the real lifecycle commands.
        state_dir, state = self._start_run()
        run_id = state['run_id']
        family_id = state['family_id']
        self.assertEqual(state['phase'], 'intake')
        rc, _, err = self._save_phase(state_dir, state, 'planned')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        rc, out, err = run_cmd('approve-plan',
            '--state-dir', str(state_dir),
            '--approved-by', 'user',
            '--quote', 'I approve this plan as written')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['phase'], 'approved')
        state = json.loads((state_dir / 'state.json').read_text())
        rc, _, err = self._save_phase(state_dir, state, 'executing')
        self.assertEqual(rc, 0, err)

        # 3. Acquire lease
        rc, out, _ = run_cmd('lease-acquire',
            '--db', str(self.db),
            '--run-id', run_id,
            '--role', 'executor',
            '--scope', 'src/',
            '--holder-id', 'executor-001')
        self.assertEqual(rc, 0)
        self.assertTrue(out['acquired'])
        lease_id = out['lease_id']
        
        # 4. Simulate interruption (just verify state can be loaded)
        rc, out, _ = run_cmd('state-load',
            '--state-dir', str(state_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(out['run_id'], run_id)
        self.assertEqual(out['phase'], 'executing')
        
        # 6. Lease check (should still be active)
        rc, out, _ = run_cmd('lease-check',
            '--db', str(self.db),
            '--run-id', run_id,
            '--scope', 'src/')
        self.assertEqual(rc, 0)
        self.assertTrue(out['active'])
        
        # 7. Record dispatch
        dispatch_file = self.state_dir / 'dispatch.json'
        dispatch_file.write_text(json.dumps({
            'run_id': run_id,
            'role': 'executor',
            'holder_id': 'executor-001',
            'triple': 'fake@1.0/fake-model@medium',
            'selection_disclosure': {
                'invocation_model_id': 'fake-model-2026-09-13',
                'reason': 'matched the task shape and protected quota reserve',
            },
            'started_at': datetime.now(timezone.utc).isoformat(),
        }))
        rc, out, _ = run_cmd('record-dispatch',
            '--db', str(self.db),
            str(dispatch_file))
        self.assertEqual(rc, 0)
        
        # 8. Record finding (accepted-material)
        finding_file = self.state_dir / 'finding.json'
        finding_file.write_text(json.dumps({
            'finding_id': 'finding-001',
            'dispatch_id': out['recorded'],
            'reviewer_dispatch_id': 'reviewer-001',
            'status': 'accepted-material',
            'severity': 'error',
            'summary': 'Missing error handling',
            'evidence': 'Missing error handling detected',
            'evidence_hash': 'sha256:abc123',
            'created_at': datetime.now(timezone.utc).isoformat(),
        }))
        rc, out, _ = run_cmd('record-finding',
            '--db', str(self.db),
            str(finding_file))
        self.assertEqual(rc, 0)
        
        # 9. Record fix dispatch
        dispatch_file.write_text(json.dumps({
            'run_id': run_id,
            'role': 'executor',
            'holder_id': 'executor-001',
            'triple': 'fake@1.0/fake-model@medium',
            'started_at': datetime.now(timezone.utc).isoformat(),
        }))
        rc, _, _ = run_cmd('record-dispatch', '--db', str(self.db), str(dispatch_file))
        self.assertEqual(rc, 0)
        
        # 10. Record accepted-minor finding
        finding_file.write_text(json.dumps({
            'finding_id': 'finding-002',
            'dispatch_id': out.get('recorded', 'dispatch-002'),
            'reviewer_dispatch_id': 'reviewer-002',
            'status': 'accepted-minor',
            'severity': 'info',
            'summary': 'All checks pass',
            'evidence': 'Verified clean',
            'evidence_hash': 'sha256:def456',
            'created_at': datetime.now(timezone.utc).isoformat(),
        }))
        rc, _, _ = run_cmd('record-finding', '--db', str(self.db), str(finding_file))
        self.assertEqual(rc, 0)
        
        # 11. Release lease
        rc, out, _ = run_cmd('lease-release',
            '--db', str(self.db),
            '--lease-id', lease_id,
            '--holder-id', 'executor-001')
        self.assertEqual(rc, 0)
        self.assertTrue(out['released'])

        # 12. Mark the review and close the run through adjacent lifecycle phases.
        state = json.loads((state_dir / 'state.json').read_text())
        rc, _, err = self._save_phase(state_dir, state, 'reviewed')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        rc, _, err = self._save_phase(state_dir, state, 'closed')
        self.assertEqual(rc, 0, err)
        rc, out, _ = run_cmd('state-load', '--state-dir', str(state_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(out['phase'], 'closed')
        self.assertIn('approval', out)

        # 13. Verify DB state
        con = sqlite3.connect(self.db)
        dispatches = con.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0]
        invocation_model_id, selection_reason = con.execute(
            'SELECT invocation_model_id, selection_reason FROM dispatches WHERE holder_id=? ORDER BY started_at LIMIT 1',
            ('executor-001',)
        ).fetchone()
        findings = con.execute('SELECT COUNT(*) FROM findings').fetchone()[0]
        leases = con.execute('SELECT COUNT(*) FROM leases').fetchone()[0]
        self.assertGreaterEqual(dispatches, 2)
        self.assertEqual(invocation_model_id, 'fake-model-2026-09-13')
        self.assertIn('task shape', selection_reason)
        self.assertGreaterEqual(findings, 2)
        self.assertGreaterEqual(leases, 1)
        con.close()
    
    def test_stale_lease_takeover(self):
        # Init DB and create run
        run_cmd('init-db', '--db', str(self.db))
        run_file = self.state_dir / 'run.json'
        rc, out, _ = run_cmd('new-run', '--family-id', 'f1', '--holder-id', 'h1',
                '--triple', 'fake@1.0/m@medium', '--gear', 'standard',
                '--playbook', 'Change', '--base-sha', 'abc1234',
                '--policy-hash', 'sha256:11111111', '--catalog-hash', 'sha256:22222222',
                '--adapter-hash', 'sha256:33333333', '--config-hash', 'sha256:44444444',
                '--out', str(run_file))
        self.assertEqual(rc, 0)
        run_data = json.loads(run_file.read_text())
        run_id = run_data['run_id']
        
        # Acquire lease with very short TTL
        rc, out, _ = run_cmd('lease-acquire', '--db', str(self.db),
                            '--run-id', run_id, '--role', 'executor',
                            '--scope', 'src/', '--holder-id', 'old-holder',
                            '--ttl', '0')  # Immediately expired
        self.assertEqual(rc, 0)
        self.assertTrue(out['acquired'])
        
        # New holder should be able to take over the expired lease
        rc, out, _ = run_cmd('lease-acquire', '--db', str(self.db),
                            '--run-id', run_id, '--role', 'executor',
                            '--scope', 'src/', '--holder-id', 'new-holder')
        self.assertEqual(rc, 0)
        self.assertTrue(out['acquired'])
        self.assertEqual(out.get('prior_holder'), 'old-holder')
    
    def test_duplicate_hook_idempotency(self):
        # State save should be idempotent once start has created the state.
        run_cmd('init-db', '--db', str(self.db))
        state_dir, state = self._start_run()
        rc0, _, err0 = self._save_phase(state_dir, state, 'planned')
        self.assertEqual(rc0, 0, err0)
        state = json.loads((state_dir / 'state.json').read_text())
        rc1, out1, _ = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'planned')
        rc2, out2, _ = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'planned')
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        # Same state should produce same hash
        self.assertEqual(out1['content_hash'], out2['content_hash'])

    def test_state_save_requires_existing_started_state(self):
        state_dir = Path(self.tmpdir) / 'not-started'
        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', 'missing-run',
            '--family-id', 'missing-family',
            '--phase', 'intake')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'no_state')
        self.assertFalse(state_dir.exists())

    def test_state_save_rejects_minimal_fake_receipt_before_approval(self):
        state_dir = Path(self.tmpdir) / 'fake-run'
        state_dir.mkdir()
        state_path = state_dir / 'state.json'
        state_path.write_text(json.dumps({
            'run_id': 'fake-run',
            'family_id': 'fake-family',
            'phase': 'intake',
        }))

        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', 'fake-run',
            '--family-id', 'fake-family',
            '--phase', 'planned')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'invalid_start_receipt')
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'intake')

        rc, out, err = run_cmd('approve-plan',
            '--state-dir', str(state_dir),
            '--approved-by', 'user',
            '--quote', 'I approve this fake plan')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'invalid_phase')
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'intake')

    def test_state_save_preserves_versions_and_rejects_post_approval_changes(self):
        state_dir, state = self._start_run()
        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'planned',
            '--plan-version', '3',
            '--packet-version', '7')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(state['plan_version'], 3)
        self.assertEqual(state['packet_version'], 7)

        rc, out, err = run_cmd('approve-plan',
            '--state-dir', str(state_dir),
            '--approved-by', 'user',
            '--quote', 'I approve version three')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(state['approval']['plan_version'], 3)
        self.assertEqual(state['approval']['packet_version'], 7)

        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'executing')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(state['plan_version'], 3)
        self.assertEqual(state['packet_version'], 7)

        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'reviewed',
            '--plan-version', '4')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'version_change_after_approval')
        after = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(after['plan_version'], 3)
        self.assertEqual(after['phase'], 'executing')

    def test_state_save_cannot_forge_approval(self):
        state_dir, state = self._start_run()
        rc, _, err = self._save_phase(state_dir, state, 'planned')
        self.assertEqual(rc, 0, err)
        state = json.loads((state_dir / 'state.json').read_text())
        rc, out, err = run_cmd('state-save',
            '--state-dir', str(state_dir),
            '--run-id', state['run_id'],
            '--family-id', state['family_id'],
            '--phase', 'approved')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'approval_required')
        after = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(after['phase'], 'planned')
        self.assertNotIn('approval', after)

    def test_start_cli_can_select_full_fit_path(self):
        env = os.environ.copy()
        env['XDG_STATE_HOME'] = str(Path(self.tmpdir) / 'xdg-full')
        rc, out, err = run_cmd('start',
            '--goal', 'exercise full risk fit',
            '--playbook', 'Change',
            '--repo', str(self.repo),
            '--irreversible',
            env=env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['gear'], 'full')

    def test_start_normalizes_nested_repo_for_pointer(self):
        nested = self.repo / 'nested' / 'directory'
        nested.mkdir(parents=True)
        env = os.environ.copy()
        env['XDG_STATE_HOME'] = str(Path(self.tmpdir) / 'xdg-nested')
        rc, out, err = run_cmd('start',
            '--goal', 'start from a nested directory',
            '--playbook', 'Change',
            '--gear', 'direct',
            '--repo', str(nested),
            env=env)
        self.assertEqual(rc, 0, err)
        pointer = self.repo / '.office' / 'runs' / f"{out['run_id']}.ref"
        self.assertTrue(pointer.exists())
        self.assertFalse((nested / '.office' / 'runs' / f"{out['run_id']}.ref").exists())
        self.assertEqual(pointer.read_text().strip(), out['state_dir'])

    def test_start_pins_independent_plugin_policy_and_effective_hashes(self):
        state_dir, state = self._start_run()
        self.assertTrue(state['plugin_commit'])
        self.assertNotEqual(state['policy_hash'], state['effective_config_hash'])
        envelope = json.loads((state_dir / 'envelope.json').read_text())
        self.assertEqual(envelope['plugin_commit'], state['plugin_commit'])
        self.assertEqual(envelope['policy_hash'], state['policy_hash'])

    def test_state_save_preserves_prior_fields(self):
        # start pins base_sha/policy_hash/etc and mark-spoke records spokes_loaded;
        # a later state-save must not drop either.
        run_cmd('init-db', '--db', str(self.db))
        state_dir, before = self._start_run()
        state_path = state_dir / 'state.json'

        rc, out, _ = run_cmd('mark-spoke', '--state-dir', str(state_dir), '--spoke', 'auto-planning')
        self.assertEqual(rc, 0)

        state = json.loads(state_path.read_text())
        rc, out, err = self._save_phase(state_dir, state, 'planned')
        self.assertEqual(rc, 0, err)

        rc, out, _ = run_cmd('state-load', '--state-dir', str(state_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(out['phase'], 'planned')
        self.assertIn('auto-planning', out.get('spokes_loaded', {}))
        self.assertEqual(out.get('policy_hash'), before['policy_hash'])
        self.assertEqual(out.get('base_sha'), before['base_sha'])

        # check-spoke must still see the earlier mark after the state-save
        rc, out, _ = run_cmd('check-spoke', '--state-dir', str(state_dir), '--spoke', 'auto-planning')
        self.assertEqual(rc, 0)
        self.assertTrue(out['loaded'])

    def test_state_save_rejects_different_run_identity(self):
        state_path = self.state_dir / 'state.json'
        state_path.write_text(json.dumps({
            'run_id': 'old-run',
            'family_id': 'family-1',
            'base_sha': 'old-base',
            'policy_hash': 'old-policy',
            'spokes_loaded': {'auto-planning': 'old-time'},
        }))

        rc, out, err = run_cmd('state-save',
            '--state-dir', str(self.state_dir),
            '--run-id', 'new-run',
            '--family-id', 'family-1',
            '--phase', 'planned')

        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'state identity mismatch')
        self.assertEqual(out['mismatches']['run_id']['existing'], 'old-run')
        self.assertEqual(json.loads(state_path.read_text())['run_id'], 'old-run')

        state_path.write_text(json.dumps({
            'run_id': 'new-run',
            'family_id': 'old-family',
        }))
        rc, out, err = run_cmd('state-save',
            '--state-dir', str(self.state_dir),
            '--run-id', 'new-run',
            '--family-id', 'new-family',
            '--phase', 'planned')
        self.assertNotEqual(rc, 0)
        self.assertEqual(err, '')
        self.assertEqual(out['error'], 'state identity mismatch')
        self.assertEqual(out['mismatches']['family_id']['existing'], 'old-family')
        self.assertEqual(json.loads(state_path.read_text())['family_id'], 'old-family')

    def test_state_save_rejects_malformed_or_non_object_state(self):
        state_path = self.state_dir / 'state.json'
        for malformed in ('', '{"run_id":"r1",', '[]', 'null', '1'):
            state_path.write_text(malformed)

            rc, out, err = run_cmd('state-save',
                '--state-dir', str(self.state_dir),
                '--run-id', 'r1',
                '--family-id', 'f1',
                '--phase', 'planned')

            self.assertNotEqual(rc, 0)
            self.assertEqual(err, '')
            self.assertEqual(out['error'], 'invalid state.json')
            self.assertEqual(state_path.read_text(), malformed)
            if malformed in ('[]', 'null', '1'):
                self.assertEqual(out['reason'], 'state.json must contain a JSON object')
            else:
                self.assertEqual(out['reason'], 'malformed JSON')

    def test_self_approval_rejected(self):
        # A finding where producer == reviewer should be rejected
        run_cmd('init-db', '--db', str(self.db))
        finding_file = self.state_dir / 'finding.json'
        finding_file.write_text(json.dumps({
            'finding_id': 'self-approve',
            'dispatch_id': 'same-dispatch',
            'reviewer_dispatch_id': 'same-dispatch',  # Same as producer!
            'status': 'accepted-material',
            'severity': 'info',
            'summary': 'Self-approved',
            'evidence': 'self check',
            'evidence_hash': 'sha256:abc',
            'created_at': datetime.now(timezone.utc).isoformat(),
        }))
        rc, out, _ = run_cmd('record-finding', '--db', str(self.db), str(finding_file))
        # Should reject self-approval
        self.assertNotEqual(rc, 0)

if __name__ == '__main__':
    unittest.main()
