"""One release identity, and the orchestrator skill's install check."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import check_ecosystem  # noqa: E402


def test_version_plugin_manifest_and_package_agree():
    assert check_ecosystem.check_versions() == []


def test_skill_checks_for_the_cli_before_anything_else():
    text = (ROOT / 'SKILL.md').read_text()
    check = text.index('## Install check')
    assert check < text.index('## Start')
    section = text[check:text.index('## Start')]
    assert '`office --version`' in section
    assert 'ask the user to approve installing it; never install' in section
    assert '`office install`' in section and '`VERSION`' in section
