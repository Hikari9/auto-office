"""Curated plugin discovery must not leak retired runtime skills."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import check_ecosystem as ce
from scripts.check_ecosystem import check_plugin_skills, frontmatter


def _write_skill(root, rel, name):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: fixture\n---\n')


def _run_main(root):
    """Run the production main() against a fixture root; return (exit code, printed output)."""
    out = io.StringIO()
    with mock.patch.object(ce, 'ROOT', root), contextlib.redirect_stdout(out):
        code = ce.main()
    return code, out.getvalue()


class TestDuplicateSkillNames(unittest.TestCase):
    def test_main_rejects_a_real_duplicate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, 'skills/one/SKILL.md', 'same')
            _write_skill(root, 'skills/two/SKILL.md', 'same')
            code, out = _run_main(root)
            self.assertEqual(code, 1)
            self.assertIn('duplicate skill name same', out)

    def test_main_does_not_flag_root_hub_and_distributed_wrapper(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, 'SKILL.md', 'auto-office')
            _write_skill(root, 'plugin-dist/skills/auto-office/SKILL.md', 'auto-office')
            _, out = _run_main(root)
            self.assertNotIn('duplicate skill name', out)


class TestFrontmatterContract(unittest.TestCase):
    def _check(self, body):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'SKILL.md'
            path.write_text(body)
            ok, msg, _, _ = frontmatter(path)
        return ok, msg

    def test_extra_key_is_rejected(self):
        ok, _ = self._check('---\nname: a\ndescription: d\nlicense: MIT\n---\n')
        self.assertFalse(ok)

    def test_non_boolean_invocation_flag_is_rejected(self):
        ok, msg = self._check('---\nname: a\ndescription: d\ndisable-model-invocation: "yes"\n---\n')
        self.assertFalse(ok)
        self.assertIn('must be a boolean', msg)

    def test_boolean_invocation_flag_is_accepted(self):
        ok, _ = self._check('---\nname: a\ndescription: d\ndisable-model-invocation: true\n---\n')
        self.assertTrue(ok)

    def test_scalar_frontmatter_fails_without_crashing(self):
        ok, msg = self._check('---\n5\n---\n')
        self.assertFalse(ok)
        self.assertIn('mapping', msg)

    def test_non_string_name_fails_without_crashing(self):
        ok, msg = self._check('---\nname: 123\ndescription: d\n---\n')
        self.assertFalse(ok)
        self.assertIn('bad name', msg)


class TestPluginSkillsField(unittest.TestCase):
    def test_actual_repo_passes(self):
        self.assertEqual(check_plugin_skills(), [])

    def fixture(self):
        ctx = TemporaryDirectory()
        root = Path(ctx.name)
        (root / '.claude-plugin').mkdir()
        (root / '.agents/plugins').mkdir(parents=True)
        (root / 'plugin-dist/skills/auto-office').mkdir(parents=True)
        (root / 'plugin-dist/skills/auto-office/SKILL.md').write_text('---\n')
        (root / 'plugin-distribution.json').write_text(json.dumps(
            {'public_skills':['auto-office'], 'retired_skills':['office-submit']}))
        (root / '.claude-plugin/plugin.json').write_text(json.dumps({'name':'auto-office'}))
        (root / '.claude-plugin/marketplace.json').write_text(json.dumps(
            {'plugins':[{'source':'./plugin-dist'}]}))
        (root / '.agents/plugins/marketplace.json').write_text(json.dumps(
            {'plugins':[{'source':{'path':'./plugin-dist'}}]}))
        return ctx, root

    def test_valid_fixture_passes(self):
        ctx, root = self.fixture()
        with ctx:
            self.assertEqual(check_plugin_skills(root), [])

    def test_missing_root_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            (root / 'plugin-dist/skills/auto-office/SKILL.md').unlink()
            self.assertTrue(any('catalog mismatch' in err for err in check_plugin_skills(root)))

    def test_retired_skill_leak_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            skill = root / 'plugin-dist/skills/office-submit'
            skill.mkdir()
            (skill / 'SKILL.md').write_text('---\n')
            self.assertTrue(any('retired visible' in err for err in check_plugin_skills(root)))

    def test_old_root_manifest_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.claude-plugin/plugin.json').write_text(json.dumps(
                {'name':'auto-office','skills':['./','./skills/']}))
            self.assertTrue(any('legacy root skills' in err for err in check_plugin_skills(root)))

    def test_invalid_plugin_json_is_reported(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.claude-plugin/plugin.json').write_text('{bad json')
            self.assertTrue(any('invalid JSON' in err for err in check_plugin_skills(root)))

    def test_missing_claude_marketplace_is_reported(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.claude-plugin/marketplace.json').unlink()
            self.assertTrue(any('marketplace missing' in err for err in check_plugin_skills(root)))

    def test_wrong_claude_source_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.claude-plugin/marketplace.json').write_text(json.dumps(
                {'plugins':[{'source':'./'}]}))
            self.assertTrue(any('expected ./plugin-dist' in err for err in check_plugin_skills(root)))

    def test_wrong_codex_source_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.agents/plugins/marketplace.json').write_text(json.dumps(
                {'plugins':[{'source':{'path':'./'}}]}))
            self.assertTrue(any('expected ./plugin-dist' in err for err in check_plugin_skills(root)))


if __name__ == '__main__':
    unittest.main()
