"""One release identity, and the orchestrator skill's install check."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import check_ecosystem  # noqa: E402


def test_version_has_one_source_and_manifests_carry_none():
    assert check_ecosystem.check_versions() == []


def test_skill_checks_for_the_cli_before_anything_else():
    text = (ROOT / 'SKILL.md').read_text()
    check = text.index('## Install check')
    assert check < text.index('## Start')
    section = text[check:text.index('## Start')]
    assert '`office --version`' in section
    assert 'only after the user picks **Install the CLI**' in section  # consent is the native choice since #924
    assert '`office install`' in section and '`src/office/_version.py`' in section


import pytest as _pytest  # noqa: E402

pytestmark = _pytest.mark.legacy  # tests the 3.0 scripts/ surface
