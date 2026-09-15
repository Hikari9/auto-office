"""Context-load line budget tests."""
import unittest
from pathlib import Path

from scripts.check_ecosystem import HUB_LINE_BUDGET, SKILL_LINE_BUDGETS, check_skill_budgets


class TestSkillBudgets(unittest.TestCase):
    def test_hub_over_budget_fails(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SKILL.md').write_text('line\n' * (HUB_LINE_BUDGET + 1), encoding='utf-8')

            errors = check_skill_budgets(root)

        self.assertEqual(errors, [
            f'SKILL.md: {HUB_LINE_BUDGET + 1} lines exceeds line budget of {HUB_LINE_BUDGET}'
        ])

    def test_skill_over_budget_fails(self):
        from tempfile import TemporaryDirectory

        relative = 'skills/agy-cli/SKILL.md'
        budget = SKILL_LINE_BUDGETS[relative]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text('line\n' * (budget + 1), encoding='utf-8')

            errors = check_skill_budgets(root)

        self.assertEqual(errors, [
            f'{relative}: {budget + 1} lines exceeds line budget of {budget}'
        ])

    def test_nested_skill_requires_budget_and_passes_once_budgeted(self):
        from tempfile import TemporaryDirectory

        relative = 'skills/foo/references/SKILL.md'
        line_count = 3
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text('line\n' * line_count, encoding='utf-8')

            self.assertEqual(check_skill_budgets(root), [
                f'{relative}: no line budget configured'
            ])

            try:
                SKILL_LINE_BUDGETS[relative] = line_count - 1
                self.assertEqual(check_skill_budgets(root), [
                    f'{relative}: {line_count} lines exceeds line budget of {line_count - 1}'
                ])

                SKILL_LINE_BUDGETS[relative] = line_count
                self.assertEqual(check_skill_budgets(root), [])
            finally:
                SKILL_LINE_BUDGETS.pop(relative, None)


if __name__ == '__main__':
    unittest.main()
