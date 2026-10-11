"""Run-level pytest tier disclosure: prevent an unlabelled partial integration gate (#563).

Only a repository declaring custom pytest ``--all`` tiers is subject to this
specific full-tier requirement. Other repositories retain their own checks.
Read the composed *commit*, never a potentially unrelated dirty working tree.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path


FULL = "full"
PARTIAL = "partial"
OMITTED = "integration, legacy and slow"


def _show(repo: Path, commit: str, path: str) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return None
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _complete_pytest_command(command: str) -> bool:
    """Recognize an unfiltered, entire-suite `pytest --all`, not a selected test.

    A narrower command is never credited as complete just for including
    ``--all`` (for example ``pytest tests/unit --all`` or ``pytest -k name --all``).
    Unrecognised wrappers conservatively require the documented validation script.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    idx = next((i for i, token in enumerate(words) if token == "pytest" or token.endswith("/pytest")), None)
    if idx is None:
        return False
    args = words[idx + 1:]
    if "--all" not in args or any(x in args for x in ("-k", "-m", "--ignore", "--ignore-glob", "--deselect",
                                                        "--lf", "--last-failed", "--collect-only", "--co")):
        return False
    if any(x.startswith(("-k=", "-m=", "--ignore=", "--deselect=", "--ignore-glob=")) for x in args):
        return False
    # A named test file/directory outside the entire tests root is partial.
    if any(x.startswith("tests/") and x != "tests/" for x in args):
        return False
    if any(x.endswith(".py") for x in args):
        return False
    return True


def audit(repo: Path, commit: str, commands: list[str]) -> tuple[str, str] | None:
    """Known pytest tier coverage, or None when no custom --all suite is declared."""
    conftest = _show(repo, commit, "tests/conftest.py")
    if not conftest or "pytest_addoption" not in conftest or '"--all"' not in conftest \
            or "pytest_collection_modifyitems" not in conftest:
        return None
    tiers = [name for name in ("integration", "legacy", "slow") if name in conftest]
    if not tiers:
        return None
    for command in commands:
        if _complete_pytest_command(command):
            return FULL, "full pytest selection (--all; no test-path or marker restriction)"
        # A documented validation script may run other static/adapter checks too.
        if re.search(r"(?:^|[\s;&])(?:\./)?scripts/validate\.sh(?:[\s;&]|$)", command):
            script = _show(repo, commit, "scripts/validate.sh")
            if script and any(_complete_pytest_command(line) for line in script.splitlines() if not line.lstrip().startswith("#")):
                return FULL, "scripts/validate.sh runs the full pytest --all selection"
    return PARTIAL, (f"run-level checks do not demonstrate the complete pytest suite; "
                     f"{', '.join(tiers)} tests can be deselected by default")
