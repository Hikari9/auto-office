#!/usr/bin/env python3
"""Fail-closed Auto Office mutation gate for PreToolUse hooks."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


HOOK_NAME = "auto-office-approval-gate"
ALLOWED_PHASES = {"approved", "executing", "reviewed", "closed"}
BLOCKED_PHASES = {"intake", "planned"}
DIRECT_MUTATING_TOOLS = {"edit", "write", "notebookedit"}
SHELL_OPERATOR_TOKENS = {";", "&", "&&", "|", "||", "(", ")", "<", ">"}
READ_ONLY_COMMANDS = {
    "awk",
    "basename",
    "cat",
    "cd",
    "comm",
    "cut",
    "diff",
    "dirname",
    "du",
    "file",
    "find",
    "grep",
    "head",
    "jq",
    "less",
    "ls",
    "more",
    "md5",
    "md5sum",
    "node",
    "pgrep",
    "ps",
    "pwd",
    "rg",
    "sed",
    "sort",
    "stat",
    "tail",
    "tr",
    "true",
    "tty",
    "uniq",
    "wc",
    "which",
    "whoami",
}
READ_ONLY_GIT_COMMANDS = {
    "branch",
    "config",
    "diff",
    "grep",
    "log",
    "ls-files",
    "ls-tree",
    "remote",
    "rev-parse",
    "show",
    "stash",
    "status",
}
MUTATING_COMMANDS = {
    "apply_patch",
    "chmod",
    "chown",
    "cp",
    "dd",
    "install",
    "ln",
    "mkdir",
    "mktemp",
    "mv",
    "patch",
    "rm",
    "rmdir",
    "shred",
    "tee",
    "touch",
    "truncate",
}
MUTATING_GIT_COMMANDS = {
    "add",
    "am",
    "apply",
    "checkout",
    "clean",
    "commit",
    "config",
    "fetch",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "rm",
    "stash",
    "switch",
    "tag",
}


def _repo_root() -> Path | None:
    try:
        current = Path.cwd().resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _office_dir() -> Path:
    value = os.environ.get("OFFICE_STATE_DIR")
    repo_root = _repo_root()
    path = Path(value).expanduser() if value else (repo_root or Path.cwd()) / ".office"
    if not path.is_absolute():
        path = (repo_root or Path.cwd()) / path
    return path.resolve()


def _block(reason: str, message: str, **fields: Any) -> int:
    payload = {
        "hook": HOOK_NAME,
        "decision": "block",
        "permissionDecision": "deny",
        "reason": reason,
        "message": message,
        **fields,
    }
    payload["hookSpecificOutput"] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": message,
    }
    print(json.dumps(payload, separators=(",", ":")))
    return 2


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input", payload.get("toolInput"))
    if isinstance(value, dict):
        return value
    tool = payload.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("input"), dict):
        return tool["input"]
    return {}


def _tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip().lower()
    tool = payload.get("tool")
    if isinstance(tool, str):
        return tool.strip().lower()
    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
        return tool["name"].strip().lower()
    return ""


def _command(payload: dict[str, Any]) -> str:
    input_data = _tool_input(payload)
    value = input_data.get("command")
    if value is None:
        value = input_data.get("cmd")
    if value is None:
        value = payload.get("command", payload.get("cmd"))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return ""


def _shell_tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _approval_command(command: str) -> bool:
    """Return true only for a shell-simple office_runtime approve-plan call."""
    if not command.strip() or any(character in command for character in "\r\n`\x00"):
        return False
    # Command and process substitutions execute nested shell code even when
    # they occur in a double-quoted argument. A conservative rejection keeps
    # the exemption limited to a plain approval invocation.
    if "$((" in command or "$(" in command or "${" in command:
        return False
    tokens = _shell_tokens(command)
    if not tokens or any(
        token in SHELL_OPERATOR_TOKENS or set(token) <= set(";&|()<>")
        for token in tokens
    ):
        return False
    tokens = _strip_prefix(tokens)
    if not tokens:
        return False

    first = Path(tokens[0]).name
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", first):
        if len(tokens) < 3:
            return False
        script, args = tokens[1], tokens[2:]
    elif first == "office_runtime.py":
        script, args = tokens[0], tokens[1:]
    else:
        return False
    return Path(script).name == "office_runtime.py" and bool(args) and args[0] == "approve-plan"


def _has_output_redirection(command: str) -> bool:
    for match in re.finditer(r"(?<![<>=])(?:\d+)?>>?\s*([^\s;&|]+)", command):
        if match.group(1) not in {"/dev/null", "/dev/stderr", "/dev/stdout"}:
            return True
    return bool(re.search(r"(?:^|[\s;&|])(?:\d+)?&>\s*([^\s;&|]+)", command))


def _tokens(segment: str) -> list[str] | None:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return None


def _strip_prefix(tokens: list[str]) -> list[str]:
    while tokens:
        if tokens[0] in {"sudo", "command", "exec", "builtin", "rtk"}:
            tokens = tokens[1:]
            continue
        if tokens[0] == "env":
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or "=" in tokens[0]):
                tokens = tokens[1:]
            continue
        if "=" in tokens[0] and not tokens[0].startswith(("./", "/")):
            tokens = tokens[1:]
            continue
        break
    return tokens


def _git_is_mutating(tokens: list[str]) -> bool:
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in {"-C", "--git-dir", "--work-tree", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token in READ_ONLY_GIT_COMMANDS:
            if token == "config":
                return not any(flag in {"--get", "--get-all", "--get-regexp", "--list", "-l"} for flag in rest[index + 1 :])
            if token == "remote":
                return not any(flag in {"-v", "--verbose"} for flag in rest[index + 1 :])
            if token == "branch":
                return any(flag in {"-d", "-D", "-m", "-M", "-c", "-C"} for flag in rest[index + 1 :])
            if token == "stash":
                return not any(subcommand == "list" for subcommand in rest[index + 1 :])
            return False
        return True
    return False


def _segment_is_mutating(segment: str) -> bool:
    tokens = _tokens(segment)
    if tokens is None:
        return True
    tokens = _strip_prefix(tokens)
    if not tokens:
        return False
    command = Path(tokens[0]).name
    if command in {"bash", "sh", "zsh"} and "-c" in tokens:
        index = tokens.index("-c")
        return _bash_is_mutating(" ".join(tokens[index + 1 :]).strip("'\""))
    if command in {"python", "python3", "python3.13", "perl", "ruby", "node"}:
        if command == "node" and any(token in {"--version", "-v", "-p", "--print", "--check", "-c"} for token in tokens[1:]):
            return False
        return True
    if command == "git":
        return _git_is_mutating(tokens)
    if command in {"npm", "npx", "yarn", "pnpm"}:
        if any(token in {"--version", "-v"} for token in tokens[1:]):
            return False
        subcommands = {token for token in tokens[1:] if not token.startswith("-")}
        return not subcommands.intersection({"list", "ls", "view", "info", "outdated", "version", "--version"})
    if command in {"pip", "pip3"}:
        subcommands = {token for token in tokens[1:] if not token.startswith("-")}
        return not subcommands.intersection({"show", "list", "freeze", "check", "--version"})
    if command in MUTATING_COMMANDS:
        return True
    if command in {"echo", "printf", "export", "set", "source", ".", "test", "true", "false", "sleep"}:
        return False
    if command in READ_ONLY_COMMANDS:
        if command == "sed":
            return "-i" in tokens[1:] or "--in-place" in tokens[1:]
        if command == "find":
            return any(token in {"-delete", "-exec", "-execdir"} for token in tokens[1:])
        return False
    return True


def _bash_is_mutating(command: str) -> bool:
    if not command.strip():
        return False
    if _has_output_redirection(command):
        return True
    if "$(" in command or "$((" in command or "${" in command or "`" in command:
        return True
    segments = re.split(r"&&|\|\||[;|\n]", command)
    return any(_segment_is_mutating(segment) for segment in segments)


def _is_mutating(payload: dict[str, Any]) -> bool:
    name = _tool_name(payload)
    if name in DIRECT_MUTATING_TOOLS:
        return True
    if name != "bash":
        return False
    command = _command(payload)
    if _approval_command(command):
        return False
    return _bash_is_mutating(command)


def _current_state(office_dir: Path) -> tuple[Path, dict[str, Any]] | tuple[None, None] | tuple[Path, None]:
    runs = office_dir / "runs"
    if not runs.exists():
        return None, None
    try:
        run_id = os.environ.get("OFFICE_RUN_ID")
        if run_id:
            refs = [runs / f"{run_id}.ref"]
        else:
            refs = sorted(
                (path for path in runs.iterdir() if path.is_file() and path.suffix == ".ref"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                reverse=True,
            )
    except OSError:
        return runs, None
    if not refs:
        return None, None
    pointer = refs[0]
    try:
        target_text = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return pointer, None
    if not target_text:
        return pointer, None
    try:
        target = Path(target_text).expanduser()
        if not target.is_absolute():
            target = pointer.parent / target
        state_file = target.resolve() / "state.json"
    except (OSError, TypeError, ValueError):
        return pointer, None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return state_file, None
        return state_file, state
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return state_file, None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict) or not _is_mutating(payload):
        return 0

    office_dir = _office_dir()
    state_path, state = _current_state(office_dir)
    if state_path is None:
        return 0
    if state is None:
        return _block(
            "unreadable_run_state",
            f"Auto Office mutation blocked: the run state at {state_path} is missing, malformed, or unreadable. "
            "The approval gate fails closed until the run state can be read.",
            state_file=str(state_path),
        )

    phase = state.get("phase")
    if phase in ALLOWED_PHASES:
        approval = state.get("approval")
        quote = approval.get("quote") if isinstance(approval, dict) else None
        if not isinstance(quote, str) or not quote.strip():
            return _block(
                "approval_required",
                f"Auto Office run {state.get('run_id', state_path.parent.name)} is in phase '{phase}' "
                "but has no recorded non-empty approval.quote. Mutation is blocked until the user's "
                "verbatim approval is recorded.",
                phase=phase,
                state_file=str(state_path),
            )
        return 0
    if phase in BLOCKED_PHASES:
        command = (
            "python3 scripts/office_runtime.py approve-plan "
            f"--state-dir {shlex.quote(str(state_path.parent))} "
            '--approved-by user --quote "<verbatim user words>"'
        )
        return _block(
            "approval_required",
            f"Auto Office run {state.get('run_id', state_path.parent.name)} is in phase '{phase}'. "
            f"Mutation is blocked until the user approves the plan. Run exactly: {command}",
            phase=phase,
            state_file=str(state_path),
            required_command=command,
        )
    return _block(
        "unknown_phase",
        f"Auto Office mutation blocked: run state {state_path} has unrecognized phase {phase!r}. "
        "The approval gate fails closed until the run reaches a known phase.",
        phase=phase,
        state_file=str(state_path),
    )


if __name__ == "__main__":
    sys.exit(main())
