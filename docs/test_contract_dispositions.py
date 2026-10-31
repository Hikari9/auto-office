"""Check that the contract suite's legacy table and the retirement inventory hold the same path-disposition rows."""

import re
from pathlib import Path

DOCS = Path(__file__).parent
ROW = re.compile(r"^\| `(tests/[^`]+)` \| ([\w-]+) \|", re.MULTILINE)


def _rows(doc: str, heading: str) -> list[tuple[str, str]]:
    body = (DOCS / doc).read_text(encoding="utf-8").split(heading, 1)[1]
    return ROW.findall(body.split("\n## ", 1)[0])


def test_legacy_table_and_inventory_hold_the_same_path_dispositions():
    inventory = _rows("test-retirement.md", "## Module inventory")
    legacy = _rows("contract-suite.md", "## Legacy test-module disposition")
    assert len({path for path, _ in inventory}) == len(inventory), "duplicate path in the retirement inventory"
    assert len({path for path, _ in legacy}) == len(legacy), "duplicate path in the legacy table"
    assert set(legacy) == set(inventory)
