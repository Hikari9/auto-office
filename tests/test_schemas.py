#!/usr/bin/env python3
import importlib.util, json, re, sqlite3, unittest
from pathlib import Path
from jsonschema import Draft202012Validator
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / 'tests/fixtures'

CONTRACT_SCHEMAS = [
    'execution-packet',
    'family-registry',
    'amendment',
    'landing',
    'checkpoint',
    'review-result',
    'completion-event',
    'start-receipt',
    'monitor-health',
    'replay-cursor',
]

class TestSchemas(unittest.TestCase):
    def test_schemas_valid(self):
        for p in (ROOT/'schemas').glob('*.json'):
            schema = json.loads(p.read_text())
            Draft202012Validator.check_schema(schema)
    
    def test_adapters_match_schema(self):
        schema_path = ROOT/'schemas/adapter.schema.json'
        if not schema_path.exists():
            return
        schema = json.loads(schema_path.read_text())
        validator = Draft202012Validator(schema)
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            errors = list(validator.iter_errors(data))
            self.assertEqual(errors, [], f'{p.name}: {errors}')

    def test_capability_floor_candidate_effort_fail_closed(self):
        # Deliverable F1: `effort` (not `min_effort`) is the real, universally-populated
        # catalog/candidate field the capability floor evaluates. A candidate missing it
        # must fail closed, not be treated as passing.
        schema = json.loads((ROOT / 'schemas/routing-candidate.schema.json').read_text())
        validator = Draft202012Validator(schema)

        accept = json.loads((FIXTURES_DIR / 'routing-candidate/accept_complete.json').read_text())
        self.assertEqual(list(validator.iter_errors(accept)), [])

        reject = json.loads((FIXTURES_DIR / 'routing-candidate/reject_missing_effort.json').read_text())
        errors = list(validator.iter_errors(reject))
        self.assertGreater(len(errors), 0, 'candidate missing effort must fail closed')
        self.assertTrue(
            any('effort' in e.message for e in errors),
            f'rejection must name the missing effort field, got: {[e.message for e in errors]}',
        )

    def test_catalog_effort_field_is_universally_populated(self):
        # Deliverable F1 empirical audit: `effort` (not `min_effort`) is 44/44 on
        # catalog/seed.yaml, and `min_effort` does not appear there at all (it is a
        # config-only threshold under roles.<role>.floor.min_effort).
        catalog = yaml.safe_load((ROOT / 'catalog/seed.yaml').read_text())['models']
        self.assertEqual(len(catalog), 44)
        self.assertEqual(sum(1 for m in catalog if m.get('effort')), 44)
        self.assertEqual(sum(1 for m in catalog if 'min_effort' in m), 0)

    def test_record_event_flag_form_produces_a_schema_valid_event(self):
        # Finding F19: the documented `record-event` flag form must be able to produce an
        # object that actually validates -- round 1 omitted --session-id/--family-id/
        # --dispatch-id, all schema-required, so a caller following only the documented
        # flags could never pass validation.
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(r"record-event \((.*?)\) \[--evidence-payload", text)
        self.assertTrue(m, 'record-event invocation line not found')
        invocation = m.group(1)
        for flag in ('--session-id', '--family-id', '--dispatch-id', '--event-id',
                     '--sequence', '--observed-status', '--terminal-classification',
                     '--source', '--evidence-timestamp', '--evidence-hash'):
            self.assertIn(flag, invocation, f'{flag} missing from documented record-event flags')

        # Simulate the CLI mapping these flags to a completion-event object and validate it.
        event = {
            'event_id': 'evt-001',
            'session_id': 'sess-001',
            'family_id': 'fam-office',
            'dispatch_id': 'disp-001',
            'sequence': 1,
            'observed_status': 'finish',
            'terminal_classification': 'success',
            'source': 'herdr',
            'evidence_timestamp': '2026-09-15T00:00:00Z',
            'evidence_hash': 'sha256:' + 'a' * 64,
        }
        schema = json.loads((ROOT / 'schemas/completion-event.schema.json').read_text())
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(event)), [])

        # And the pre-fix flag set (without the three identity flags) cannot validate.
        del event['session_id']
        del event['family_id']
        del event['dispatch_id']
        self.assertGreater(len(list(validator.iter_errors(event))), 0)

    def test_routing_candidate_triple_pinning_hard_exclusion_and_expiry(self):
        # Finding F28 / issue-39 "Routability rules": a numeric prior (effort, and by the
        # same rule intelligence_index/price/speed) is pinned to one exact routable
        # triple; "effort unparseable" is a hard exclusion ("a mis-keyed triple corrupts
        # every prior attached to it") enforced by the schema's strict effort enum. A
        # superseded triple is retained, not deleted (issue-39 "Expiry and
        # reproducibility": last_seen/superseded_by), so it must remain schema-valid --
        # expiry is a routing decision, not a schema violation.
        schema = json.loads((ROOT / 'schemas/routing-candidate.schema.json').read_text())
        validator = Draft202012Validator(schema)

        reject = json.loads((FIXTURES_DIR / 'routing-candidate/reject_unparseable_effort.json').read_text())
        errors = list(validator.iter_errors(reject))
        self.assertGreater(len(errors), 0, 'a candidate with an unparseable effort must be hard-excluded')

        accept = json.loads((FIXTURES_DIR / 'routing-candidate/accept_superseded_still_valid.json').read_text())
        self.assertEqual(list(validator.iter_errors(accept)), [])
        self.assertIn('superseded_by', accept)
        self.assertIn('last_seen', accept)

    def test_packet_doc_excerpt_matches_committed_schema(self):
        # Finding R7: the doc excerpt in Sec 2.1 previously claimed to differ from the
        # committed schema only by the omitted $schema line, but item constraints,
        # selection_disclosure's optional fields, and minLengths had drifted. Assert
        # byte-for-structure equality (modulo the omitted $schema key) so the two cannot
        # silently diverge again.
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(r'### 2\.1 Dispatch Packet.*?```json\n(.*?)\n```', text, re.S)
        self.assertTrue(m, 'execution packet doc excerpt not found')
        doc_schema = json.loads(m.group(1))
        real_schema = json.loads((ROOT / 'schemas/execution-packet.schema.json').read_text())
        doc_schema['$schema'] = real_schema['$schema']
        self.assertEqual(doc_schema, real_schema)

    def test_execution_packet_replaced_dispatch_id(self):
        # Finding R4: replaced_dispatch_id is now an explicit optional field so a
        # replacement packet (per Sec 3.1 step 4) can actually express citing the
        # dispatch it supersedes; empty string still fails closed like every other
        # identity field in this schema.
        schema = json.loads((ROOT / 'schemas/execution-packet.schema.json').read_text())
        validator = Draft202012Validator(schema)
        self.assertNotIn('replaced_dispatch_id', schema['required'])

        accept = json.loads((FIXTURES_DIR / 'execution-packet/accept_replacement_packet.json').read_text())
        self.assertEqual(list(validator.iter_errors(accept)), [])

        reject = json.loads((FIXTURES_DIR / 'execution-packet/reject_empty_replaced_dispatch_id.json').read_text())
        self.assertGreater(len(list(validator.iter_errors(reject))), 0)

    def test_create_execution_packet_names_session_id_and_packet_version(self):
        # Finding R4: session_id and packet_version were reachable only through **kwargs,
        # so a caller could omit two schema-required fields with no signature-level
        # signal. Both must now be explicit named parameters.
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(r'def create_execution_packet\((.*?)\n\) -> dict:', text, re.S)
        self.assertTrue(m, 'create_execution_packet signature not found')
        sig = m.group(1)
        self.assertIn('session_id: str', sig)
        self.assertIn('packet_version: int', sig)
        self.assertIn('replaced_dispatch_id', sig)

    def test_run_envelope_version_requiredness_matches_real_kickoff_shape(self):
        # Finding R4, partially rejected on evidence: requirements_version/routing_version
        # must stay optional on the run envelope because the real
        # office_runtime.py::_new_run_envelope writes envelope.json once, at kickoff,
        # before planning/routing exist -- only plan_version/packet_version (both
        # hardcoded 1 at that point) are ever populated. Prove both directions: the
        # two are still declared properties (so a later writer *can* record them), but
        # not required, and a kickoff-shaped envelope missing both still validates.
        schema = json.loads((ROOT / 'schemas/run-envelope.schema.json').read_text())
        self.assertIn('plan_version', schema['required'])
        self.assertIn('packet_version', schema['required'])
        self.assertNotIn('requirements_version', schema['required'])
        self.assertNotIn('routing_version', schema['required'])
        self.assertIn('requirements_version', schema['properties'])
        self.assertIn('routing_version', schema['properties'])

        validator = Draft202012Validator(schema)
        kickoff_envelope = {
            'run_id': 'run-001', 'family_id': 'fam-office', 'dispatch_id': 'disp-001',
            'role': 'orchestrator', 'holder_id': 'holder-1',
            'triple': 'agy@local/gemini-3.8-flash@medium', 'mode': 'direct',
            'playbook': 'Change', 'base_sha': '5d7a450', 'policy_hash': 'a' * 8,
            'catalog_snapshot_hash': 'b' * 8, 'adapter_snapshot_hash': 'c' * 8,
            'effective_config_hash': 'd' * 8, 'plan_version': 1, 'packet_version': 1,
            'created_at': '2026-09-15T00:00:00Z',
        }
        self.assertEqual(list(validator.iter_errors(kickoff_envelope)), [])

    def test_monitor_signatures_pin_event_identity_params(self):
        # Finding R5: record_completion_event lacked event_id/evidence_timestamp/
        # evidence_hash, and both cursor functions lacked family_id (the cursor is
        # keyed on session_id+family_id+dispatch_id per Sec 5.5's cursor_id formula).
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()

        m = re.search(r'def record_completion_event\((.*?)\n\) -> dict:', text, re.S)
        self.assertTrue(m, 'record_completion_event signature not found')
        sig = m.group(1)
        for param in ('event_id: str', 'family_id: str', 'evidence_timestamp: str', 'evidence_hash: str'):
            self.assertIn(param, sig, f'{param} missing from record_completion_event')

        m2 = re.search(r'def get_event_cursor\((.*?)\) -> int:', text, re.S)
        self.assertTrue(m2, 'get_event_cursor signature not found')
        self.assertIn('family_id: str', m2.group(1))

        m3 = re.search(r'def acknowledge_events\((.*?)\n\) -> dict:', text, re.S)
        self.assertTrue(m3, 'acknowledge_events signature not found')
        for param in ('family_id: str', 'event_id: str'):
            self.assertIn(param, m3.group(1), f'{param} missing from acknowledge_events')

    def test_event_id_deterministic_formula_produces_schema_valid_event(self):
        # Finding R5: event_id must be a pinned deterministic derivation so a repeated
        # (dispatch_id, sequence) always yields the same event_id -- that identity is
        # what makes "dedup" concrete rather than a hand-waved word.
        import hashlib
        dispatch_id, sequence = 'disp-001', 3
        event_id = 'evt-' + hashlib.sha256(f'{dispatch_id}:{sequence}'.encode()).hexdigest()[:16]
        event_id_again = 'evt-' + hashlib.sha256(f'{dispatch_id}:{sequence}'.encode()).hexdigest()[:16]
        self.assertEqual(event_id, event_id_again)

        event = {
            'event_id': event_id, 'session_id': 'sess-001', 'family_id': 'fam-office',
            'dispatch_id': dispatch_id, 'sequence': sequence, 'observed_status': 'finish',
            'terminal_classification': 'success', 'source': 'herdr',
            'evidence_timestamp': '2026-09-15T00:00:00Z', 'evidence_hash': 'sha256:' + 'a' * 64,
        }
        schema = json.loads((ROOT / 'schemas/completion-event.schema.json').read_text())
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(event)), [])

    def test_family_phase_enum_matches_runtime_phase_order(self):
        # Finding R6: the schema's phase enum must equal the real runtime's PHASE_ORDER
        # tuple, read from the actual parent-repo file (not a copy pasted into this
        # worktree), so the two cannot silently diverge again.
        import subprocess
        common_dir = subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', '--git-common-dir'], text=True
        ).strip()
        main_root = Path(common_dir).resolve().parent
        runtime_path = main_root / 'scripts' / 'office_runtime.py'
        self.assertTrue(runtime_path.exists(), f'parent runtime not found at {runtime_path}')
        source = runtime_path.read_text()
        m = re.search(r'PHASE_ORDER\s*=\s*\(([^)]*)\)', source)
        self.assertTrue(m, 'PHASE_ORDER tuple not found in real office_runtime.py')
        phase_order = tuple(x.strip().strip('"\'') for x in m.group(1).split(',') if x.strip())

        schema = json.loads((ROOT / 'schemas/family-registry.schema.json').read_text())
        phase_enum = schema['properties']['families']['additionalProperties']['properties']['phase']['enum']
        self.assertEqual(tuple(phase_enum), phase_order)

    def test_review_status_to_landing_disposition_mapping_is_pinned(self):
        # Finding R6: landing dispositions had no mapping to review finding statuses.
        # Every value of both enums must appear in the pinned conversion table.
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(
            r'### 3\.2 Review Finding Status vs\. Landing Disposition.*?(?=\n---\n|\n## )',
            text, re.S,
        )
        self.assertTrue(m, 'R6 mapping section not found')
        section = m.group(0)

        rr_schema = json.loads((ROOT / 'schemas/review-result.schema.json').read_text())
        landing_schema = json.loads((ROOT / 'schemas/landing.schema.json').read_text())
        status_enum = rr_schema['properties']['findings']['items']['properties']['status']['enum']
        disposition_enum = landing_schema['properties']['review']['properties']['dispositions']['items']['properties']['disposition']['enum']

        for status in status_enum:
            self.assertIn(status, section, f'review status {status} missing from R6 mapping table')
        for disposition in disposition_enum:
            self.assertIn(disposition, section, f'landing disposition {disposition} missing from R6 mapping table')

    def test_routing_candidate_permits_null_local_reward(self):
        # Finding F14: the contract requires local_reward=null to represent unmeasured
        # evidence (§7.4.2), but the schema declared type: number, rejecting it outright.
        schema = json.loads((ROOT / 'schemas/routing-candidate.schema.json').read_text())
        validator = Draft202012Validator(schema)
        data = json.loads((FIXTURES_DIR / 'routing-candidate/accept_null_local_reward.json').read_text())
        self.assertEqual(list(validator.iter_errors(data)), [])

    def test_reward_sort_key_orders_unmeasured_between_positive_and_neutral(self):
        # Finding F14: extract §7.4.3's reward_sort_key straight out of the contract doc
        # and prove null (unmeasured) sorts after every positive reward and ahead of
        # neutral/negative measured ones -- the whole point of distinguishing "unknown"
        # from a measured zero.
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(r"def reward_sort_key\(c\):.*?return \(3, -float\(r\)\).*?\n", text, re.S)
        self.assertTrue(m, 'reward_sort_key not found in docs/v3-runtime-contracts.md')
        namespace = {}
        exec(m.group(0), namespace)
        reward_sort_key = namespace['reward_sort_key']

        candidates = [
            {'id': 'neg', 'local_reward': -0.8},
            {'id': 'unmeasured', 'local_reward': None},
            {'id': 'pos_low', 'local_reward': 0.1},
            {'id': 'neutral', 'local_reward': 0.0},
            {'id': 'pos_high', 'local_reward': 0.9},
        ]
        ordered = [c['id'] for c in sorted(candidates, key=reward_sort_key)]
        self.assertEqual(ordered, ['pos_high', 'pos_low', 'unmeasured', 'neutral', 'neg'])

    def test_family_registry_permits_null_latest_landing_pre_landing(self):
        # Finding F8: a newly created family (issue-77 kickoff) has no landing yet.
        # latest_landing must accept null pre-landing while staying strict once populated.
        schema = json.loads((ROOT / 'schemas/family-registry.schema.json').read_text())
        validator = Draft202012Validator(schema)

        initial = json.loads((FIXTURES_DIR / 'family-registry/accept_initial_family_no_landing.json').read_text())
        self.assertEqual(list(validator.iter_errors(initial)), [])

        incomplete = json.loads(
            (FIXTURES_DIR / 'family-registry/reject_missing_evidence_latest_landing_evidence_hash.json').read_text()
        )
        self.assertGreater(len(list(validator.iter_errors(incomplete))), 0)

    def test_outcome_label_evidence_gate(self):
        # Finding F20: a technical label must carry a full, valid sha256: hash, never
        # merely a non-null one. Covers null, empty, malformed, and valid for every
        # always-mandatory label plus the two mandatory `abandoned` narrative subtypes.
        schema = json.loads((ROOT / 'schemas/outcome-label.schema.json').read_text())
        validator = Draft202012Validator(schema)
        valid_hash = 'sha256:' + 'a' * 64

        def is_valid(obj):
            return list(validator.iter_errors(obj)) == []

        for label in ('verified_no_observed_failure', 'recurrence_failure',
                      'revert_failure', 'material_post_merge_defect'):
            with self.subTest(label=label, case='missing'):
                self.assertFalse(is_valid({'label': label}))
            with self.subTest(label=label, case='null'):
                self.assertFalse(is_valid({'label': label, 'evidence_hash': None}))
            with self.subTest(label=label, case='empty'):
                self.assertFalse(is_valid({'label': label, 'evidence_hash': ''}))
            with self.subTest(label=label, case='malformed'):
                self.assertFalse(is_valid({'label': label, 'evidence_hash': 'sha256:deadbeef'}))
            with self.subTest(label=label, case='valid'):
                self.assertTrue(is_valid({'label': label, 'evidence_hash': valid_hash}))

        # abandoned: untagged, narrative:abandoned, narrative:operator_rejected -> optional
        for tag in (None, 'narrative:abandoned', 'narrative:operator_rejected'):
            obj = {'label': 'abandoned'}
            if tag:
                obj['contributing_attributions'] = [tag]
            with self.subTest(tag=tag):
                self.assertTrue(is_valid(obj))

        # abandoned: narrative:defect_detected / narrative:failed_verification -> mandatory
        for tag in ('narrative:defect_detected', 'narrative:failed_verification'):
            with self.subTest(tag=tag, case='missing'):
                self.assertFalse(is_valid({'label': 'abandoned', 'contributing_attributions': [tag]}))
            with self.subTest(tag=tag, case='valid'):
                self.assertTrue(is_valid({
                    'label': 'abandoned',
                    'contributing_attributions': [tag],
                    'evidence_hash': valid_hash,
                }))

        # environment_failure and pending never require evidence
        self.assertTrue(is_valid({'label': 'environment_failure'}))
        self.assertTrue(is_valid({'label': 'pending'}))

        for name in ('accept_complete', 'accept_abandoned_untagged_no_evidence'):
            with self.subTest(fixture=name):
                data = json.loads((FIXTURES_DIR / 'outcome-label' / f'{name}.json').read_text())
                self.assertEqual(list(validator.iter_errors(data)), [])
        for name in ('reject_missing_evidence_evidence_hash', 'reject_malformed_evidence_hash',
                     'reject_abandoned_defect_detected_missing_evidence'):
            with self.subTest(fixture=name):
                data = json.loads((FIXTURES_DIR / 'outcome-label' / f'{name}.json').read_text())
                self.assertGreater(len(list(validator.iter_errors(data))), 0)

    def test_outcome_label_narrative_subtype_reconstruction_is_total(self):
        # Finding F22: narrative-subtype reconstruction must be total and unambiguous --
        # absent, a single on-label recognised tag, a single off-label tag, an unknown
        # tag, and two tags (conflicting or duplicated) are all covered explicitly rather
        # than left to guesswork.
        schema = json.loads((ROOT / 'schemas/outcome-label.schema.json').read_text())
        validator = Draft202012Validator(schema)
        valid_hash = 'sha256:' + 'a' * 64

        def is_valid(obj):
            return list(validator.iter_errors(obj)) == []

        # Absent: fine.
        self.assertTrue(is_valid({'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash}))

        # Single, on-label, recognised tag: fine.
        self.assertTrue(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:success'],
        }))
        self.assertTrue(is_valid({
            'label': 'abandoned', 'contributing_attributions': ['narrative:abandoned'],
        }))

        # Off-label tag (recognised elsewhere, wrong label here): rejected.
        self.assertFalse(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:abandoned'],
        }))
        self.assertFalse(is_valid({
            'label': 'recurrence_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:success'],
        }))

        # Unknown tag: rejected, not silently ignored.
        self.assertFalse(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:bogus'],
        }))

        # Conflicting (or merely duplicated) tags: rejected -- at most one narrative tag.
        self.assertFalse(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:success', 'narrative:partial_success'],
        }))
        self.assertFalse(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['narrative:success', 'narrative:success'],
        }))

        # A non-narrative attribution string alongside a single narrative tag: still fine.
        self.assertTrue(is_valid({
            'label': 'verified_no_observed_failure', 'evidence_hash': valid_hash,
            'contributing_attributions': ['some-note', 'narrative:success'],
        }))

        for name in ('reject_two_narrative_tags', 'reject_unknown_narrative_tag',
                     'reject_off_label_narrative_tag'):
            with self.subTest(fixture=name):
                data = json.loads((FIXTURES_DIR / 'outcome-label' / f'{name}.json').read_text())
                self.assertGreater(len(list(validator.iter_errors(data))), 0)

    def test_pending_label_never_carries_evidence(self):
        # Finding F26: the contract says pending never carries evidence; the schema must
        # enforce that, not merely describe it.
        schema = json.loads((ROOT / 'schemas/outcome-label.schema.json').read_text())
        validator = Draft202012Validator(schema)
        valid_hash = 'sha256:' + 'a' * 64

        def is_valid(obj):
            return list(validator.iter_errors(obj)) == []

        self.assertTrue(is_valid({'label': 'pending'}))
        self.assertFalse(is_valid({'label': 'pending', 'evidence_hash': valid_hash}))
        self.assertFalse(is_valid({'label': 'pending', 'evidence_hash': None}))
        self.assertFalse(is_valid({'label': 'pending', 'contributing_attributions': ['narrative:success']}))

        data = json.loads((FIXTURES_DIR / 'outcome-label/reject_pending_with_evidence.json').read_text())
        self.assertGreater(len(list(validator.iter_errors(data))), 0)

    def test_reward_migration_no_policy_flip(self):
        # Finding F15: the F4 vocabulary consolidation must not change any reward number.
        # OLD_BASE_REWARD is docs/v3-runtime-contracts.md as pinned at commit 688246b
        # (`git show 688246b:docs/v3-runtime-contracts.md`), before this task touched it.
        # NEW_BASE_REWARD is the (stored_label, narrative_tag) scheme from the current
        # doc's §7.3.1/§7.4.1. For every narrative outcome that existed pre-migration,
        # both must produce the identical number given the same underlying evidence.
        OLD_BASE_REWARD = {
            'success': 0.8,
            'partial_success': 0.4,
            'environment_failure': 0.0,
            'abandoned': -0.4,
            'defect_detected': -0.5,
            'failed_verification': -0.6,
            'operator_rejected': -0.7,
            'recurrence_failure': -0.9,
            'material_post_merge_defect': -1.0,
        }

        NEW_BASE_REWARD = {
            ('verified_no_observed_failure', 'narrative:success'): 0.8,
            ('verified_no_observed_failure', None): 0.8,
            ('verified_no_observed_failure', 'narrative:partial_success'): 0.4,
            ('environment_failure', None): 0.0,
            ('abandoned', 'narrative:abandoned'): -0.4,
            ('abandoned', None): -0.4,
            ('abandoned', 'narrative:defect_detected'): -0.5,
            ('abandoned', 'narrative:failed_verification'): -0.6,
            ('abandoned', 'narrative:operator_rejected'): -0.7,
            ('recurrence_failure', None): -0.9,
            ('material_post_merge_defect', None): -1.0,
        }

        # narrative outcome -> (stored_label, narrative_tag) it migrates to
        MIGRATION = {
            'success': ('verified_no_observed_failure', 'narrative:success'),
            'partial_success': ('verified_no_observed_failure', 'narrative:partial_success'),
            'environment_failure': ('environment_failure', None),
            'abandoned': ('abandoned', 'narrative:abandoned'),
            'defect_detected': ('abandoned', 'narrative:defect_detected'),
            'failed_verification': ('abandoned', 'narrative:failed_verification'),
            'operator_rejected': ('abandoned', 'narrative:operator_rejected'),
            'recurrence_failure': ('recurrence_failure', None),
            'material_post_merge_defect': ('material_post_merge_defect', None),
        }

        self.assertEqual(set(MIGRATION), set(OLD_BASE_REWARD))
        for narrative, (stored_label, tag) in MIGRATION.items():
            with self.subTest(narrative=narrative):
                old_value = OLD_BASE_REWARD[narrative]
                new_value = NEW_BASE_REWARD[(stored_label, tag)]
                self.assertEqual(
                    old_value, new_value,
                    f'{narrative} reward changed across the F4 vocabulary migration: '
                    f'{old_value} (pre-migration) != {new_value} (post-migration, '
                    f'label={stored_label!r} tag={tag!r})',
                )

        # revert_failure has no pre-migration entry at all (confirmed absent from
        # OLD_BASE_REWARD above) and must stay explicitly unscored, not assigned a
        # number invented during this migration.
        self.assertNotIn('revert_failure', OLD_BASE_REWARD)
        self.assertNotIn(('revert_failure', None), NEW_BASE_REWARD)

    def test_v3_contract_fixtures_accept_complete(self):
        for schema_name in CONTRACT_SCHEMAS:
            with self.subTest(schema=schema_name, case='accept_complete'):
                schema_path = ROOT / 'schemas' / f'{schema_name}.schema.json'
                self.assertTrue(schema_path.exists(), f'Missing schema {schema_path}')
                schema = json.loads(schema_path.read_text())
                validator = Draft202012Validator(schema)

                fixture_path = FIXTURES_DIR / schema_name / 'accept_complete.json'
                self.assertTrue(fixture_path.exists(), f'Missing fixture {fixture_path}')
                data = json.loads(fixture_path.read_text())
                errors = list(validator.iter_errors(data))
                self.assertEqual(errors, [], f'accept_complete failed for {schema_name}: {errors}')

    def test_v3_contract_fixtures_reject_missing_fields(self):
        for schema_name in CONTRACT_SCHEMAS:
            schema_path = ROOT / 'schemas' / f'{schema_name}.schema.json'
            schema = json.loads(schema_path.read_text())
            validator = Draft202012Validator(schema)

            schema_fixtures = FIXTURES_DIR / schema_name
            reject_files = list(schema_fixtures.glob('reject_*.json'))
            self.assertGreater(len(reject_files), 0, f'No reject fixtures for {schema_name}')

            tested_categories = {'identity': 0, 'version': 0, 'evidence': 0}
            for rf in reject_files:
                for cat in tested_categories:
                    if f'reject_missing_{cat}_' in rf.name:
                        tested_categories[cat] += 1

                with self.subTest(schema=schema_name, reject_fixture=rf.name):
                    data = json.loads(rf.read_text())
                    errors = list(validator.iter_errors(data))
                    self.assertGreater(
                        len(errors), 0,
                        f'{schema_name}/{rf.name} was expected to fail validation, but passed'
                    )

            # Ensure every schema has at least one rejecting fixture per required category
            for cat, count in tested_categories.items():
                self.assertGreater(
                    count, 0,
                    f'Schema {schema_name} must have at least one rejecting fixture for category {cat}'
                )

class TestRouteNoNetworkAccess(unittest.TestCase):
    """Finding F25: issue-40's resolution requires routing to consume a reproducible
    local catalog snapshot and never perform live network discovery at route time.
    Prove it by banning socket construction for the duration of a real route() call
    that runs the full candidate pipeline through to a selection."""

    def test_route_selects_without_opening_a_socket(self):
        import socket
        original_socket = socket.socket

        def _forbidden(*args, **kwargs):
            raise AssertionError('route() must not open a socket')

        candidate = {
            'harness': 'agy', 'harness_version': 'local', 'model_id': 'gemini-3.8-flash',
            'effort': 'medium', 'adapter_state': 'proven', 'capabilities': [],
            'absolute_floor_pass': True,
            'quota': {'status': 'ok', 'tightest_remaining_percent': 50},
            'cost': {}, 'local_reward': 0,
        }
        socket.socket = _forbidden
        try:
            spec = importlib.util.spec_from_file_location('office_runtime', ROOT / 'scripts/office_runtime.py')
            rt = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(rt)
            result = rt.route({'role': 'worker', 'candidates': [candidate]})
        finally:
            socket.socket = original_socket

        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['selected'], 'agy@local/gemini-3.8-flash@medium')


class TestTrustQuery(unittest.TestCase):
    """Findings F3/F12: extract the pinned trust-evaluation SQL directly out of
    docs/v3-runtime-contracts.md and exercise it against a real sqlite schema, so the
    contract's own query text (not a hand-copied stand-in) is what gets proven closed."""

    TRIPLE = "agy@local/gemini-3.8-flash@medium"
    VALID_HASH = "sha256:" + "a" * 64

    @classmethod
    def setUpClass(cls):
        text = (ROOT / 'docs/v3-runtime-contracts.md').read_text()
        m = re.search(r"```sql\n(WITH latest_labels AS \(.*?)\n```", text, re.S)
        assert m, "trust evaluation SQL block not found in docs/v3-runtime-contracts.md"
        cls.QUERY = m.group(1)

    def _con(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
        CREATE TABLE dispatches(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, holder_id TEXT, triple TEXT, invocation_model_id TEXT, selection_reason TEXT, task_shape TEXT, size_class TEXT, started_at TEXT, ended_at TEXT, money_estimate REAL, money_actual REAL, quota_estimate REAL, quota_delta REAL, wall_clock_seconds REAL, attribution TEXT, outcome TEXT);
        CREATE TABLE findings(id TEXT PRIMARY KEY, dispatch_id TEXT, reviewer_dispatch_id TEXT, status TEXT, severity TEXT, summary TEXT, evidence_hash TEXT, created_at TEXT);
        CREATE TABLE validations(id TEXT PRIMARY KEY, dispatch_id TEXT, kind TEXT, command TEXT, passed INTEGER, known_bad_proven INTEGER, evidence_hash TEXT, created_at TEXT);
        CREATE TABLE outcome_labels(id TEXT PRIMARY KEY, dispatch_id TEXT, label TEXT, primary_attribution TEXT, contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT);
        CREATE TABLE lineage(id TEXT PRIMARY KEY, component_kind TEXT, component_id TEXT, parent_id TEXT, event TEXT, multiplier REAL, created_at TEXT);
        CREATE TABLE artifact_versions(id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, version INTEGER, content_hash TEXT, created_at TEXT);
        """)
        return con

    def _run(self, con, min_successful=5, min_shapes=2):
        return con.execute(self.QUERY, {
            "target_triple": self.TRIPLE,
            "proven_min_successful_dispatches": min_successful,
            "proven_min_task_shapes": min_shapes,
        }).fetchone()

    def _dispatch(self, con, did, task_shape="shapeA", attribution=None, run_id="run-1"):
        con.execute("INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, run_id, "executor", "h1", self.TRIPLE, "m1", "r", task_shape, "M",
                     "t", "t", 0, 0, 0, 0, 0, attribution, None))

    def _label(self, con, did, label, evidence_hash=None, primary_attribution="model",
               labeled_at="2026-09-15T00:00:00Z"):
        con.execute("INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
                    (f"lab-{did}-{label}", did, label, primary_attribution, None,
                     labeled_at, evidence_hash))

    def _resolve(self, con, lineage_id="lin-1", run_id="run-fix", val_id="val-fix",
                 passed=1, known_bad_proven=1, kind="known-bad-regression",
                 evidence_hash=None, created_at="2026-09-16T00:00:00Z",
                 bind_artifact=True):
        # Finding F27: a qualifying resolution needs a dispatch (for its run_id), a
        # validation row, and (unless bind_artifact=False, to test the unbound case) a
        # matching artifact_versions row in the same run whose content_hash equals the
        # validation's evidence_hash.
        evidence_hash = evidence_hash or self.VALID_HASH
        fixup_id = f"disp-fixup-{val_id}"
        self._dispatch(con, fixup_id, run_id=run_id)
        con.execute("INSERT INTO validations VALUES (?,?,?,?,?,?,?,?)",
                    (val_id, fixup_id, kind, "pytest ...", passed, known_bad_proven,
                     evidence_hash, created_at))
        if bind_artifact:
            con.execute("INSERT INTO artifact_versions VALUES (?,?,?,?,?,?)",
                        (f"art-{val_id}", run_id, "test-log", 1, evidence_hash, created_at))
        con.execute("INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
                    (lineage_id, "adapter", self.TRIPLE, val_id, "resolved_adapter_defect",
                     1.0, created_at))

    def _seed_five_evidenced_successes(self, con):
        shapes = ["shapeA", "shapeA", "shapeA", "shapeB", "shapeB"]
        for i, shape in enumerate(shapes):
            did = f"disp-ok-{i}"
            self._dispatch(con, did, task_shape=shape)
            self._label(con, did, "verified_no_observed_failure", evidence_hash=self.VALID_HASH)

    def test_empty_string_evidence_hash_does_not_qualify(self):
        # Finding F12: SQL "IS NOT NULL" is true for '', so an unsigned self-report with
        # evidence_hash='' must not count toward successful_dispatches.
        con = self._con()
        for i in range(6):
            did = f"disp-empty-{i}"
            self._dispatch(con, did)
            self._label(con, did, "verified_no_observed_failure", evidence_hash="")
        self.assertEqual(self._run(con)[3], "candidate")

    def test_non_hex_evidence_hash_does_not_qualify(self):
        # Finding F21: correct prefix and length are not enough -- a suffix containing a
        # non-hex character (here 'z', repeated to keep the length exactly 71) must not
        # be treated as valid evidence.
        con = self._con()
        for i in range(6):
            did = f"disp-nonhex-{i}"
            self._dispatch(con, did)
            self._label(con, did, "verified_no_observed_failure",
                        evidence_hash="sha256:" + "z" * 64)
        self.assertEqual(self._run(con)[3], "candidate")

    def test_uppercase_hex_evidence_hash_does_not_qualify(self):
        # Finding F21: the schema pattern ^sha256:[0-9a-f]{64}$ is lowercase-only; an
        # uppercase-hex suffix has the right length and character class casing mismatch
        # must still be rejected, not silently case-folded into acceptance.
        con = self._con()
        for i in range(6):
            did = f"disp-upper-{i}"
            self._dispatch(con, did)
            self._label(con, did, "verified_no_observed_failure",
                        evidence_hash="sha256:" + "A" * 64)
        self.assertEqual(self._run(con)[3], "candidate")

    def test_adapter_abandoned_with_critical_finding_quarantines(self):
        # Finding F12: an adapter-attributed dispatch that never landed but carries an
        # accepted-material critical finding must quarantine even without a
        # recurrence_failure/material_post_merge_defect label.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad-abandoned", attribution="adapter")
        self._label(con, "disp-bad-abandoned", "abandoned", evidence_hash=None)
        con.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
                    ("f1", "disp-bad-abandoned", "rev-1", "accepted-material", "critical",
                     "broke prod", self.VALID_HASH, "2026-09-15T00:00:00Z"))
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_resolution_record_with_empty_hash_does_not_clear_quarantine(self):
        # Finding F12: the resolved_adapter_defects lookup must reject an empty-string
        # validation evidence_hash the same way the label check does.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad2", attribution="adapter")
        self._label(con, "disp-bad2", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self._resolve(con, val_id="val-fake", evidence_hash="", bind_artifact=False)
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_resolution_with_known_bad_proven_false_does_not_clear_quarantine(self):
        # Finding F27 property 7 (relevance): passed=1 with a form-valid hash is not
        # enough -- the validation must have actually proven the known-bad case.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad3", attribution="adapter")
        self._label(con, "disp-bad3", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self._resolve(con, val_id="val-unproven", known_bad_proven=0)
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_resolution_with_wrong_kind_does_not_clear_quarantine(self):
        # Finding F27 property 7: only the approved kind for a resolution-proving
        # validation qualifies; an arbitrary passing check does not.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad4", attribution="adapter")
        self._label(con, "disp-bad4", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self._resolve(con, val_id="val-wrongkind", kind="pytest")
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_resolution_hash_not_bound_to_real_artifact_does_not_clear_quarantine(self):
        # Finding F27 property 7: the evidence_hash must be matched against a real,
        # independently recorded artifact_versions.content_hash, not trusted as a string.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad5", attribution="adapter")
        self._label(con, "disp-bad5", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self._resolve(con, val_id="val-unbound", bind_artifact=False)
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_backdated_resolution_does_not_clear_quarantine(self):
        # Finding F27 property 8 (recency): a validation dated before the failure it
        # claims to resolve cannot have tested the fix for it.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad6", attribution="adapter")
        self._label(con, "disp-bad6", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    labeled_at="2026-09-20T00:00:00Z")
        self._resolve(con, val_id="val-backdated", created_at="2026-09-10T00:00:00Z")
        self.assertEqual(self._run(con)[3], "quarantined")

    def test_fully_qualifying_resolution_clears_quarantine(self):
        # Finding F27: with relevance and recency both satisfied (in addition to the
        # pre-existing form/scope checks), resolution genuinely clears quarantine.
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad7", attribution="adapter")
        self._label(con, "disp-bad7", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    labeled_at="2026-09-10T00:00:00Z")
        self._resolve(con, val_id="val-good", created_at="2026-09-20T00:00:00Z")
        self.assertEqual(self._run(con)[3], "proven")

    def test_legit_evidenced_case_still_proves(self):
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self.assertEqual(self._run(con), (5, 2, 0, "proven"))

    def test_one_dispatch_short_stays_candidate(self):
        con = self._con()
        self._seed_five_evidenced_successes(con)
        con.execute("DELETE FROM dispatches WHERE id='disp-ok-4'")
        self.assertEqual(self._run(con)[3], "candidate")

    def test_unresolved_recurrence_quarantines_then_resolution_clears_it(self):
        con = self._con()
        self._seed_five_evidenced_successes(con)
        self._dispatch(con, "disp-bad", attribution="adapter")
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH,
                    labeled_at="2026-09-15T00:00:00Z")
        self.assertEqual(self._run(con)[3], "quarantined")
        self._resolve(con, val_id="val-fix", created_at="2026-09-16T00:00:00Z")
        self.assertEqual(self._run(con)[3], "proven")


if __name__ == '__main__':
    unittest.main()
