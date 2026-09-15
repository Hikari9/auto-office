#!/usr/bin/env python3
import json, unittest
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

if __name__ == '__main__':
    unittest.main()
