#!/usr/bin/env python3
"""Adapter conformance tests."""
import json, yaml, unittest
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]

class TestAdapterConformance(unittest.TestCase):
    def _load_schema(self):
        schema_path = ROOT/'schemas/adapter.schema.json'
        if not schema_path.exists():
            return None
        return json.loads((ROOT/'schemas/adapter.schema.json').read_text())
    
    def test_all_adapters_valid_schema(self):
        schema = self._load_schema()
        if not schema: return
        validator = Draft202012Validator(schema)
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            errors = list(validator.iter_errors(data))
            self.assertEqual(errors, [], f'{p.name}: {errors}')
    
    def test_adapters_have_required_semantics(self):
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            self.assertIn('id', data)
            self.assertIn('verified_state', data)
            self.assertIn('invocation', data)
            self.assertIn('safe_prompt_passing', data)
            inv = data.get('invocation', {})
            self.assertTrue('executable' in inv or 'binary' in inv, f'{p.name}: invocation missing executable')
            self.assertFalse(data['safe_prompt_passing'].get('shell', True),
                           f'{p.name}: shell must be false')
    
    def test_hermes_adapter_exists(self):
        hermes = ROOT / 'adapters/seed/hermes.yaml'
        self.assertTrue(hermes.exists(), 'Hermes adapter missing')
        data = yaml.safe_load(hermes.read_text())
        self.assertEqual(data['id'], 'hermes')
    
    def test_adapter_ids_unique(self):
        ids = []
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            ids.append(data['id'])
        self.assertEqual(len(ids), len(set(ids)), 'Duplicate adapter IDs')
    
    def test_effort_mappings_canonical(self):
        canon = {'none', 'low', 'medium', 'high', 'xhigh', 'max'}
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            for k in data.get('effort_mapping', {}):
                self.assertIn(k, canon, f'{p.name}: non-canonical effort {k}')
    
    def test_canonical_harness_set(self):
        adapters = {p.stem for p in (ROOT/'adapters/seed').glob('*.yaml')}
        required = {'codex', 'claude', 'agy', 'hermes'}
        self.assertTrue(required.issubset(adapters),
                       f'Missing adapters: {required - adapters}')

    def test_quota_probe_scripts_exist(self):
        for p in (ROOT/'adapters/seed').glob('*.yaml'):
            data = yaml.safe_load(p.read_text())
            probe = data.get('quota_probe', {})
            cmd = probe.get('command', [])
            if cmd and len(cmd) >= 2 and cmd[0] == 'python3':
                script_path = ROOT / cmd[1]
                self.assertTrue(script_path.exists(), f"{p.name}: probe script {cmd[1]} not found")

    def test_agy_usage_process_quota(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('agy_usage', ROOT / 'scripts/agy-usage.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Gemini models filtered, Claude excluded, tightest computed
        res = mod.process_quota({
            'buckets': [
                {'modelId': 'gemini-3.8-flash-high', 'remainingFraction': 0.85, 'resetTime': '2026-09-15T00:00:00Z'},
                {'modelId': 'gemini-3.8-flash-low', 'remainingFraction': 0.60, 'resetTime': '2026-09-15T00:00:00Z'},
                {'modelId': 'claude-opus-4-6-thinking', 'remainingFraction': 0.10},
                {'modelId': 'chat_20706', 'remainingFraction': 0.20}
            ]
        })
        self.assertEqual(res['tightest_remaining_percent'], 60)
        self.assertIn('gemini-3.8-flash-high', res['models'])
        self.assertIn('gemini-3.8-flash-low', res['models'])
        self.assertNotIn('claude-opus-4-6-thinking', res['models'])
        self.assertNotIn('chat_20706', res['models'])

        # --all flag includes non-Claude models
        res_all = mod.process_quota({
            'buckets': [
                {'modelId': 'gemini-3.8-flash-high', 'remainingFraction': 0.85},
                {'modelId': 'claude-opus-4-6-thinking', 'remainingFraction': 0.10},
                {'modelId': 'gpt-oss-120b-medium', 'remainingFraction': 0.40}
            ]
        }, all_models=True)
        self.assertIn('gpt-oss-120b-medium', res_all['models'])
        self.assertNotIn('claude-opus-4-6-thinking', res_all['models'])
        self.assertEqual(res_all['tightest_remaining_percent'], 40)

    def test_agy_usage_missing_token_exit_code(self):
        import subprocess
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp/nonexistent-home-for-agy-quota-test'}
        res = subprocess.run(
            ['python3', str(ROOT / 'scripts/agy-usage.py')],
            env=env,
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn('Token file not found', res.stderr)

if __name__ == '__main__':
    unittest.main()
