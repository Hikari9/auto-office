"""Curated plugin discovery must not leak retired runtime skills."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from scripts.check_ecosystem import check_plugin_skills

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

    def test_wrong_codex_source_is_rejected(self):
        ctx, root = self.fixture()
        with ctx:
            (root / '.agents/plugins/marketplace.json').write_text(json.dumps(
                {'plugins':[{'source':{'path':'./'}}]}))
            self.assertTrue(any('expected ./plugin-dist' in err for err in check_plugin_skills(root)))

if __name__ == '__main__':
    unittest.main()
