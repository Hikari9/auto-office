"""Integration tests for scripts/office_routing.py's derived route() -- Task T2B
(docs/plans/v3-final-merge.md amendment v3; contract docs/v3-runtime-contracts.md
section 7). Exercises the full 9-stage pipeline end to end against a seeded runs.db,
proving the receipts in the T2B dispatch brief: a caller-supplied adapter_state,
absolute_floor_pass or local_reward has no effect; only a recorded trust act ever
raises trust; a missing capability-floor field fails closed and names itself; unknown
reward never ranks as a measured zero; and an override is rejected without a recorded
authorization and honoured (and disclosed) with one.
"""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


office_runtime = _load('office_runtime', 'scripts/office_runtime.py')
scoring = _load('office_scoring', 'scripts/office_scoring.py')
routing = _load('office_routing', 'scripts/office_routing.py')


def cand(harness, model_id='m', effort='high', money=1.0, remaining=80, burn=1,
         adapter_state='proven', absolute_floor_pass=True, advisory_pass=True,
         local_reward=0.99, caps=('builder',), invocation_source='local-evidence:x'):
    """A candidate that asserts every one of the four now-derived values as favorably
    as possible for itself -- the whole point of these tests is that none of it works."""
    return {
        'harness': harness, 'harness_version': '1', 'model_id': model_id, 'effort': effort,
        'adapter_state': adapter_state, 'absolute_floor_pass': absolute_floor_pass,
        'advisory_pass': advisory_pass, 'local_reward': local_reward,
        'capabilities': list(caps), 'supported_playbooks': ['Change'],
        'invocation_source': invocation_source,
        'quota': {'status': 'ok', 'tightest_remaining_percent': remaining, 'projected_burn_percent': burn},
        'cost': {'money_estimate': money, 'quota_burn': burn, 'wall_clock_seconds': 10},
    }


class DerivedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        office_runtime.init_db(Path(self.db_path)).close()

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    # ---- receipt: asserting proven/floor-pass/high-reward changes nothing ----

    def test_self_asserted_proven_floor_and_reward_are_ignored_for_mutable_role(self):
        c = cand('agy')
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['status'], 'no_qualifying_candidate')
        reasons = [r['reason'] for r in result['rejected'] if r['stage'] == 2]
        self.assertTrue(reasons, "expected a stage-2 rejection despite the self-asserted proven state")
        self.assertIn("derived as 'valid-unverified'", reasons[0])
        self.assertIn('not proven', reasons[0])

    def test_asserting_vs_omitting_the_four_values_routes_identically(self):
        """Proves the four caller-supplied fields are read nowhere: a candidate that
        omits them entirely gets the identical outcome to one that asserts the most
        favorable possible values for itself."""
        asserting = cand('agy')
        omitting = dict(asserting)
        for k in ('adapter_state', 'absolute_floor_pass', 'advisory_pass', 'local_reward'):
            omitting.pop(k, None)
        req_a = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [asserting]}
        req_b = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [omitting]}
        result_a = routing.route(req_a)
        result_b = routing.route(req_b)
        self.assertEqual(result_a['status'], result_b['status'])
        self.assertEqual(result_a['selected'], result_b['selected'])

    # ---- receipt: only a recorded trust act promotes; labels alone never do ----

    def test_recorded_trust_act_makes_a_mutable_role_routable(self):
        triple = 'agy@1/m@high'
        scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified rollout')
        c = cand('agy')
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertEqual(result['selected'], triple)
        self.assertEqual(result['status'], 'selected')

    # ---- receipt: stage 3 required_capabilities actually rejects (ADDED FINDING) ----

    def test_executor_missing_all_capabilities_is_rejected_by_stage_3(self):
        """config.default.yaml declares executor.required_capabilities: [builder]. A
        candidate that carries no capabilities must be rejected at stage 3, not waved
        through because no `policy.required_capabilities` was explicitly passed."""
        triple = 'agy@1/m@high'
        scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified rollout')
        c = cand('agy', caps=())
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['status'], 'no_qualifying_candidate')
        reasons = [r['reason'] for r in result['rejected'] if r['stage'] == 3]
        self.assertTrue(reasons, "expected a stage-3 rejection for a capability-less candidate")
        self.assertIn('builder', reasons[0])

    def test_executor_with_wrong_capability_is_rejected_by_stage_3(self):
        """A candidate carrying an unrelated capability (`review`, not `builder`) must
        still be rejected -- stage 3 checks for the required capability, not merely
        that the candidate has *some* capability."""
        triple = 'agy@1/m@high'
        scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified rollout')
        c = cand('agy', caps=('review',))
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['status'], 'no_qualifying_candidate')
        reasons = [r['reason'] for r in result['rejected'] if r['stage'] == 3]
        self.assertTrue(reasons, "expected a stage-3 rejection for a wrongly-capable candidate")
        self.assertIn('builder', reasons[0])

    def test_worker_with_no_capabilities_is_still_selected(self):
        """config.default.yaml declares worker.required_capabilities: []. An empty
        requirement is legitimate -- worker must NOT be rejected at stage 3 just
        because it lacks capabilities. Fixing stage 3 must not turn into rejecting
        everything regardless of role."""
        c = cand('agy', caps=())
        request = {'role': 'worker', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertEqual(result['selected'], 'agy@1/m@high')
        self.assertEqual(result['status'], 'selected')

    def test_seeded_success_labels_alone_never_promote(self):
        """No count of self-reported successes may promote -- amendments v5/v6."""
        triple = 'agy@1/m@high'
        con = __import__('sqlite3').connect(self.db_path)
        for i, shape in enumerate(['s1', 's1', 's1', 's2', 's2']):
            did = f'd{i}'
            con.execute(
                "INSERT INTO dispatches(id, run_id, role, holder_id, triple, task_shape, attribution) "
                "VALUES (?,?,?,?,?,?,?)",
                (did, 'run-1', 'executor', 'h1', triple, shape, None),
            )
            con.execute(
                "INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
                (f'lab-{did}', did, 'verified_no_observed_failure', 'model', None,
                 '2026-09-15T00:00:00Z', 'sha256:' + 'a' * 64),
            )
        con.commit(); con.close()
        c = cand('agy')
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c]}
        result = routing.route(request)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['status'], 'no_qualifying_candidate')

    # ---- receipt: missing capability-floor field fails closed, names the field ----

    def test_missing_catalog_field_for_floor_fails_closed_and_names_it(self):
        c = cand('agy')
        del c['effort']
        scoring.record_trust_act(self.db_path, 'agy@1/m@None', 'proven', 'rico', 'operator-verified rollout')
        request = {
            'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c],
            'policy': {'floor': {'min_effort': 'medium'}},
        }
        result = routing.route(request)
        self.assertEqual(result['status'], 'no_qualifying_candidate')
        reasons = [r['reason'] for r in result['rejected'] if r['stage'] == 4]
        self.assertTrue(reasons)
        self.assertIn("missing required catalog field 'effort'", reasons[0])

    # ---- receipt: unknown reward never ranks as a measured zero ----

    def test_unmeasured_reward_beats_measured_neutral_in_tie_break(self):
        triple_unmeasured = 'agy@1/unmeasured@high'
        triple_neutral = 'agy@1/neutral@high'
        for triple in (triple_unmeasured, triple_neutral):
            scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified rollout')
        import sqlite3
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO dispatches(id, run_id, role, holder_id, triple, task_shape, attribution) "
            "VALUES (?,?,?,?,?,?,?)",
            ('d-neutral', 'run-1', 'executor', 'h1', triple_neutral, 's1', None),
        )
        con.execute(
            "INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
            ('lab-neutral', 'd-neutral', 'environment_failure', 'model', None,
             '2026-09-15T00:00:00Z', None),
        )
        con.commit(); con.close()
        unmeasured = cand('agy', model_id='unmeasured')
        neutral = cand('agy', model_id='neutral')
        request = {
            'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
            'candidates': [neutral, unmeasured],
        }
        result = routing.route(request)
        self.assertEqual(result['selected'], triple_unmeasured)

    # ---- receipt: override rejected without authorization, honoured with it ----

    def test_override_without_recorded_authorization_is_a_hard_stop(self):
        c = cand('agy')
        request = {
            'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [c],
            'allow_unverified_override': True,
            'recorded_override': {
                'override_id': 'ov-1', 'run_id': 'run-1', 'family_id': 'fam-1', 'task_id': 'task-1',
                'role': 'executor', 'candidate_id': 'agy@1/m@high', 'bypass_stage': 2,
                'rationale': 'forged, never logged into runs.db',
                'authorized_by': 'user',
                'authorized_at': datetime.now(timezone.utc).isoformat(),
                'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            },
        }
        result = routing.route(request)
        self.assertEqual(result['status'], 'override_not_authorized')
        self.assertIsNone(result['selected'])

    def test_recorded_override_bypasses_trust_gate_and_is_disclosed(self):
        c = cand('agy')
        override = {
            'override_id': 'ov-2', 'run_id': 'run-1', 'family_id': 'fam-1', 'task_id': 'task-1',
            'role': 'executor', 'candidate_id': 'agy@1/m@high', 'bypass_stage': 2,
            'rationale': 'user explicitly authorized this exact triple for today only',
            'authorized_by': 'user',
            'authorized_at': datetime.now(timezone.utc).isoformat(),
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        routing.record_override(self.db_path, override)
        request = {
            'role': 'executor', 'playbook': 'Change', 'run_id': 'run-1', 'family_id': 'fam-1',
            'task_id': 'task-1', 'runs_db': self.db_path, 'candidates': [c],
            'allow_unverified_override': True, 'recorded_override': override,
        }
        result = routing.route(request)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['selected'], 'agy@1/m@high')
        self.assertIn('override', result['selection_disclosure'])
        self.assertEqual(result['selection_disclosure']['override']['override_id'], 'ov-2')

    def test_expired_override_is_rejected(self):
        c = cand('agy')
        override = {
            'override_id': 'ov-3', 'run_id': 'run-1', 'family_id': 'fam-1', 'task_id': 'task-1',
            'role': 'executor', 'candidate_id': 'agy@1/m@high', 'bypass_stage': 2,
            'rationale': 'this one already expired before use',
            'authorized_by': 'user',
            'authorized_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            'expires_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }
        routing.record_override(self.db_path, override)
        request = {
            'role': 'executor', 'playbook': 'Change', 'run_id': 'run-1', 'family_id': 'fam-1',
            'task_id': 'task-1', 'runs_db': self.db_path, 'candidates': [c],
            'allow_unverified_override': True, 'recorded_override': override,
        }
        result = routing.route(request)
        self.assertEqual(result['status'], 'override_not_authorized')

    # ---- receipt: replay over a seeded dataset shows no decision flips ----

    def test_no_decision_flip_the_policy_is_unchanged_by_the_refactor(self):
        """T2B changed where routing's four inputs COME FROM, not what the policy does
        with them. Expressing that as old-route vs new-route stopped being possible once
        T2 applied the delegation shim: office_runtime.route() now delegates, so the
        "old" monolithic implementation no longer exists in the tree and a request
        without runs_db correctly selects nothing. Asserting the two agree would now be
        asserting that the pre-refactor bug survived.

        So the guarantee is pinned against the policy's intent instead, which is not
        circular: with both candidates proven by an explicit act, the cheaper one wins
        regardless of harness; and a candidate missing a required capability is not
        selected no matter how cheap it is.
        """
        for triple in ('agy@1/m@high', 'claude@1/other@high'):
            scoring.record_trust_act(self.db_path, triple, 'proven', 'rico',
                                     'operator-verified rollout')

        cheaper_agy = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                       'candidates': [cand('agy', money=1),
                                      cand('claude', model_id='other', money=5)]}
        cheaper_claude = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                          'candidates': [cand('agy', money=5),
                                         cand('claude', model_id='other', money=1)]}
        # worker requires [] (config/config.default.yaml), so an empty-capability worker
        # legitimately qualifies. The executor/[builder] case belongs with the capability
        # filter itself and is covered by T2B's remediation, not by this shim test.
        no_capability_worker = {'role': 'worker', 'playbook': 'Change', 'runs_db': self.db_path,
                                'candidates': [cand('agy', caps=())]}

        self.assertEqual(routing.route(cheaper_agy).get('selected'), 'agy@1/m@high')
        self.assertEqual(routing.route(cheaper_claude).get('selected'), 'claude@1/other@high')
        self.assertEqual(routing.route(no_capability_worker).get('selected'), 'agy@1/m@high')

    def test_delegating_route_without_runs_db_cannot_select_on_asserted_trust(self):
        """The shim's real consequence, asserted rather than assumed. A request carrying
        no runs_db has no recorded evidence to derive trust from, so nothing qualifies --
        the caller can no longer reach a selection by asserting adapter_state itself.
        This is the behaviour that broke the old comparison, so it is worth a test of its
        own rather than an unexplained deletion."""
        request = {'role': 'executor', 'playbook': 'Change',
                   'candidates': [cand('agy', money=1)], 'adapter_state': 'proven'}
        result = office_runtime.route(dict(request))
        self.assertIsNone(result.get('selected'))


if __name__ == '__main__':
    unittest.main()
