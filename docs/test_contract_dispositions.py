"""Check that the legacy inventory preserves the retirement record's classifications."""

import re
from pathlib import Path


def test_legacy_dispositions_match_retirement_inventory():
    docs = Path(__file__).parent
    inventory = (docs / "test-retirement.md").read_text().split("## Module inventory", 1)[1]
    legacy = (docs / "contract-suite.md").read_text().split("## Legacy test-module disposition", 1)[1]
    pattern = r"^\| `(tests/[^`]+\.py)` \| ([\w-]+) \|"
    expected = dict(re.findall(pattern, inventory, re.MULTILINE))
    actual = dict(re.findall(pattern, legacy, re.MULTILINE))
    assert len(expected) == 298
    assert {path: actual.get(path) for path in expected} == expected
