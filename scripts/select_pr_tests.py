#!/usr/bin/env python3
"""Select named safety-smoke and change-relevant tests for the local PR gate.

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

# Named safety-smoke checks accompany the mandatory tests/contracts suite.
SAFETY_SMOKE = (
    "tests/v31/test_verification_coverage.py",
    "tests/v31/test_issue_563_artifact_integrity.py",
    "tests/test_pr_test_selection.py",
)
SPECIAL = {
    "catalog/": ("tests/test_schemas.py", "tests/test_alias_resolution.py",
                 "tests/v31/test_intelligence_floor.py", "tests/test_shipped_catalog_tables.py"),
    "config/": ("tests/v31/test_config_command.py", "tests/v31/test_config_sandbox.py",
                "tests/v31/test_intelligence_floor.py"),
    "adapters/": ("tests/test_adapters.py", "tests/v31/test_harness_model_cli.py"),
    "schemas/": ("tests/test_schemas.py",),
    "skills/": ("tests/test_skills.py",),
    "src/office/web/": ("tests/web/test_server.py",),
    "src/office/_resources/": ("tests/test_resource_bundle.py",),
    ".githooks/": ("tests/test_pre_push_hook.py",),
    "scripts/select_pr_tests.py": ("tests/test_pr_test_selection.py",),
    "pyproject.toml": ("tests/v31/test_verification_coverage.py",),
    "src/office/integration.py": ("tests/v31/test_run_checks.py", "tests/v31/test_verification_coverage.py"),
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


def selection(root: Path, base_ref: str = "refs/remotes/origin/main", *,
              reasons: dict[str, set[str]] | None = None) -> tuple[str, list[str], list[str]]:
    """Return (merge base, changed files, relevant existing test modules)."""
    root = root.resolve()
    # Require a real local base; never turn a missing origin into an empty diff.
    git(root, "rev-parse", "--verify", base_ref + "^{commit}")
    base = git(root, "merge-base", "HEAD", base_ref)
    changed = set(git(root, "diff", "--no-renames", "--name-only", "--diff-filter=ACDMRT", base, "HEAD").splitlines())
    changed.update(git(root, "diff", "--no-renames", "--name-only", "--diff-filter=ACDMRT", "HEAD").splitlines())
    if not changed:
        raise SelectionError("No changed files versus PR base; refusing an empty PR-test attestation")
    if changed.intersection({"conftest.py", "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"}):
        raise SelectionError("Test configuration changed: run full validation explicitly")
    if any(p.startswith("tests/") and not (root / p).is_file() for p in changed):
        raise SelectionError("A test file was deleted/renamed: review coverage and run full validation explicitly")

    tests = sorted(p for p in git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", "tests").splitlines()
                   if Path(p).name.startswith("test_") and p.endswith(".py"))
    if any(p.startswith("tests/") and p not in tests for p in changed):
        raise SelectionError("Test infrastructure changed: run full validation explicitly")
    selected: set[str] = set()

    def include(test: str, reason: str) -> None:
        # A selector can name an exact pytest node as well as a module.
        if test in tests or ("::" in test and test.split("::", 1)[0] in tests):
            selected.add(test)
            if reasons is not None:
                reasons.setdefault(test, set()).add(reason)

    missing_smoke = sorted(name for name in SAFETY_SMOKE if name not in tests)
    if missing_smoke:
        raise SelectionError("Safety-smoke test modules are missing: " + ", ".join(missing_smoke))
    for name in SAFETY_SMOKE:
        include(name, "named safety smoke")
    # Direct module references in tests, including both import forms. This
    # deliberately adds affected test modules but cannot discover every indirect
    # dependency. See the printed scope disclosure and PR review requirements.
    relevant = [p for p in sorted(changed) if p.startswith(("src/", "scripts/")) and p.endswith(".py")]
    for path in sorted(changed):
        if path.startswith("tests/") and path in tests:
            include(path, "modified test")
        for prefix, targets in SPECIAL.items():
            if path == prefix or path.startswith(prefix):
                for test in targets:
                    include(test, f"explicit mapping: {prefix}")
        if path.endswith((".js", ".css", ".html")) and "web/" in path:
            for test in tests:
                if test.startswith("tests/web/") and ("browser" not in test or "test_workbench" in test):
                    include(test, f"web asset: {path}")
    for source in relevant:
        stem = Path(source).stem
        if stem in ("__init__", "_version"):
            continue
        pattern = re.compile(r"\b(?:from\s+office\s+import\s+|office\.|import\s+office\.|from\s+office\.)" + re.escape(stem) + r"\b")
        direct = [test for test in tests if Path(test).stem == f"test_{stem}"
                  or Path(test).stem.startswith(f"test_{stem}_")]
        for test in direct:
            if source == "src/office/integration.py" and test == "tests/test_integration.py":
                # This is a legacy 3.0 integration module with many unrelated
                # end-to-end tests; its single v3.1 source import is in #577.
                include("tests/test_integration.py::test_issue_577_failed_integration_check_evidence_includes_log",
                        "direct legacy regression: office.integration")
            else:
                include(test, f"direct test: {source}")
        # Unrelated imports must not turn a PR gate back into a nearly full
        # suite. Keep one cross-module test if there is direct/mapped coverage,
        # or two fallback importers if this module has no explicit tests.
        mapped = any((source == prefix or source.startswith(prefix))
                     and any(t in tests for t in targets)
                     for prefix, targets in SPECIAL.items())
        indirect = [test for test in tests if test not in direct
                    and pattern.search((root / test).read_text(encoding="utf-8", errors="replace"))]
        indirect.sort(key=lambda test: (stem not in Path(test).stem, test))
        for test in indirect[:1 if (direct or mapped) else 2]:
            include(test, f"bounded import dependency: {source}")
        if not direct and not indirect and not mapped:
            raise SelectionError(
                f"No tests mapped to changed runtime module {source}; "
                "add an explicit test or a selector mapping before landing")
    if not selected:
        raise SelectionError("No test modules selected; fail closed")
    return base, sorted(changed), sorted(selected)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="refs/remotes/origin/main")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    reasons: dict[str, set[str]] = {}
    try:
        base, changed, tests = selection(root, args.base, reasons=reasons)
    except SelectionError as exc:
        print(f"[PR GATE] BLOCKED: {exc}", file=sys.stderr)
        return 2
    args.output.write_text("\n".join(tests) + "\n")
    print(f"[PR GATE] HEAD {git(root, 'rev-parse', 'HEAD')}; merge base {base}")
    print(f"[PR GATE] {len(changed)} changed files: {', '.join(changed)}")
    print(f"[PR GATE] named safety-smoke set: {', '.join(SAFETY_SMOKE)}")
    print(f"[PR GATE] {len(tests)} selected test modules (scoped, not full-suite):")
    for name in tests:
        print(f"  {name} — {'; '.join(sorted(reasons.get(name) or {'unclassified'}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
