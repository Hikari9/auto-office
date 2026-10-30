"""Run-level pytest tier disclosure: prevent an unlabelled partial integration gate (#563).

Only a repository declaring custom pytest ``--all`` tiers is subject to this
specific full-tier requirement. Other repositories retain their own checks.
Read the composed *commit*, never a potentially unrelated dirty working tree.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path


FULL = "full"
SCOPED = "scoped"  # verified change-relevant tests; NEVER a full-suite claim
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


def _validate_words(command: str) -> list[str]:
    words = _tokens(command)
    if words[:1] in (["bash"], ["sh"]):
        words = words[1:]
    return words


def _is_validate_script(command: str) -> bool:
    return _validate_words(command) in (["scripts/validate.sh"], ["./scripts/validate.sh"])


_OPERATOR_CHARS = frozenset("();<>|&")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_FULL_SUITE_WORDS = (["scripts/validate.sh"], ["./scripts/validate.sh"],
                     ["scripts/validate.sh", "--full"], ["./scripts/validate.sh", "--full"])


def _simple_commands(command: str) -> list[list[str]]:
    """Words of each simple command in a shell line. Operators (; && || | & and parentheses)
    separate commands. A redirection is dropped with its fd digit and target. Quoted text is one word."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    commands, words, skip_target = [], [], False
    for tok in tokens:
        if skip_target:
            skip_target = False
        elif tok and set(tok) <= _OPERATOR_CHARS:
            if "<" in tok or ">" in tok:
                if words and words[-1].isdigit():
                    words.pop()
                skip_target = True
            else:
                commands.append(words)
                words = []
        else:
            words.append(tok)
    commands.append(words)
    return [words for words in commands if words]


def _is_full_suite_words(words: list[str]) -> bool:
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if words[:1] in (["bash"], ["sh"]):
        words = words[1:]
        while words[:1] and words[0].startswith("-"):
            flag = words.pop(0)
            if flag in ("-o", "-O"):  # the option's value is the next word
                words = words[1:]
    return words in _FULL_SUITE_WORDS


def is_full_suite(command: str) -> bool:
    """The full regression run `--pr` does not replace: bare `scripts/validate.sh` or `--full`.

    A line is a full-suite check when any of its simple commands is one. Redirections, chaining
    and comments do not change which suite runs. Coverage evidence keeps the strict matcher above."""
    return any(_is_full_suite_words(words) for words in _simple_commands(command))


_SCOPED_GATE_SHA256 = {
    "scripts/validate.sh": "24c65e6e3cd90eb81d7ce777d5dc7798b05ae76714c3e618c925d7f693304157",
    "scripts/select_pr_tests.py": "ebdf1a495c490bc1d81b758f6a27c9925ac07518d504887ef4cfe3da2b5ff5d5",
    "tests/test_pr_test_selection.py": "e94154d0575e7a518a241e5f0c36e778d1c0bb6c9792a181a0d06835446ffd4b",
    "tests/test_version_single_source.py": "fe6547051a4444ae0bc39575afcbc36fbfe613ad693cf4a2a78e7783c8fe35e3",
    "tests/v31/test_verification_coverage.py": "363c07d0fbd3963c492b8118625f4373ed304c6b77077190058aeddcb36f9a27",
    "tests/v31/test_issue_563_artifact_integrity.py": "5829d9ae9bce96cade8dab0b8ec7c239d48d4a214d26773f3ab1eb2526525c8b",
    ".githooks/pre-push": "56374f2e62e8bf67242b8ec91eace602e55bf880cce06751022de50124502072",
    "tests/contracts/test_executor_policy.py": "088011a0d3992a695539925b2e186c206862fb63121e5b038d45e56a1fcec746",
    "tests/contracts/test_route_parity.py": "abbfc5288b84e1a9af689e971f2e325902b01af1aae2f5a78d12331f613aa689",
    "tests/contracts/core/test_cli_smokes.py": "09a5af324ad2a9298f4ffc60e9bfcd30240157a89fb2ae0ec9d23d9246bf07e5",
    "tests/contracts/core/test_configuration_integrity.py": "c78a2dd1ac6431d0118d16f9a81acf55b99f666cb5b6c34f657bb110afa79d2d",
    "tests/contracts/core/test_helpers.py": "eadd0e39841e47bb986ed50ea6f3add47f944c1c1aacb65aea9bf82b3dfe599b",
    "tests/contracts/core/test_policy_precedence.py": "e685bc86e30fa398622f3b9f27dee53a58054e95d07d38bce1acdc962a9b97cb",
    "tests/contracts/core/test_receipts_replay.py": "b68f18a9a44c2303fcf3c56daca4bda9e7f3452bc502eda97a9c0fef687bcdb9",
    "tests/contracts/core/test_runtime_selection.py": "14504c923f75b69a66f5a38763abe99ed387a53507bc89c979a5c6902ed7254f",
    "tests/contracts/core/test_safety_invariants.py": "e5c921b7259299858f45b8b3a820bdaadbaf92f803bf724ce70e6e23acf084fc",
}


def _scoped_validate(repo: Path, commit: str, command: str) -> bool:
    """Recognize the maintained PR selector from the exact committed tree.

    The selector must actually exist in that tree. A similarly named command
    in an unrelated repo cannot waive #563's full-suite requirement.
    """
    words = _tokens(command)
    if words[:1] in (["bash"], ["sh"]):
        words = words[1:]
    if words not in (["scripts/validate.sh", "--pr"], ["./scripts/validate.sh", "--pr"]):
        return False
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return False
    # Trust only the reviewed gate, selector, integrity tests and hook, byte for
    # byte. Updating any of them requires reviewing the attestation and updating
    # these digests in the same change.
    for path, expected in _SCOPED_GATE_SHA256.items():
        proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                              capture_output=True)
        if proc.returncode or hashlib.sha256(proc.stdout).hexdigest() != expected:
            return False
    return True


_ATTEST_PYTEST = ".venv/bin/python3 -m pytest tests/ -q -n 2 --all"


def supplemental_checks(repo: Path, commit: str, commands: list[str]) -> list[str]:
    if (_tiered_repo(repo, commit) and any(_is_validate_script(c) for c in commands)
            and not any(_complete_pytest_command(c) or _scoped_validate(repo, commit, c)
                        for c in commands)):
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
    if any(_scoped_validate(repo, commit, c) for c in selected):
        return SCOPED, ("change-targeted test modules executed with --all markers; "
                        "not the complete pytest suite")
    return PARTIAL, ("run-level checks do not demonstrate the complete pytest suite; "
                     f"{', '.join(tiers)} tests can be deselected by default; "
                     "shell script text is not execution evidence")


def attested_checks(con, run: dict, commit: str) -> list[str]:
    """Only an intact recorded check-output artifact attests full-tier coverage.

    Evidence metadata, even with a PASS gate, is insufficient if the log has
    been deleted, relocated outside the run evidence root, or modified. No
    implicit archive fallback is allowed: archived evidence must be a recorded
    and verifiable artifact before it can count.
    """
    import json
    from office import paths
    proof = ((run.get("landing") or {}).get("integration") or {}).get("full_suite") or {}
    if con is None or proof.get("commit") != commit or not proof.get("gate"):
        return []
    row = con.execute("SELECT status, verdict, input_key, revision_id FROM gates "
                      "WHERE id=? AND run_id=? AND kind='checks'",
                      (proof["gate"], run["id"])).fetchone()
    if (not row or row["status"] != "done" or row["verdict"] != "PASS"
            or row["input_key"] != f"integration:{commit}"
            or row["revision_id"] != "I" + commit[:7]):
        return []
    expected = proof.get("command")
    if not expected or not (_complete_pytest_command(expected)
                            or _scoped_validate(Path(run["repo_root"]), commit, expected)):
        return []
    run_root = paths.run_dir(run["id"])
    evidence_root = run_root / "evidence"
    integration_root = evidence_root / "integration"
    root = integration_root / str(row["revision_id"] or "")
    # Check the directory chain itself. Merely resolving the revision directory
    # would allow a symlink to relocate the supposedly authorized evidence root.
    if any(part.is_symlink() for part in (evidence_root, integration_root, root)):
        return []
    for record in con.execute(
            "SELECT revision_id, path, bytes, sha256, meta_json FROM evidence "
            "WHERE run_id=? AND gate_id=? AND kind='check_output'",
            (run["id"], proof["gate"])):
        try:
            meta = json.loads(record["meta_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if meta.get("command") != expected or meta.get("exit") != 0:
            continue
        if record["revision_id"] != row["revision_id"]:
            continue
        if not (isinstance(record["bytes"], int) and record["bytes"] >= 0
                and isinstance(record["sha256"], str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", record["sha256"])):
            continue
        if not record["path"]:
            continue
        path = Path(record["path"])
        if path.is_symlink() or not re.fullmatch(r"check-[1-9][0-9]*\.log", path.name):
            continue
        try:
            resolved = path.resolve(strict=True)
            anchor = root.resolve(strict=True)
            if (anchor.parent != integration_root.resolve(strict=True)
                    or resolved.parent != anchor or not resolved.is_file()):
                continue
            # A race-safe digest over one file descriptor, not metadata alone.
            fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as artifact:
                before = os.fstat(artifact.fileno())
                digest = hashlib.sha256()
                actual_size = 0
                for chunk in iter(lambda: artifact.read(1 << 20), b""):
                    digest.update(chunk)
                    actual_size += len(chunk)
                after = os.fstat(artifact.fileno())
            if (before.st_size == after.st_size == actual_size == record["bytes"]
                    and before.st_mtime_ns == after.st_mtime_ns
                    and "sha256:" + digest.hexdigest() == record["sha256"]):
                return [expected]
        except (OSError, ValueError):
            continue
    return []
