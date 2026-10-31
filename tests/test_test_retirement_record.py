"""Check that the contract suite's legacy table and the retirement inventory hold the same path-disposition rows."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
ROW = re.compile(r"^\| `(tests/[^`]+)` \| ([\w-]+) \|", re.MULTILINE)


def _rows(doc: str, heading: str) -> list[tuple[str, str]]:
    text = (DOCS / doc).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    assert match, f"{doc} has no heading {heading!r}"
    body = text[match.end():]
    next_section = re.search(r"^## ", body, re.MULTILINE)
    if next_section:
        body = body[: next_section.start()]
    rows = ROW.findall(body)
    table_lines = [line for line in body.splitlines() if line.startswith("| `tests/")]
    assert rows and len(rows) == len(table_lines), f"{doc}: every test table row must match the (path, disposition) shape"
    return rows


def test_legacy_table_and_inventory_hold_the_same_path_dispositions():
    inventory = _rows("test-retirement.md", "## Module inventory")
    legacy = _rows("contract-suite.md", "## Legacy test-module disposition")
    assert len({path for path, _ in inventory}) == len(inventory), "duplicate path in the retirement inventory"
    assert len({path for path, _ in legacy}) == len(legacy), "duplicate path in the legacy table"
    assert set(legacy) == set(inventory)
