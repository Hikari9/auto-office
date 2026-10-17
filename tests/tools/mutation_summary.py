"""Summarise a finished `mutmut run` per function, and show the diff of every survivor.

    .venv/bin/python tests/tools/mutation_summary.py [--survivors] [REGEX]

REGEX filters mutant names such as `office.scoring.x_evaluate_capability_floor`. See docs/mutation-testing.md.
"""
from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

MUTMUT = str(Path(sys.executable).with_name("mutmut"))
STATUSES = ("killed", "survived", "timeout", "no tests", "skipped", "suspicious", "segfault", "not checked")


def _results() -> list[tuple[str, str]]:
    out = subprocess.run([MUTMUT, "results", "--all", "true"], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        name, sep, status = line.strip().partition(": ")
        if sep and "__mutmut_" in name:
            rows.append((name, status.strip()))
    return rows


def _function(name: str) -> str:
    return name.rsplit("__mutmut_", 1)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regex", nargs="?", default="")
    parser.add_argument("--survivors", action="store_true", help="print the diff of every surviving mutant")
    args = parser.parse_args()
    pattern = re.compile(args.regex)
    rows = [(n, s) for n, s in _results() if pattern.search(n)]
    per_function: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for name, status in rows:
        per_function[_function(name)][status] += 1
    total = collections.Counter(status for _, status in rows)
    width = max((len(f) for f in per_function), default=10)
    print(f"{'function':<{width}}  " + "  ".join(f"{s[:8]:>8}" for s in STATUSES))
    for function in sorted(per_function):
        counts = per_function[function]
        print(f"{function:<{width}}  " + "  ".join(f"{counts[s]:>8}" for s in STATUSES))
    print(f"{'TOTAL':<{width}}  " + "  ".join(f"{total[s]:>8}" for s in STATUSES))
    tested = sum(v for s, v in total.items() if s not in ("no tests", "skipped", "not checked"))
    if tested:
        print(f"killed {total['killed']}/{tested} of the mutants any test reached ({100 * total['killed'] / tested:.0f}%)")
    if args.survivors:
        for name, status in rows:
            if status == "survived":
                shown = subprocess.run([MUTMUT, "show", name], capture_output=True, text=True).stdout
                diff = [ln for ln in shown.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
                print(f"\n{name}\n" + "\n".join("   " + ln for ln in diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
