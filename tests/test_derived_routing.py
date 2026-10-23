"""Integration tests for scripts/office_routing.py's derived route() -- Task T2B
(docs/plans/v3-final-merge.md amendment v3; contract docs/v3-runtime-contracts.md
section 7). Exercises the full 9-stage pipeline end to end against a seeded runs.db,
proving the receipts in the T2B dispatch brief: a caller-supplied adapter_state,
absolute_floor_pass or local_reward has no effect; only a recorded trust act ever
raises trust; a missing capability-floor field fails closed and names itself; unknown
reward never ranks as a measured zero; and an override is rejected without a recorded
authorization and honoured (and disclosed) with one.
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


import office_routing as routing
import office_runtime
import office_scoring as scoring
from office import routing as current_routing
from office import candidates as candidate_builder


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

    def test_concurrency_cap_refuses_full_model_across_runs_and_falls_back(self):
        target = cand('opencode', 'glm-5.3-flash-free', effort='high', money=0)
        target.update(invocation_model_id='opencode/glm-5.3-flash-free', max_concurrent=2)
        fallback = cand('agy', 'fallback', money=0)
        other_model = cand('opencode', 'another-model', money=0)
        other_model.update(invocation_model_id='opencode/another-model', max_concurrent=1)
        target_id = 'opencode@1/glm-5.3-flash-free@high'
        fallback_id = 'agy@1/fallback@high'
        other_id = 'opencode@1/another-model@high'
        scoring.record_trust_act(self.db_path, target_id, 'proven', 'rico', 'operator-verified route')
        scoring.record_trust_act(self.db_path, fallback_id, 'proven', 'rico', 'operator-verified route')
        scoring.record_trust_act(self.db_path, other_id, 'proven', 'rico', 'operator-verified route')
        from office import db as office_db
        office_db.connect(Path(self.db_path)).close()
        import sqlite3
        con = sqlite3.connect(self.db_path)
        for i, effort in enumerate(('low', 'medium')):
            con.execute("INSERT INTO runs(id, phase, status) VALUES (?, 'executing', 'running')", (f'run-{i}',))
            con.execute(
                "INSERT INTO dispatches(id, run_id, role, holder_id, triple, status, harness, model, effort) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f'live-{i}', f'run-{i}', 'executor', f'h{i}', f'opencode@1/glm-5.3-flash-free@{effort}',
                 'running' if i == 0 else 'launching', 'opencode', 'opencode/glm-5.3-flash-free', effort),
            )
        con.commit(); con.close()
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                   'candidates': [target, fallback, other_model]}

        result = current_routing.route(request)

        self.assertEqual(result['selected'], fallback_id)
        refusal = next(r for r in result['rejected'] if r['candidate'] == target_id)
        self.assertEqual(refusal['stage'], 6)
        self.assertIn('concurrency cap reached (2/2)', refusal['reason'])
        self.assertEqual(request['concurrency_snapshot'][target_id], 2)
        self.assertEqual(request['concurrency_snapshot'][other_id], 0)
        replay = current_routing.route(request)
        self.assertEqual(replay['selected'], result['selected'])
        self.assertEqual(replay['rejected'], result['rejected'])

    def test_one_live_dispatch_below_cap_keeps_route_and_cost_score(self):
        target = cand('opencode', 'glm-5.3-flash-free', effort='high', money=0)
        target.update(invocation_model_id='opencode/glm-5.3-flash-free', max_concurrent=2,
                      price_fields={'input_per_mtok': 0.0, 'output_per_mtok': 0.0})
        triple = 'opencode@1/glm-5.3-flash-free@high'
        scoring.record_trust_act(self.db_path, triple, 'proven', 'rico', 'operator-verified route')
        from office import db as office_db
        office_db.connect(Path(self.db_path)).close()
        import sqlite3
        con = sqlite3.connect(self.db_path)
        rows = [
            ('live', 'run-live', 'executing', 'running', 'running', None),
            ('closed-phase', 'run-closed-phase', 'closed', 'running', 'running', None),
            ('abandoned-status', 'run-abandoned-status', 'executing', 'abandoned', 'running', None),
            ('landed-phase', 'run-landed-phase', 'landed', 'running', 'running', None),
            ('closed-status', 'run-closed-status', 'executing', 'closed', 'running', None),
            ('ended', 'run-ended', 'executing', 'running', 'running', '2026-10-22T00:00:00Z'),
            ('failed', 'run-failed', 'executing', 'running', 'failed', '2026-10-22T00:00:00Z'),
            ('superseded', 'run-superseded', 'executing', 'running', 'superseded', None),
        ]
        for did, rid, phase, run_status, status, ended_at in rows:
            con.execute("INSERT INTO runs(id, phase, status) VALUES (?,?,?)", (rid, phase, run_status))
            con.execute(
                "INSERT INTO dispatches(id, run_id, role, holder_id, triple, status, harness, model, effort, ended_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, rid, 'executor', did, triple, status, 'opencode', 'opencode/glm-5.3-flash-free', 'low', ended_at),
            )
        con.commit(); con.close()
        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path, 'candidates': [target]}

        result = current_routing.route(request)

        self.assertEqual(result['selected'], triple)
        self.assertEqual(target['concurrency'], {'count': 1, 'cap': 2})
        self.assertEqual(result['routing']['candidates'][0]['components']['cost'], 1.0)

    def test_opencode_quota_wall_cooldown_is_route_local_and_success_clears_it(self):
        import json
        from office import db as office_db, dispatch
        from datetime import datetime, timezone

        target = cand('opencode', 'muse-free')
        alternate = cand('opencode', 'deepseek-free')
        target_id = current_routing.candidate_id(target)
        alternate_id = current_routing.candidate_id(alternate)
        scoring.record_trust_act(self.db_path, target_id, 'proven', 'rico', 'verified route')
        scoring.record_trust_act(self.db_path, alternate_id, 'proven', 'rico', 'verified route')
        office_db.connect(Path(self.db_path)).close()

        now = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
        log = Path(self.db_path).with_suffix('.log')
        log.write_text('Free usage limit reached\n', encoding='utf-8')
        con = office_db.connect(Path(self.db_path))
        con.execute("INSERT INTO runs(id, phase, status, policy_json) VALUES (?,?,?,?)",
                    ('run-cooldown', 'executing', 'running', json.dumps({'quota': {'cooldown_minutes': 15}})))
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, status, triple, log_path) "
                    "VALUES ('dispatch-cooldown', 'run-cooldown', 'observer', 'observer', 'running', ?, ?)",
                    (target_id, str(log)))
        real_connect = office_db.connect
        con.close()
        with unittest.mock.patch.object(dispatch.db, 'connect', lambda: real_connect(Path(self.db_path))), \
                unittest.mock.patch.object(dispatch, 'datetime') as clock:
            clock.now.return_value = now
            dispatch._finish('dispatch-cooldown', 1, None, 'nonzero', 1.0)

        con = office_db.connect(Path(self.db_path))
        cooldown_columns = {row[1] for row in con.execute("PRAGMA table_info(route_cooldowns)")}
        self.assertNotIn('reason', cooldown_columns)
        expires = con.execute("SELECT expires_at FROM route_cooldowns WHERE route=?", (target_id,)).fetchone()[0]
        self.assertEqual(expires, '2026-10-23T09:15:00+00:00')
        con.close()

        request = {'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                   'candidates': [target, alternate]}
        result = current_routing.route(request)
        self.assertEqual(result['selected'], alternate_id)
        rejected = next(row for row in result['rejected'] if row['candidate'] == target_id)
        self.assertEqual(rejected['category'], 'rate-limited')
        self.assertEqual(rejected['reason'], 'rate-limited')

        con = office_db.connect(Path(self.db_path))
        con.execute("UPDATE route_cooldowns SET expires_at='2000-01-01T00:00:00+00:00' WHERE route=?", (target_id,))
        con.close()
        result = current_routing.route({'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                                        'candidates': [target]})
        self.assertEqual(result['selected'], target_id)
        self.assertEqual(target['quota']['status'], 'ok')

        con = office_db.connect(Path(self.db_path))
        con.execute("UPDATE route_cooldowns SET expires_at='9999-01-01T00:00:00+00:00' WHERE route=?", (target_id,))
        con.execute("INSERT INTO runs(id, phase, status) VALUES ('run-success', 'executing', 'running')")
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, status, triple) "
                    "VALUES ('dispatch-success', 'run-success', 'observer', 'observer', 'running', ?)",
                    (target_id,))
        con.close()
        with unittest.mock.patch.object(dispatch.db, 'connect', lambda: real_connect(Path(self.db_path))):
            dispatch._finish('dispatch-success', 0, None, 'success', 1.0)
        result = current_routing.route({'role': 'executor', 'playbook': 'Change', 'runs_db': self.db_path,
                                        'candidates': [target]})
        self.assertEqual(result['selected'], target_id)

    def test_usage_limit_reset_time_overrides_configured_cooldown(self):
        from office import db as office_db, dispatch
        from datetime import datetime, timezone

        route = 'opencode@1/muse-free@high'
        now = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
        log = Path(self.db_path).with_suffix('.reset.log')
        log.write_text('Free usage limit reached · resets 9:30pm (Asia/Manila)\n', encoding='utf-8')
        run = {'id': 'run-reset', 'policy': {'quota': {'cooldown_minutes': 15}}}
        dispatch_row = {'id': 'dispatch-reset', 'triple': route, 'log_path': str(log)}
        con = office_db.connect(Path(self.db_path))
        dispatch._record_route_cooldown(con, run, dispatch_row, now=now)
        expires = con.execute("SELECT expires_at FROM route_cooldowns WHERE route=?", (route,)).fetchone()[0]
        con.close()
        self.assertEqual(expires, '2026-10-23T13:30:00+00:00')

    def test_opencode_failure_signatures_cover_usage_and_rate_limit_text(self):
        import yaml
        from office import dispatch
        adapter = yaml.safe_load((ROOT / 'adapters/seed/opencode.yaml').read_text())
        default_config = yaml.safe_load((ROOT / 'config/config.default.yaml').read_text())
        self.assertEqual(default_config['quota']['cooldown_minutes'], 15)
        patterns = {signature['pattern'] for signature in adapter['failure_signatures']}
        self.assertIn('Free usage limit reached', patterns)
        self.assertIn('Rate limit exceeded', patterns)
        log = Path(self.db_path).with_suffix('.signature.log')
        d = {'id': 'dispatch-signature', 'terminal_classification': 'nonzero', 'log_path': str(log)}
        for text in ('Free usage limit reached', 'Rate limit exceeded'):
            log.write_text(text + '\n', encoding='utf-8')
            self.assertEqual(dispatch._quota_wall({'id': 'run-signature'}, d), text)

    def test_catalog_cap_overrides_free_adapter_default_and_other_routes_are_uncapped(self):
        adapter = {'free_model_max_concurrent': 2}
        self.assertEqual(candidate_builder._max_concurrent({'max_concurrent': 3}, adapter, True), 3)
        self.assertEqual(candidate_builder._max_concurrent({}, adapter, True), 2)
        self.assertIsNone(candidate_builder._max_concurrent({}, adapter, False))
        self.assertIsNone(candidate_builder._max_concurrent({}, {}, False))

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

    def _user_config(self, text):
        cfg = Path(tempfile.mkdtemp()) / 'user-config.yaml'
        if text is not None:
            cfg.write_text(text)
        return unittest.mock.patch.dict(os.environ, {'OFFICE_USER_CONFIG': str(cfg)})

    def test_disabled_free_model_is_rejected_without_substituting_it_for_selected_route(self):
        regular = cand('agy', model_id='regular', money=5.0)
        free = cand('agy', model_id='free', money=0.0)
        free['free_model'] = True
        request = {'role': 'worker', 'playbook': 'Change', 'runs_db': self.db_path,
                   'cost_policy': 'money_saver', 'candidates': [free, regular]}
        with self._user_config(None):
            result = current_routing.route(request)
        self.assertEqual(result['selected'], 'agy@1/regular@high')
        rejected = [r for r in result['rejected'] if r.get('category') == 'free-model-opt-in']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['candidate'], 'agy@1/free@high')

    def test_router_reads_the_free_model_opt_in_when_the_caller_omits_it(self):
        regular = cand('agy', model_id='regular', money=5.0)
        free = cand('agy', model_id='free', money=0.0)
        free['free_model'] = True
        request = {'role': 'worker', 'playbook': 'Change', 'runs_db': self.db_path, 'adaptive': False,
                   'cost_policy': 'money_saver', 'candidates': [free, regular]}
        with self._user_config('onboarding:\n  free_models_enabled: true\n'):
            self.assertEqual(current_routing.route(request)['selected'], 'agy@1/free@high')
            # An explicit caller value still wins over the stored consent.
            result = current_routing.route({**request, 'allow_free_models': False})
        self.assertEqual(result['selected'], 'agy@1/regular@high')

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

    def _override(self, **overrides):
        """An override that is valid in EVERY respect except the one a test changes.

        Built as a helper because the earlier version of the test below hand-rolled a request
        missing run_id/family_id/task_id: it rejected at the context-match check and never
        reached the rule under test, so the rule could have been deleted with the test still
        green. Each rejecting test now differs from the honoured case by exactly one field.
        """
        record = {
            'override_id': 'ov-x', 'run_id': 'run-1', 'family_id': 'fam-1', 'task_id': 'task-1',
            'role': 'executor', 'candidate_id': 'agy@1/m@high', 'bypass_stage': 2,
            'rationale': 'user explicitly authorized this exact triple for today only',
            'authorized_by': 'user',
            'authorized_at': datetime.now(timezone.utc).isoformat(),
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        record.update(overrides)
        return record

    def _request_with(self, override, record_it=True):
        if record_it:
            routing.record_override(self.db_path, override)
        return {
            'role': 'executor', 'playbook': 'Change', 'run_id': 'run-1', 'family_id': 'fam-1',
            'task_id': 'task-1', 'runs_db': self.db_path, 'candidates': [cand('agy')],
            'allow_unverified_override': True, 'recorded_override': override,
        }

    def test_override_without_recorded_authorization_is_a_hard_stop(self):
        """Rule 5: the row must exist in runs.db.

        Every other field matches the request, so the ONLY thing separating this from the
        honoured case is that the record was never written. Previously this test omitted
        run_id/family_id/task_id and so rejected at the context check instead -- it passed
        whether or not rule 5 existed.
        """
        override = self._override(override_id='ov-unlogged',
                                  rationale='never logged into runs.db, otherwise identical')
        request = self._request_with(override, record_it=False)
        result = routing.route(request)
        self.assertEqual(result['status'], 'override_not_authorized')
        self.assertIsNone(result['selected'])

        # The control: record that same row and the identical request is honoured. Without this
        # the test above could pass for any reason at all.
        routing.record_override(self.db_path, override)
        honoured = routing.route(request)
        self.assertEqual(honoured['status'], 'selected')

    def test_override_not_authorized_by_the_user_is_rejected(self):
        """Rule 1: `authorized_by` must be `user`. An agent cannot authorize its own bypass."""
        override = self._override(override_id='ov-selfauth', authorized_by='orchestrator')
        result = routing.route(self._request_with(override))
        self.assertEqual(result['status'], 'override_not_authorized')

    def test_override_of_the_quota_stage_is_rejected(self):
        """bypass_stage is pinned to [2, 4, 7]. Stage 6 -- quota safety -- is NOT overridable.

        This is the rule that stops a run from authorizing its way into the protected reserve,
        so it is the one most worth a rejecting test.
        """
        override = self._override(override_id='ov-quota', bypass_stage=6)
        result = routing.route(self._request_with(override))
        self.assertEqual(result['status'], 'override_not_authorized')

    def test_override_with_a_token_rationale_is_rejected(self):
        """Rule 4: a rationale under 10 non-whitespace characters is not a reason."""
        override = self._override(override_id='ov-terse', rationale='ok   ')
        result = routing.route(self._request_with(override))
        self.assertEqual(result['status'], 'override_not_authorized')

    def test_override_for_a_candidate_not_in_the_request_is_rejected(self):
        """An override authorizes one triple, not the request it happens to arrive with."""
        override = self._override(override_id='ov-othercand', candidate_id='agy@1/other@high')
        result = routing.route(self._request_with(override))
        self.assertEqual(result['status'], 'override_not_authorized')

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


import pytest as _pytest  # noqa: E402

pytestmark = _pytest.mark.legacy  # tests the 3.0 scripts/ surface


if __name__ == '__main__':
    unittest.main()
