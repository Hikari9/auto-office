"""Fail-closed guard for manual Git remote writes in plan-originated shell commands.

Office-owned PR syncing invokes Git using an argv list and separate branch/lease
authority. This guard is for user/planner-authored shell strings; it is not
a general-purpose shell sandbox.
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

_NEGATED = re.compile(r"^\s*(?:never|do\s+not|don't|must\s+not|avoid|forbid)\b", re.I)
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--config-env", "--namespace",
                      "--exec-path", "--super-prefix"}

# An unknown git subcommand may be a locally configured alias for git push.
# Permit ordinary built-ins; unknown extensions require separate authorization.
_KNOWN_GIT_COMMANDS = frozenset({
    "status", "fetch", "show", "log", "diff", "rev-parse", "merge-base",
    "ls-files", "ls-tree", "cat-file", "describe", "branch", "checkout",
    "switch", "worktree", "add", "commit", "merge", "rebase", "reset",
    "restore", "tag", "grep", "blame", "config", "remote", "help", "version",
    "check-ignore", "symbolic-ref", "for-each-ref", "archive", "submodule",
    "shortlog", "reflog", "cherry-pick", "revert", "stash", "update-index",
    "diff-tree", "diff-files", "name-rev", "show-ref", "fsck", "gc",
})


def unsafe_manual_push(command: str, *, prose: bool = False) -> bool:
    """Recognize prohibited manual push via POSIX shell quoting and wrappers."""
    if not isinstance(command, str) or not command.strip():
        return False
    if prose and _NEGATED.match(command) and not re.search(r"[;&|\n]", command):
        return False
    return _scan(command, 0)


def _scan(command: str, depth: int) -> bool:
    if depth > 5:
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return bool(re.search(r"push|git|eval", command, re.I))
    if not tokens:
        return False
    for token in tokens:
        # Shell -c, eval and quoted substitutions carry a nested command string.
        if any(ch.isspace() for ch in token) and _scan(token, depth + 1):
            return True
    for i, token in enumerate(tokens):
        if PurePosixPath(token).name != "git":
            continue
        j = i + 1
        while j < len(tokens):
            t = tokens[j]
            if t in _GLOBAL_WITH_VALUE:
                j += 2
            elif t.startswith("-"):
                j += 1
            else:
                break
        if j < len(tokens) and (tokens[j] == "push" or tokens[j] not in _KNOWN_GIT_COMMANDS):
            return True
    # Dynamic executable construction must not bypass a detectable push token.
    if "push" in tokens and (
        re.search(r"git", command, re.I)
        or "$" in command or "\x60" in command
        or any(t in tokens for t in ("eval", "source", "alias", "function"))
    ):
        return True
    # Dynamically evaluated shell commands cannot be proved safe.
    if any(t in ("eval", "source") for t in tokens) and ("$" in command or "\x60" in command):
        return True
    if any(t in ("sh", "bash", "dash", "zsh", "ksh") for t in tokens) and (
        any(t in ("-c", "-lc", "-ec") for t in tokens)
        and ("$" in command or "\x60" in command)
    ):
        return True
    return False
