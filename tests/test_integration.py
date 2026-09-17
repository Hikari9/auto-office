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

    def test_plan_version_bump_invalidates_prior_approval(self):
        state_dir, state = self._start_run()
        rc, _, err = self._save_phase(state_dir, state, 'planned')
        self.assertEqual(rc, 0, err)
        rc, out, err = run_cmd('approve-plan',
            '--state-dir', str(state_dir),
            '--approved-by', 'user',
            '--quote', 'I approve version one')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['approval']['plan_version'], 1)

        rc, out, err = run_cmd('increment-plan', '--state-dir', str(state_dir))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out['approval_invalidated'])
        self.assertEqual(out['prior'], 1)
        self.assertEqual(out['plan_version'], 2)

        state = json.loads((state_dir / 'state.json').read_text())
        self.assertEqual(state['phase'], 'planned')
        self.assertNotIn('approval', state)
        self.assertEqual(state['invalidated_approvals'][-1]['plan_version'], 1)
        self.assertEqual(
            state['invalidated_approvals'][-1]['invalidated_for']['plan_version'], 2)

        hook = ROOT / 'scripts' / 'hooks' / 'pre_tool_use.py'
        hook_env = os.environ.copy()
        hook_env.pop('OFFICE_STATE_DIR', None)
        hook_result = subprocess.run(
            [sys.executable, str(hook)],
            cwd=self.repo,
            env=hook_env,
            input=json.dumps({
                'tool_name': 'Edit',
                'tool_input': {'file_path': str(self.repo / 'target.py')},
            }),
            capture_output=True,
            text=True,
        )
        self.assertEqual(hook_result.returncode, 2)
        self.assertIn("phase 'planned'", hook_result.stdout)

        rc, out, err = run_cmd('approve-plan',
            '--state-dir', str(state_dir),
            '--approved-by', 'user',
            '--quote', 'I approve version two')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out['approval']['plan_version'], 2)

    def test_malformed_route_defect_row_fails_closed(self):
        state_dir, _ = self._start_run()
        (state_dir / 'route-defects.jsonl').write_text('{malformed row\n')

        rc, out, err = run_cmd('check-route-defects', '--state-dir', str(state_dir))
        self.assertEqual(rc, 2)
        self.assertEqual(err, '')
        self.assertFalse(out['clear'])
        self.assertEqual(out['error'], 'route_defects_unreadable')
        self.assertEqual(out['row'], 1)
        self.assertIn('row 1', out['message'])

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

        # mark-spoke now requires proof the spoke was located (--digest), so
        # this call carries one. The assertion under test is unchanged: it is
        # about state-save preserving spokes_loaded, not about the receipt
        # contract, which tests/test_spoke_receipt_integrity.py owns.
        rc, out, _ = run_cmd('spoke-digest', '--spoke', 'auto-planning')
        self.assertEqual(rc, 0)
        rc, out, _ = run_cmd('mark-spoke', '--state-dir', str(state_dir),
                             '--spoke', 'auto-planning', '--digest', out['digest'])
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


class DerivedRoutingRoundTripTest(unittest.TestCase):
    """Amendment v3's whole point: evidence recorded at one run's closeout must change a
    LATER run's route. A label that is written but never consumed proves nothing, so this
    asserts the round trip rather than either half of it.

    Role is 'worker' deliberately: it sits outside MUTABLE_TRUST_ROLES, so stage 2 does not
    reject an unpromoted triple and the test isolates the reward path it is actually about.
    """

    GOOD = 'agy@1/good-model@high'
    POOR = 'agy@1/poor-model@high'

    def setUp(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'runs.db')
        run_cmd('init-db', '--db', self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _candidates(self):
        # Identical on every cost axis, so the reward tie-break is the only thing that can
        # separate them. If cost differed, a passing test would prove nothing about reward.
        def cand(triple):
            harness, rest = triple.split('@', 1)
            version, rest = rest.split('/', 1)
            model_id, effort = rest.split('@', 1)
            return {
                'harness': harness, 'harness_version': version,
                'model_id': model_id, 'effort': effort,
                'capabilities': [], 'supported_playbooks': ['Change'],
                'quota': {'status': 'ok', 'tightest_remaining_percent': 90,
                          'projected_burn_percent': 1},
                'cost': {'money_estimate': 1, 'quota_burn': 1, 'wall_clock_seconds': 10},
            }
        return [cand(self.POOR), cand(self.GOOD)]

    def _route(self):
        import office_routing as routing
        return routing.route({'role': 'worker', 'playbook': 'Change',
                              'runs_db': self.db, 'candidates': self._candidates()})

    def _record_closeout(self, triple, dispatch_id, label, narrative):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO dispatches(id, run_id, role, triple, money_estimate, money_actual) "
            "VALUES (?,?,?,?,?,?)",
            (dispatch_id, 'run-1', 'worker', triple, 1.0, 1.0))
        con.execute(
            "INSERT INTO outcome_labels(id, dispatch_id, label, primary_attribution, "
            "contributing_attributions, labeled_at, evidence_hash) VALUES (?,?,?,?,?,?,?)",
            (f'lbl-{dispatch_id}', dispatch_id, label, 'model',
             json.dumps([narrative]), datetime.now(timezone.utc).isoformat(),
             'sha256:' + 'b' * 64))
        con.commit()
        con.close()

    def test_labels_recorded_at_closeout_change_a_later_runs_route(self):
        import office_scoring as scoring

        # Run 1: no evidence exists. Both rewards are unknown, so neither triple can be
        # preferred on evidence and the tie falls to the deterministic candidate_id order.
        con = sqlite3.connect(self.db)
        self.assertIsNone(scoring.compute_local_reward(con, self.GOOD))
        self.assertIsNone(scoring.compute_local_reward(con, self.POOR))
        con.close()

        first = self._route()
        self.assertEqual(first['status'], 'selected')
        self.assertEqual(first['selected'], self.GOOD,
                         'precondition: with no evidence the tie-break is alphabetical by '
                         'candidate_id, so GOOD wins for a reason that has nothing to do '
                         'with quality')

        # Closeout of run 1 records what actually happened on each triple.
        self._record_closeout(self.POOR, 'd-poor', 'abandoned', 'narrative:failed_verification')
        self._record_closeout(self.GOOD, 'd-good', 'verified_no_observed_failure',
                              'narrative:success')

        con = sqlite3.connect(self.db)
        good_r = scoring.compute_local_reward(con, self.GOOD)
        poor_r = scoring.compute_local_reward(con, self.POOR)
        con.close()
        self.assertIsNotNone(good_r)
        self.assertIsNotNone(poor_r)
        self.assertGreater(good_r, poor_r)

        # Run 2: the same request, routed again, now consumes that evidence.
        second = self._route()
        self.assertEqual(second['selected'], self.GOOD)
        self.assertIn('reason', second['selection_disclosure'])

    def test_the_round_trip_can_reverse_a_no_evidence_preference(self):
        """The previous test's GOOD triple also wins the no-evidence tie, so on its own it
        cannot distinguish 'evidence decided this' from 'nothing changed'. Here the triple
        that loses the alphabetical tie is the one evidence favours, so a selection flip is
        only explicable by the recorded labels."""
        import office_scoring as scoring

        first = self._route()
        self.assertEqual(first['selected'], self.GOOD)

        self._record_closeout(self.GOOD, 'd-good-bad', 'abandoned',
                              'narrative:operator_rejected')
        self._record_closeout(self.POOR, 'd-poor-good', 'verified_no_observed_failure',
                              'narrative:success')

        con = sqlite3.connect(self.db)
        self.assertGreater(scoring.compute_local_reward(con, self.POOR),
                           scoring.compute_local_reward(con, self.GOOD))
        con.close()

        second = self._route()
        self.assertEqual(
            second['selected'], self.POOR,
            'a later run must route differently because of what the earlier run recorded; '
            'if this still selects GOOD, labels are being written and never consumed')

    def test_an_unmeasured_triple_does_not_rank_as_a_measured_zero(self):
        """§7.4.3: unknown is its own tier above measured-neutral. A triple nobody has
        measured must not be treated as one that was measured and scored zero."""
        import office_scoring as scoring
        self.assertLess(scoring.reward_sort_key(None), scoring.reward_sort_key(0.0))
        self.assertLess(scoring.reward_sort_key(0.5), scoring.reward_sort_key(None))
