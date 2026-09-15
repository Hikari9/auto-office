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

    def test_no_decision_flip_against_the_pre_refactor_monolithic_route(self):
        """office_runtime.route() (still the pre-shim monolithic implementation in this
        worktree -- T2 has not yet applied the delegation shim here) accepted the four
        values directly from the caller. Seeding runs_db so the *derived* truth matches
        exactly what a caller used to assert, both implementations must choose the same
        candidate: this task changes where the values come from, not what the routing
        policy does with them.
        """
        for triple in ('agy@1/m@high', 'claude@1/other@high'):
            scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified rollout')

        scenarios = [
            {'role': 'executor', 'playbook': 'Change',
             'candidates': [cand('agy', money=1), cand('claude', model_id='other', money=5)]},
            {'role': 'executor', 'playbook': 'Change',
             'candidates': [cand('agy', money=5), cand('claude', model_id='other', money=1)]},
            {'role': 'worker', 'playbook': 'Change',
             'candidates': [cand('agy', caps=())]},
        ]
        for request in scenarios:
            old_request = dict(request)
            new_request = dict(request)
            new_request['runs_db'] = self.db_path
            old_result = office_runtime.route(old_request)
            new_result = routing.route(new_request)
            self.assertEqual(
                old_result.get('selected'), new_result.get('selected'),
                f"decision flip for {request['role']}/{[c['harness'] for c in request['candidates']]}",
            )
            self.assertEqual(old_result.get('status'), new_result.get('status'))


if __name__ == '__main__':
    unittest.main()
