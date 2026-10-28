#!/usr/bin/env python3
"""Select change-relevant Auto-Office tests for the local PR gate (#854).

This is a deliberately scoped regression check, NOT a proof that every test
has run. It is tied to the merge base and refuses to guess when base
information is missing. The full suite remains scripts/validate.sh.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Cheap invariant checks that every PR carries; they are not exhaustive.
ALWAYS = (
    "tests/test_version_single_source.py",
    "tests/v31/test_verification_coverage.py",
    "tests/test_pr_test_selection.py",
)
SPECIAL = {
    "catalog/": ("tests/test_schemas.py", "tests/test_alias_resolution.py",
                 "tests/v31/test_intelligence_floor.py"),
    "config/": ("tests/test_config.py", "tests/v31/test_config.py",
                "tests/v31/test_intelligence_floor.py"),
    "adapters/": ("tests/test_adapters.py", "tests/v31/test_harness_model_cli.py"),
    "schemas/": ("tests/test_schemas.py",),
    "skills/": ("tests/test_skills.py",),
    "src/office/web/": ("tests/web/test_service.py",),
    "src/office/_resources/": ("tests/test_resource_bundle.py",),
    ".githooks/": ("tests/v31/test_verification_coverage.py",),
    "scripts/": ("tests/test_pr_test_selection.py",),
    "pyproject.toml": ("tests/v31/test_verification_coverage.py",),
    "src/office/integration.py": ("tests/test_integration.py", "tests/v31/test_run_checks.py"),
    "src/office/land.py": ("tests/v31/test_land.py", "tests/v31/test_land_recovery.py"),
    "src/office/briefs.py": ("tests/v31/test_preflight.py",),
}


class SelectionError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode:
        raise SelectionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def selection(root: Path, base_ref: str = "refs/remotes/origin/main") -> tuple[str, list[str], list[str]]:
    """Return (merge base, changed files, relevant existing test modules)."""
    root = root.resolve()
    # Require a real local base; never turn a missing origin into an empty diff.
    git(root, "rev-parse", "--verify", base_ref + "^{commit}")
    base = git(root, "merge-base", "HEAD", base_ref)
    changed = set(git(root, "diff", "--name-only", "--diff-filter=ACDMRT", base, "HEAD").splitlines())
    changed.update(git(root, "diff", "--name-only", "--diff-filter=ACDMRT", "HEAD").splitlines())
    if not changed:
        raise SelectionError("No changed files versus PR base; refusing an empty PR-test attestation")
    if any(p.startswith("tests/") and not (root / p).is_file() for p in changed):
        raise SelectionError("A test file was deleted/renamed: review coverage and run full validation explicitly")

    tests = sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))
    selected = {p for p in ALWAYS if p in tests}
    # Direct module references in tests, including both import forms. This
    # deliberately adds affected test modules but cannot discover every indirect
    # dependency. See the printed scope disclosure and PR review requirements.
    relevant = [p for p in sorted(changed) if p.startswith(("src/", "scripts/")) and p.endswith(".py")]
    for path in sorted(changed):
        if path.startswith("tests/") and path in tests:
            selected.add(path)
        for prefix, targets in SPECIAL.items():
            if path == prefix or path.startswith(prefix):
                selected.update(t for t in targets if t in tests)
        if path.endswith((".js", ".css", ".html")) and "web/" in path:
            selected.update(t for t in tests if t.startswith("tests/web/") and
                            ("browser" not in t or "test_workbench" in t))
    for source in relevant:
        stem = Path(source).stem
        if stem in ("__init__", "_version"):
            continue
        pattern = re.compile(r"\b(?:from\s+office\s+import\s+|office\.|import\s+office\.|from\s+office\.)" + re.escape(stem) + r"\b")
        direct = [test for test in tests if Path(test).stem == f"test_{stem}"
                  or Path(test).stem.startswith(f"test_{stem}_")]
        selected.update(direct)
        # Broad lifecycle imports can appear in a hundred integration tests.
        # Their entire import closure is not a useful PR gate. Bound indirect
        # fan-out, give name-relevant tests priority, and disclose this scope.
        indirect = [test for test in tests if test not in direct
                    and pattern.search((root / test).read_text(encoding="utf-8", errors="replace"))]
        indirect.sort(key=lambda test: (stem not in Path(test).stem, test))
        selected.update(indirect[:4])
    if not selected:
        raise SelectionError("No test modules selected; fail closed")
    return base, sorted(changed), sorted(selected)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="refs/remotes/origin/main")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        base, changed, tests = selection(root, args.base)
    except SelectionError as exc:
        print(f"[PR GATE] BLOCKED: {exc}", file=sys.stderr)
        return 2
    args.output.write_text("\n".join(tests) + "\n")
    print(f"[PR GATE] HEAD {git(root, 'rev-parse', 'HEAD')}; merge base {base}")
    print(f"[PR GATE] {len(changed)} changed files: {', '.join(changed)}")
    print(f"[PR GATE] {len(tests)} selected test modules (scoped, not full-suite):")
    for name in tests:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
