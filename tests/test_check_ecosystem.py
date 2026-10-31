"""Plugin manifest skills check in scripts/check_ecosystem.py."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.check_ecosystem import check_plugin_skills


class TestPluginSkillsField(unittest.TestCase):
    def test_repository_manifest_exposes_root_and_skills_dir(self):
        self.assertEqual(check_plugin_skills(), [])

    def test_dropping_root_skill_fails(self):
        errors = self._check_manifest(['./skills/'])

        self.assertEqual(errors, [
            ".claude-plugin/plugin.json: skills must expose './'",
        ])

    def test_dropping_skills_dir_fails(self):
        errors = self._check_manifest(['./'])

        self.assertEqual(errors, [
            ".claude-plugin/plugin.json: skills must expose './skills/'",
        ])

    def test_equivalent_spelling_of_required_entries_passes(self):
        self.assertEqual(self._check_manifest(['./', './skills']), [])

    def _check_manifest(self, skills):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'skills').mkdir()
            (root / '.claude-plugin').mkdir()
            (root / '.claude-plugin' / 'plugin.json').write_text(
                json.dumps({'name': 'auto-office', 'skills': skills}), encoding='utf-8')

            return check_plugin_skills(root)


if __name__ == '__main__':
    unittest.main()
