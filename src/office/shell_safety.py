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

_WRAPPERS = frozenset({"env", "timeout", "nice", "sudo", "nohup", "stdbuf", "command", "time"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_SEPARATORS = frozenset({";", "&", "|", "(", ")"})

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# In plan prose, matching Markdown code delimiters are formatting, not shell
# substitution. Retain the code text so actual git pushes/aliases still scan.
_INLINE_CODE = re.compile(r"(?<!`)(`+)([^`\n]+?)\1(?!`)")


def _is_dynamic(token: str) -> bool:
    """Conservatively fail-closed: any expansion/glob metachar is dynamic.

    Deliberately ignores shlex posix=True quoting loss: a quoted "$D" is
    still rejected as dynamic rather than trusted as a safe literal.
    """
    if "$" in token or "`" in token:
        return True
    for ch in "*?[]{}~":
        if ch in token:
            return True
    return False


def unsafe_manual_push(command: str, *, prose: bool = False) -> bool:
    """Recognize prohibited manual push via POSIX shell quoting and wrappers."""
    if not isinstance(command, str) or not command.strip():
        return False
    if prose and _NEGATED.match(command) and not re.search(r"[;&|\n]", command):
        return False
    if prose:
        command = _INLINE_CODE.sub(lambda match: match.group(2), command)
    return _scan(command, 0)


def _scan(command: str, depth: int) -> bool:
    if depth > 5:
        return True
    # For executable shell fields, scan backtick substitution bodies before
    # shlex tokenizes them; a non-leading substitution can otherwise be missed.
    if any(_scan(span.group(2), depth + 1) for span in _INLINE_CODE.finditer(command)):
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
    if _scan_wrappers(tokens, depth):
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


def _scan_wrappers(tokens: list[str], depth: int) -> bool:
    """Resolve wrapper prefixes at each command start; fail closed on dynamic."""
    for start in _command_starts(tokens):
        result = _resolve_at(tokens, start, depth)
        if result:
            return True
    return False


def _command_starts(tokens: list[str]) -> list[int]:
    starts: list[int] = []
    for i, tok in enumerate(tokens):
        if tok in _SEPARATORS or tok in ("<", ">"):
            continue
        if i == 0 or tokens[i - 1] in _SEPARATORS or tokens[i - 1] in ("<", ">"):
            starts.append(i)
    return starts


def _resolve_at(tokens: list[str], pos: int, depth: int) -> bool:
    """Return True if the command at pos is an unsafe (unverifiable/push) one."""
    seen_wrappers = 0
    while pos < len(tokens):
        name = PurePosixPath(tokens[pos]).name
        if name in _SHELLS:
            if _shell_c_is_unsafe(tokens, pos, depth):
                return True
            # A shell without -c (e.g. `sh script.sh`) is opaque; only the
            # -c script case is resolvable here. Other shell forms fall
            # through to the generic dynamic fallbacks in _scan.
            return False
        if name not in _WRAPPERS or seen_wrappers > 8:
            break
        nxt = _strip_wrapper(tokens, pos)
        if nxt is None:
            # Dynamic/unverifiable wrapper operand: fail closed.
            return True
        if nxt == pos:
            break
        pos, seen_wrappers = nxt, seen_wrappers + 1
    if pos >= len(tokens):
        return False
    head = tokens[pos]
    if _is_dynamic(head):
        # Inner command itself is computed (e.g. `env $X git push`,
        # `timeout 5 $CMD`): cannot prove it is not `git push`.
        # Only fail closed when the surrounding context suggests a remote
        # write attempt or the head is the sole command word; a bare
        # dynamic word with no push token elsewhere is still unverifiable
        # if it follows a wrapper (the wrapper chain proves intent to exec).
        return True
    if PurePosixPath(head).name == "git":
        return _git_is_push(tokens, pos)
    return False


def _git_is_push(tokens: list[str], pos: int) -> bool:
    j = pos + 1
    while j < len(tokens):
        t = tokens[j]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t in _GLOBAL_WITH_VALUE:
            j += 2
        elif t.startswith("-"):
            j += 1
        else:
            break
    if j < len(tokens) and tokens[j] not in _SEPARATORS:
        return tokens[j] == "push" or tokens[j] not in _KNOWN_GIT_COMMANDS
    return False


def _shell_c_is_unsafe(tokens: list[str], pos: int, depth: int) -> bool:
    for k in range(pos + 1, min(pos + 4, len(tokens))):
        t = tokens[k]
        if t == "--":
            continue
        if t == "-c" or (t.startswith("-") and "c" in t[1:] and t not in ("--",)):
            if k + 1 < len(tokens):
                script = tokens[k + 1]
                if _is_dynamic(script) and ("$" in script or "`" in script):
                    return True
                if _scan(script, depth + 1):
                    return True
            return False
        if not t.startswith("-"):
            return False
    return False


def _consume_value(tokens: list[str], pos: int) -> bool:
    """True if tokens[pos] exists and is static (safe to consume as a value)."""
    if pos >= len(tokens):
        return False
    return not _is_dynamic(tokens[pos])


def _strip_wrapper(tokens: list[str], pos: int) -> int | None:
    """Advance pos past one wrapper's options. None = dynamic (deny)."""
    name = PurePosixPath(tokens[pos]).name
    if name == "env":
        return _strip_env(tokens, pos)
    if name == "timeout":
        return _strip_timeout(tokens, pos)
    if name == "nice":
        return _strip_nice(tokens, pos)
    if name == "sudo":
        return _strip_sudo(tokens, pos)
    if name == "nohup":
        return _strip_nohup(tokens, pos)
    if name == "stdbuf":
        return _strip_stdbuf(tokens, pos)
    if name == "command":
        return _strip_command(tokens, pos)
    if name == "time":
        return _strip_time(tokens, pos)
    return pos


def _strip_env(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    want_value = {"-C", "--chdir", "-S", "--split-string", "--argv0",
                  "--argv-zero", "-u", "--unset", "--block-signal", "--default-signal"}
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t == "--":
            i += 1
            # After --, assignments may still precede the command.
            while i < len(tokens) and _ASSIGN.match(tokens[i]):
                if _is_dynamic(tokens[i]):
                    return None
                i += 1
            return i
        if t in ("-u", "--unset"):
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--unset=") or t.startswith("--chdir=") or t.startswith("-C"):
            if t.startswith("-C") and t not in ("-C",):
                if _is_dynamic(t[2:]):
                    return None
                i += 1
                continue
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-u") and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if t in want_value:
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--") and "=" in t:
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-") and t != "-" and "=" not in t:
            if _is_dynamic(t):
                return None
            i += 1
            continue
        if _ASSIGN.match(t):
            if _is_dynamic(t):
                return None
            i += 1
            continue
        break
    return i


def _strip_timeout(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t == "--":
            i += 1
            break
        if t in ("-s", "--signal", "-k", "--kill-after", "--duration"):
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--signal=") or t.startswith("--kill-after=") or t.startswith("--duration="):
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-s") and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if t.startswith("-k") and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if t.startswith("--") and "=" in t:
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-") and t != "-" and "=" not in t:
            if _is_dynamic(t):
                return None
            # --preserve-status, --foreground, -v, -s<attached> handled above.
            i += 1
            continue
        break
    # Duration operand is mandatory and must be static.
    if i >= len(tokens) or tokens[i] in _SEPARATORS:
        return i
    if _is_dynamic(tokens[i]):
        return None
    return i + 1


def _strip_nice(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t == "--":
            return i + 1
        if t in ("-n", "--adjustment"):
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--adjustment="):
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-n") and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if re.match(r"^-\d+$", t):
            i += 1
            continue
        if t.startswith("-") and t != "-":
            if _is_dynamic(t):
                return None
            i += 1
            continue
        break
    return i


_SUDO_WANT_VALUE = {"-u", "--user", "-g", "--group", "-C", "--close-from",
                    "-D", "--chdir", "-p", "--prompt", "-r", "--role",
                    "-t", "--type", "--command-timeout", "--chroot", "--cwd",
                    "--login-class", "--group-id", "--user-id"}


def _strip_sudo(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t == "--":
            return i + 1
        if t in _SUDO_WANT_VALUE:
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--") and "=" in t:
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-") and "=" in t:
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if t.startswith("-") and t != "-" and len(t) > 1:
            if _is_dynamic(t):
                return None
            # Attached single-letter value form: -uUSER, -gGRP, -pPROMPT, ...
            # consumed inline; static already verified above.
            i += 1
            continue
        if _ASSIGN.match(t):
            if _is_dynamic(t):
                return None
            i += 1
            continue
        break
    return i


def _strip_nohup(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens) and tokens[i] == "--":
        i += 1
    return i


def _strip_stdbuf(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    want = {"-i", "-o", "-e", "--input", "--output", "--error"}
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t == "--":
            return i + 1
        if t in want:
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--input=") or t.startswith("--output=") or t.startswith("--error="):
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if (t.startswith("-i") or t.startswith("-o") or t.startswith("-e")) and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if t.startswith("-") and t != "-":
            if _is_dynamic(t):
                return None
            i += 1
            continue
        break
    return i


def _strip_command(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens) and tokens[i] in ("-p", "-v", "-V", "--"):
        i += 1
    return i


def _strip_time(tokens: list[str], pos: int) -> int | None:
    i = pos + 1
    while i < len(tokens):
        t = tokens[i]
        if t in _SEPARATORS or t in ("<", ">"):
            break
        if t in ("-f", "--format", "-o", "--output"):
            if not _consume_value(tokens, i + 1):
                return None
            i += 2
            continue
        if t.startswith("--format=") or t.startswith("--output="):
            _, _, val = t.partition("=")
            if _is_dynamic(val):
                return None
            i += 1
            continue
        if (t.startswith("-f") or t.startswith("-o")) and len(t) > 2:
            if _is_dynamic(t[2:]):
                return None
            i += 1
            continue
        if t == "--":
            return i + 1
        if t.startswith("-") and t != "-":
            if _is_dynamic(t):
                return None
            i += 1
            continue
        break
    return i
