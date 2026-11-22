"""The retirement record's module inventory covers the test modules that exist, not only itself.

`docs/test-retirement.md` classifies every test module under `tests/` except `tests/contracts/`.
A module added to the tree without a row, a row for a module that is not there, and a Totals line
that drifted from the rows all fail here.
"""

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPOSITIONS = {"keep", "consolidate", "retire", "historical-diagnostic"}
ROW = re.compile(r"^\| `(tests/[^`]+)` \| ([\w-]+) \| (.*) \|$", re.MULTILINE)


def _inventory() -> tuple[list[tuple[str, str, str]], str]:
    text = (ROOT / "docs" / "test-retirement.md").read_text(encoding="utf-8")
    start = re.search(r"^## Module inventory$", text, re.MULTILINE)
    assert start, "docs/test-retirement.md has no Module inventory heading"
    body = text[start.end():]
    end = re.search(r"^## ", body, re.MULTILINE)
    body = body[: end.start()] if end else body
    rows = ROW.findall(body)
    table_lines = [line for line in body.splitlines() if line.startswith("| `tests/")]
    assert rows and len(rows) == len(table_lines), "every inventory row must be | `path` | disposition | reason |"
    return rows, body


def _test_modules() -> set[str]:
    return {path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").rglob("test_*.py")
            if "contracts" not in path.relative_to(ROOT / "tests").parts}


def test_the_inventory_lists_exactly_the_test_modules_that_exist_plus_the_retired_ones():
    rows, _ = _inventory()
    paths = [path for path, _, _ in rows]
    assert len(set(paths)) == len(paths), "duplicate path in the retirement inventory"
    on_disk = _test_modules()
    retired = {path for path, disposition, _ in rows if disposition == "retire"}
    assert on_disk - set(paths) == set(), "test modules with no inventory row"
    assert set(paths) - on_disk == retired, "an inventory row names a module that is gone but is not marked retire"
    assert retired.isdisjoint(on_disk), "a module marked retire still exists"


def test_every_inventory_row_has_a_known_disposition_and_a_reason_unless_it_is_keep():
    rows, _ = _inventory()
    for path, disposition, reason in rows:
        assert disposition in DISPOSITIONS, path
        if disposition != "keep":
            assert reason.strip(), f"{path} is {disposition} without a reason"


def test_the_inventory_totals_match_its_rows():
    rows, body = _inventory()
    match = re.search(r"^Totals: (\{.*\})\.$", body, re.MULTILINE)
    assert match, "the module inventory has no Totals line"
    assert ast.literal_eval(match.group(1)) == dict(Counter(disposition for _, disposition, _ in rows))
