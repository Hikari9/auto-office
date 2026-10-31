"""Curated plugin discovery must not leak retired runtime skills."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from scripts.check_ecosystem import check_plugin_skills, discovered_skills, duplicate_name_errors, frontmatter


def _write_skill(root, rel, name):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: fixture\n---\n')


def _duplicate_errors_for(root):
    named = []
    for path in discovered_skills(root):
        ok, _, fm, _ = frontmatter(path)
        if ok:
            named.append((path.relative_to(root), fm['name']))
    return duplicate_name_errors(named)


class TestDuplicateSkillNames(unittest.TestCase):
    def test_real_duplicate_is_rejected(self):
        self.assertEqual(
            duplicate_name_errors([('skills/a/SKILL.md', 'x'), ('skills/b/SKILL.md', 'x')]),
            ['skills/b/SKILL.md: duplicate skill name x'])

    def test_distinct_names_pass(self):
        self.assertEqual(duplicate_name_errors([('skills/a/SKILL.md', 'a'), ('skills/b/SKILL.md', 'b')]), [])

    def test_root_hub_and_distributed_wrapper_are_not_both_discovered(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, 'SKILL.md', 'auto-office')
            _write_skill(root, 'plugin-dist/skills/auto-office/SKILL.md', 'auto-office')
            self.assertEqual(_duplicate_errors_for(root), [])

    def test_real_duplicate_in_source_tree_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, 'skills/one/SKILL.md', 'same')
            _write_skill(root, 'skills/two/SKILL.md', 'same')
            self.assertEqual(_duplicate_errors_for(root), ['skills/two/SKILL.md: duplicate skill name same'])


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
