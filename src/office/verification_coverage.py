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


def _show(repo: Path, commit: str, path: str) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return None
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


# A pytest command cannot prove the full tier if it can alter collection,
# swallow the exit status, or invoke a different config. Favor the documented
# validation script over guessing the meaning of an unfamiliar pytest option.
_SAFE_SWITCHES = frozenset({
    "-q", "-qq", "-v", "-vv", "-vvv", "-s", "-x", "-ra", "-rA",
    "--disable-warnings", "--strict-markers", "--strict-config",
    "--no-header", "--no-summary", "--color=yes", "--color=no",
})
_SAFE_VALUES = frozenset({"-n", "--numprocesses", "--tb", "--maxfail", "--durations", "--dist"})
_SAFE_EQUALS = ("--tb=", "--maxfail=", "--durations=", "--dist=", "--color=", "-n=")


def _tokens(command: str) -> list[str]:
    """Parse a single simple shell invocation; refuse chains and substitutions."""
    if any(ch in command for ch in ";&|<>`\n") or "$(" in command:
        return []
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if words and words[0] == "env":
        words.pop(0)
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        key = words[0].split("=", 1)[0]
        if key.startswith("PYTEST_"):
            return []  # PYTEST_ADDOPTS can impose -m/-k without CLI flags
        words.pop(0)
    return words


def _complete_pytest_command(command: str) -> bool:
    """Only a real, unfiltered full-suite pytest invocation counts as evidence.

    Unrecognized flags/configuration are not automatically credited: pytest
    options and plugins can narrow collection without spelling `-k` or `-m`.
    """
    words = _tokens(command)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    if not words:
        return False
    exe = Path(words[0]).name
    if exe == "pytest":
        args = words[1:]
    elif re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", exe) and words[1:3] == ["-m", "pytest"]:
        args = words[3:]
    else:
        return False
    if "--all" not in args:
        return False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--all" or arg in _SAFE_SWITCHES or arg in ("tests/", "tests", "."):
            i += 1
        elif arg in _SAFE_VALUES and i + 1 < len(args) and args[i + 1]:
            i += 2
        elif any(arg.startswith(prefix) and len(arg) > len(prefix) for prefix in _SAFE_EQUALS):
            i += 1
        else:
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
        words = _tokens(command)
        if words[:1] in (["bash"], ["sh"]):
            words = words[1:]
        if words in (["scripts/validate.sh"], ["./scripts/validate.sh"]):
            script = _show(repo, commit, "scripts/validate.sh")
            if script and any(_complete_pytest_command(line) for line in script.splitlines() if not line.lstrip().startswith("#")):
                return FULL, "scripts/validate.sh runs the full pytest --all selection"
    return PARTIAL, (f"run-level checks do not demonstrate the complete pytest suite; "
                     f"{', '.join(tiers)} tests can be deselected by default")
