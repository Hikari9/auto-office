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


def _tiered_repo(repo: Path, commit: str) -> list[str]:
    conftest = _show(repo, commit, "tests/conftest.py")
    if not conftest or "pytest_addoption" not in conftest or '"--all"' not in conftest \
            or "pytest_collection_modifyitems" not in conftest:
        return []
    return [name for name in ("integration", "legacy", "slow") if name in conftest]


def _is_validate_script(command: str) -> bool:
    words = _tokens(command)
    if words[:1] in (["bash"], ["sh"]):
        words = words[1:]
    return words in (["scripts/validate.sh"], ["./scripts/validate.sh"])


_ATTEST_PYTEST = ".venv/bin/python3 -m pytest tests/ -q -n 2 --all"


def supplemental_checks(repo: Path, commit: str, commands: list[str]) -> list[str]:
    if (_tiered_repo(repo, commit) and any(_is_validate_script(c) for c in commands)
            and not any(_complete_pytest_command(c) for c in commands)):
        return [_ATTEST_PYTEST]
    return []


def audit(repo: Path, commit: str, commands: list[str], *,
          verified_commands: list[str] | None = None) -> tuple[str, str] | None:
    tiers = _tiered_repo(repo, commit)
    if not tiers:
        return None
    selected = commands if verified_commands is None else verified_commands
    if any(_complete_pytest_command(c) for c in selected):
        return FULL, "full pytest --all selection executed by integration checks"
    return PARTIAL, ("run-level checks do not demonstrate the complete pytest suite; "
                     f"{', '.join(tiers)} tests can be deselected by default; "
                     "shell script text is not execution evidence")


def attested_checks(con, run: dict, commit: str) -> list[str]:
    import json
    proof = ((run.get("landing") or {}).get("integration") or {}).get("full_suite") or {}
    if con is None or proof.get("commit") != commit or not proof.get("gate"):
        return []
    row = con.execute("SELECT status, verdict, input_key FROM gates "
                      "WHERE id=? AND run_id=? AND kind='checks'",
                      (proof["gate"], run["id"])).fetchone()
    if not row or row["status"] != "done" or row["verdict"] != "PASS" or row["input_key"] != f"integration:{commit}":
        return []
    expected = proof.get("command")
    if not expected or not _complete_pytest_command(expected):
        return []
    for record in con.execute("SELECT meta_json FROM evidence WHERE run_id=? AND gate_id=? AND kind='check_output'",
                              (run["id"], proof["gate"])):
        try:
            meta = json.loads(record["meta_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if meta.get("command") == expected and meta.get("exit") == 0:
            return [expected]
    return []
