#!/usr/bin/env python3
import json, os, shlex, subprocess, sys, tempfile, unittest, shutil
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]

class TestHooks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='office-hooks-test-')
        self.repo = Path(self.tmpdir)
        subprocess.run(['git', 'init'], cwd=self.repo, capture_output=True)
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'], cwd=self.repo, capture_output=True)
        self.state_dir = self.repo / '.office'
        self.state_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_run(self, phase, state_text=None, approval_quote=None):
        run_id = 'run-123'
        canonical_state = self.repo / 'canonical-state' / run_id
        canonical_state.mkdir(parents=True)
        if state_text is None:
            state = {'run_id': run_id, 'phase': phase}
            if approval_quote is not None:
                state['approval'] = {'quote': approval_quote}
            state_text = json.dumps(state)
        (canonical_state / 'state.json').write_text(state_text)
        pointer_dir = self.state_dir / 'runs'
        pointer_dir.mkdir()
        (pointer_dir / f'{run_id}.ref').write_text(str(canonical_state) + '\n')

    def _run_state_dir(self):
        return self.repo / 'canonical-state' / 'run-123'

    def _trusted_runtime(self):
        return ROOT / 'scripts' / 'office_runtime.py'

    def _approval_command(self, suffix=''):
        return (
            f'python3 {shlex.quote(str(self._trusted_runtime()))} approve-plan '
            f'--state-dir {shlex.quote(str(self._run_state_dir()))} '
            '--approved-by user --quote "I approve this plan."' + suffix
        )

    def _assert_stop_hook_uses_executable_node(self, commands):
        stop_command = next(
            (command for command in commands if 'close_finished_panes.mjs' in command),
            None,
        )
        self.assertIsNotNone(stop_command, 'Stop hook must invoke close_finished_panes.mjs')
        command_parts = shlex.split(stop_command)
        self.assertGreaterEqual(len(command_parts), 2)
        node_path = Path(command_parts[0])
        self.assertTrue(node_path.is_file(), f'Node binary does not exist: {node_path}')
        self.assertTrue(os.access(node_path, os.X_OK), f'Node binary is not executable: {node_path}')

    def _run_pre_tool_use(self, payload, cwd=None, with_state_dir=True):
        hook = ROOT / 'scripts' / 'hooks' / 'pre_tool_use.py'
        env = {**os.environ}
        if with_state_dir:
            env['OFFICE_STATE_DIR'] = str(self.state_dir)
        else:
            env.pop('OFFICE_STATE_DIR', None)
        return subprocess.run(
            [sys.executable, str(hook)],
            cwd=cwd or self.repo,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )

    def test_pre_tool_use_no_run_state_allows_mutation(self):
        r = self._run_pre_tool_use({'tool_name': 'Edit', 'tool_input': {'file_path': 'x'}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_invalid_json_fails_closed(self):
        hook = ROOT / 'scripts' / 'hooks' / 'pre_tool_use.py'
        r = subprocess.run(
            [sys.executable, str(hook)],
            cwd=self.repo,
            env={**os.environ, 'OFFICE_STATE_DIR': str(self.state_dir)},
            input='{not-json',
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn('invalid_payload', r.stdout)

    def test_pre_tool_use_intake_blocks_mutation(self):
        self._write_run('intake')
        r = self._run_pre_tool_use({'tool_name': 'Write', 'tool_input': {'file_path': 'x'}})
        self.assertEqual(r.returncode, 2)
        self.assertIn('approve-plan', r.stdout)
        self.assertIn("phase 'intake'", r.stdout)

    def test_pre_tool_use_planned_blocks_mutation(self):
        self._write_run('planned')
        r = self._run_pre_tool_use({'tool_name': 'NotebookEdit', 'tool_input': {'notebook_path': 'x'}})
        self.assertEqual(r.returncode, 2)
        self.assertIn('approve-plan', r.stdout)
        self.assertIn("phase 'planned'", r.stdout)

    def test_pre_tool_use_approved_allows_mutation(self):
        self._write_run('approved', approval_quote='I approve this plan.')
        r = self._run_pre_tool_use({'tool_name': 'Edit', 'tool_input': {'file_path': 'x'}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_approved_without_quote_blocks_mutation(self):
        self._write_run('approved')
        r = self._run_pre_tool_use({'tool_name': 'Edit', 'tool_input': {'file_path': 'x'}})
        self.assertEqual(r.returncode, 2)
        self.assertIn('approval_required', r.stdout)
        self.assertIn('approval.quote', r.stdout)

    def test_pre_tool_use_default_state_is_found_from_repo_root(self):
        self._write_run('planned')
        nested = self.repo / 'nested' / 'directory'
        nested.mkdir(parents=True)
        r = self._run_pre_tool_use(
            {'tool_name': 'Write', 'tool_input': {'file_path': 'x'}},
            cwd=nested,
            with_state_dir=False,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("phase 'planned'", r.stdout)

    def test_pre_tool_use_scratch_write_outside_repository_allows(self):
        r = self._run_pre_tool_use(
            {
                'cwd': '/tmp',
                'tool_name': 'Write',
                'tool_input': {'file_path': '/tmp/scratch.md'},
            },
            cwd=ROOT,
            with_state_dir=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_scratch_bash_redirect_outside_repository_allows(self):
        r = self._run_pre_tool_use(
            {
                'cwd': '/tmp',
                'tool_name': 'Bash',
                'tool_input': {'command': 'echo hi > /tmp/x'},
            },
            cwd=ROOT,
            with_state_dir=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_plain_git_repository_without_run_allows(self):
        plain_repo = Path(tempfile.mkdtemp(prefix='office-hooks-plain-repo-'))
        (plain_repo / '.git').mkdir()
        try:
            r = self._run_pre_tool_use(
                {'tool_name': 'Write', 'tool_input': {'file_path': 'target.txt'}},
                cwd=plain_repo,
                with_state_dir=False,
            )
        finally:
            shutil.rmtree(plain_repo, ignore_errors=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_malformed_target_blocks_when_run_is_active(self):
        self._write_run('planned')
        r = self._run_pre_tool_use(
            {
                'cwd': None,
                'tool_name': 'Write',
                'tool_input': {'file_path': 'target.txt'},
            },
            cwd=self.repo,
            with_state_dir=False,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn('unresolvable_target', r.stdout)

    def test_pre_tool_use_malformed_target_without_run_allows(self):
        plain_repo = Path(tempfile.mkdtemp(prefix='office-hooks-plain-repo-'))
        (plain_repo / '.git').mkdir()
        try:
            r = self._run_pre_tool_use(
                {'cwd': None, 'tool_name': 'Write', 'tool_input': {'file_path': 'target.txt'}},
                cwd=plain_repo,
                with_state_dir=False,
            )
        finally:
            shutil.rmtree(plain_repo, ignore_errors=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_payload_cwd_selects_target_repository(self):
        self._write_run('planned')
        unrelated = Path(tempfile.mkdtemp(prefix='office-hooks-unrelated-'))
        (unrelated / '.git').mkdir()
        try:
            r = self._run_pre_tool_use(
                {
                    'cwd': str(self.repo),
                    'tool_name': 'Write',
                    'tool_input': {'file_path': 'target.txt'},
                },
                cwd=unrelated,
                with_state_dir=False,
            )
        finally:
            shutil.rmtree(unrelated, ignore_errors=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("phase 'planned'", r.stdout)

    def test_pre_tool_use_file_path_selects_target_repository(self):
        self._write_run('planned')
        unrelated = Path(tempfile.mkdtemp(prefix='office-hooks-unrelated-'))
        (unrelated / '.git').mkdir()
        try:
            r = self._run_pre_tool_use(
                {
                    'tool_name': 'Write',
                    'tool_input': {'file_path': str(self.repo / 'target.txt')},
                },
                cwd=unrelated,
                with_state_dir=False,
            )
        finally:
            shutil.rmtree(unrelated, ignore_errors=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("phase 'planned'", r.stdout)

    def test_pre_tool_use_approval_exemption_requires_standalone_command(self):
        self._write_run('planned')
        r = self._run_pre_tool_use(
            {'tool_name': 'Bash', 'tool_input': {'command': self._approval_command()}},
            cwd=ROOT,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_pre_tool_use_approval_exemption_rejects_chained_mutation(self):
        self._write_run('planned')
        command = self._approval_command(' && rm -f victim')
        r = self._run_pre_tool_use(
            {'tool_name': 'Bash', 'tool_input': {'command': command}},
            cwd=ROOT,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn('approval_required', r.stdout)

    def test_pre_tool_use_approval_exemption_rejects_untrusted_runtime_path(self):
        self._write_run('planned')
        command = (
            '/tmp/office_runtime.py approve-plan '
            f'--state-dir {shlex.quote(str(self._run_state_dir()))} '
            '--approved-by user --quote "I approve this plan."'
        )
        r = self._run_pre_tool_use(
            {'tool_name': 'Bash', 'tool_input': {'command': command}},
            cwd=ROOT,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn('approval_required', r.stdout)

    def test_pre_tool_use_approval_exemption_rejects_shell_injection_shapes(self):
        self._write_run('planned')
        commands = [
            'echo office_runtime.py approve-plan && rm -f victim',
            'python3 scripts/office_runtime.py approve-plan --quote "$(rm -f victim)"',
            'python3 scripts/office_runtime.py approve-plan --quote "safe" > victim',
            'python3 scripts/office_runtime.py approve-plan --quote "safe" 2>&victim',
            'python3 scripts/office_runtime.py approve-plan --quote "safe"\nrm -f victim',
        ]
        for command in commands:
            with self.subTest(command=command):
                r = self._run_pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': command}})
                self.assertEqual(r.returncode, 2)

    def test_pre_tool_use_malformed_state_blocks_mutation(self):
        self._write_run('intake', '{not-json')
        r = self._run_pre_tool_use({'tool_name': 'Write', 'tool_input': {'file_path': 'x'}})
        self.assertEqual(r.returncode, 2)
        self.assertIn('unreadable_run_state', r.stdout)
        self.assertIn('fails closed', r.stdout)

    def test_pre_tool_use_non_mutating_tool_allows_with_unapproved_run(self):
        self._write_run('intake')
        r = self._run_pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': 'git status --short'}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')

    def test_install_and_uninstall_hooks(self):
        install_script = ROOT / 'scripts' / 'hooks' / 'install_hooks.sh'
        fake_home = self.repo / 'home'
        (fake_home / '.claude').mkdir(parents=True)
        (fake_home / '.codex').mkdir(parents=True)
        (fake_home / '.gemini' / 'config').mkdir(parents=True)
        (fake_home / '.claude' / 'settings.json').write_text(json.dumps({
            'hooks': {}
        }))
        (fake_home / '.codex' / 'hooks.json').write_text(json.dumps({
            'hooks': {
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'node /old/office-skills/eval/hooks/catch-up.mjs --brand codex'}]}]
            }
        }))
        (fake_home / '.gemini' / 'config' / 'hooks.json').write_text(json.dumps({
            'office-skills': {
                'Stop': [{'hooks': [{'type': 'command', 'command': 'node /old/office-skills/eval/hooks/catch-up.mjs --brand gemini'}]}]
            }
        }))
        env = {**os.environ, 'HOME': str(fake_home), 'OFFICE_STATE_DIR': str(self.state_dir)}
        
        # Test install
        r = subprocess.run([str(install_script)], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        
        manifest_path = self.state_dir / 'hook-manifest.json'
        self.assertTrue(manifest_path.exists())
        
        # Validate manifest against schema
        schema_path = ROOT / 'schemas' / 'hook-manifest.schema.json'
        schema = json.loads(schema_path.read_text())
        manifest_data = json.loads(manifest_path.read_text())
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(manifest_data))
        self.assertEqual(errors, [])
        approval_hooks = [h for h in manifest_data['hooks'] if h['name'] == 'pre_tool_use_approval_gate']
        self.assertEqual(len(approval_hooks), 1)
        self.assertTrue(approval_hooks[0]['script'].endswith('/pre_tool_use.py'))

        for config_path in [fake_home / '.codex' / 'hooks.json', fake_home / '.gemini' / 'config' / 'hooks.json']:
            config = json.loads(config_path.read_text())
            self.assertNotIn('catch-up.mjs', json.dumps(config))
            self.assertIn(str(self.state_dir / 'hooks'), json.dumps(config))

        codex_config = json.loads((fake_home / '.codex' / 'hooks.json').read_text())
        self.assertIn('pre_tool_use.py', json.dumps(codex_config['hooks']['PreToolUse']))

        # Claude Code requires hooks.<Event> to be an array of matchers, not a bare
        # command string (a bare string is silently ignored by Claude Code).
        claude_config = json.loads((fake_home / '.claude' / 'settings.json').read_text())
        for event in ('SessionEnd', 'PreCompact', 'Stop'):
            self.assertIsInstance(claude_config['hooks'][event], list,
                                   f'hooks.{event} must be a matcher array, not a bare string')
            self.assertIn(str(self.state_dir / 'hooks'), json.dumps(claude_config['hooks'][event]))
        self.assertIn('pre_tool_use.py', json.dumps(claude_config['hooks']['PreToolUse']))
        claude_stop_commands = [
            hook['command']
            for matcher in claude_config['hooks']['Stop']
            for hook in matcher.get('hooks', [])
            if isinstance(hook, dict) and isinstance(hook.get('command'), str)
        ]
        self._assert_stop_hook_uses_executable_node(claude_stop_commands)
        gemini_config = json.loads((fake_home / '.gemini' / 'config' / 'hooks.json').read_text())
        self._assert_stop_hook_uses_executable_node(
            [command for command in gemini_config['Stop'] if isinstance(command, str)]
        )

        # Test uninstall
        r = subprocess.run([str(install_script), '--uninstall'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(manifest_path.exists())

    def test_pre_compact_and_advisor(self):
        # Create minimal state.json
        state_file = self.state_dir / 'state.json'
        state_file.write_text(json.dumps({
            'run_id': 'run-123',
            'family_id': 'fam-456',
            'phase': 'executing',
            'plan_version': 1,
            'packet_version': 1
        }))
        
        pre_compact = ROOT / 'scripts' / 'hooks' / 'pre_compact.sh'
        env = {**os.environ, 'OFFICE_STATE_DIR': str(self.state_dir)}
        r = subprocess.run([str(pre_compact)], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        
        compact_dir = self.state_dir / 'compact'
        self.assertTrue(compact_dir.exists())
        snapshots = list(compact_dir.glob('snapshot-*.json'))
        self.assertGreaterEqual(len(snapshots), 1)

        compact_advisor = ROOT / 'scripts' / 'hooks' / 'compact_advisor.sh'
        r = subprocess.run([str(compact_advisor)], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        catch_up = self.state_dir / 'catch-up.md'
        self.assertTrue(catch_up.exists())
        content = catch_up.read_text()
        self.assertIn('run-123', content)

    def test_close_panes_idempotent(self):
        close_panes = ROOT / 'scripts' / 'hooks' / 'close_panes.sh'
        env = {**os.environ, 'OFFICE_STATE_DIR': str(self.state_dir)}
        r = subprocess.run([str(close_panes)], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)

if __name__ == '__main__':
    unittest.main()
