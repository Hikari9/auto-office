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
        """)
        return con

    def _run(self, con, min_successful=5, min_shapes=2):
        return con.execute(self.QUERY, {
            "target_triple": self.TRIPLE,
            "proven_min_successful_dispatches": min_successful,
            "proven_min_task_shapes": min_shapes,
        }).fetchone()

    def _dispatch(self, con, did, task_shape="shapeA", attribution=None):
        con.execute("INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, "run-1", "executor", "h1", self.TRIPLE, "m1", "r", task_shape, "M",
                     "t", "t", 0, 0, 0, 0, 0, attribution, None))

    def _label(self, con, did, label, evidence_hash=None, primary_attribution="model"):
        con.execute("INSERT INTO outcome_labels VALUES (?,?,?,?,?,?,?)",
                    (f"lab-{did}-{label}", did, label, primary_attribution, None,
                     "2026-09-15T00:00:00Z", evidence_hash))

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
        con.execute("INSERT INTO validations VALUES (?,?,?,?,?,?,?,?)",
                    ("val-fake", "disp-fixup", "regression-test", "pytest ...", 1, 1, "",
                     "2026-09-16T00:00:00Z"))
        con.execute("INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
                    ("lin-2", "adapter", self.TRIPLE, "val-fake", "resolved_adapter_defect",
                     1.0, "2026-09-16T00:00:00Z"))
        self.assertEqual(self._run(con)[3], "quarantined")

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
        self._label(con, "disp-bad", "recurrence_failure", evidence_hash=self.VALID_HASH)
        self.assertEqual(self._run(con)[3], "quarantined")
        con.execute("INSERT INTO validations VALUES (?,?,?,?,?,?,?,?)",
                    ("val-fix", "disp-fixup", "regression-test", "pytest ...", 1, 1,
                     self.VALID_HASH, "2026-09-16T00:00:00Z"))
        con.execute("INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
                    ("lin-1", "adapter", self.TRIPLE, "val-fix", "resolved_adapter_defect",
                     1.0, "2026-09-16T00:00:00Z"))
        self.assertEqual(self._run(con)[3], "proven")


if __name__ == '__main__':
    unittest.main()
