#!/usr/bin/env python3
"""Auto Office mutation check for PreToolUse hooks — defence in depth, not a gate.

This raises the cost of mutating below approval; it does not prevent it, and by
decision it is not meant to. The office states the rule in the brief and trusts
the executor to comply, correcting course when it does not — see
references/why-trust-not-enforcement.md, which also records that the Bash
classifier below is machinery that document says not to build.

Do not extend the classifier to cover a newly found case. The surface is
unbounded and chasing it was already tried for five review rounds.
"""

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
READ_ONLY_TOOLS = {
    "askuserquestion",
    "enterplanmode",
    "exitplanmode",
    "glob",
    "grep",
    "ls",
    "notebookread",
    "read",
    "search",
    "todoread",
    "webfetch",
    "websearch",
}
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


def _repo_root(start: Path | None = None) -> Path | None:
    try:
        current = (start if start is not None else Path.cwd()).resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _resolve_directory(value: Any, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        return path if path.is_dir() else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _resolve_file_target(value: Any, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
        target = path.resolve()
        return target if target.parent.is_dir() else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _target_repo_root(
    payload: dict[str, Any],
) -> tuple[Path | None, str | None, Path | None, bool]:
    try:
        process_cwd = Path.cwd().resolve()
    except OSError:
        return None, "the hook process working directory is unavailable", None, False
    process_root = _repo_root(process_cwd)

    input_data = _tool_input(payload)
    cwd_present = "cwd" in payload or "cwd" in input_data
    cwd_value = payload["cwd"] if "cwd" in payload else input_data.get("cwd")
    cwd_dir = None
    cwd_root = None
    if cwd_present:
        cwd_dir = _resolve_directory(cwd_value, process_cwd)
        if cwd_dir is None:
            return (
                None,
                "the payload cwd is missing, invalid, or not an existing directory",
                process_root,
                True,
            )
        cwd_root = _repo_root(cwd_dir)

    file_path_present = False
    file_path_value = None
    for key in ("file_path", "notebook_path"):
        if key in input_data:
            file_path_present = True
            file_path_value = input_data[key]
            break
    if file_path_present:
        file_target = _resolve_file_target(file_path_value, cwd_dir or process_cwd)
        if file_target is None:
            return (
                None,
                "the payload file path is missing, invalid, or has no existing parent directory",
                cwd_root or process_root,
                True,
            )
        file_root = _repo_root(file_target)
        if file_root is None:
            return None, None, None, True
        if cwd_root is not None and cwd_root != file_root:
            return (
                None,
                "the payload cwd and file path target different repositories",
                cwd_root or process_root,
                True,
            )
        return file_root, None, file_root, True

    if cwd_root is not None:
        return cwd_root, None, cwd_root, True
    if cwd_present:
        return None, None, None, True
    return process_root, None, process_root, False


def _office_dir(repo_root: Path | None = None) -> Path:
    base = (repo_root or Path.cwd()).resolve()
    trusted = (base / ".office").resolve()
    value = os.environ.get("OFFICE_STATE_DIR")
    if repo_root is None or not value or not value.strip():
        return trusted

    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        supplied = base / supplied
    try:
        supplied = supplied.resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("OFFICE_STATE_DIR is invalid") from exc
    if supplied != trusted:
        raise ValueError(
            f"OFFICE_STATE_DIR resolves to {supplied}, but the repository pointer directory is {trusted}"
        )
    return trusted


def _trusted_runtime(repo_root: Path | None) -> Path | None:
    if repo_root is None:
        return None
    try:
        return (repo_root / "scripts" / "office_runtime.py").resolve()
    except (OSError, RuntimeError):
        return None


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


def _approval_command(
    command: str,
    state_path: Path | None,
    trusted_runtime: Path | None,
) -> bool:
    """Return true only for a shell-simple office_runtime approve-plan call."""
    if (
        state_path is None
        or trusted_runtime is None
        or not command.strip()
        or any(character in command for character in "\r\n`\x00")
    ):
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
    first = Path(tokens[0]).name
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", first):
        if len(tokens) < 3:
            return False
        script, args = tokens[1], tokens[2:]
    elif first == "office_runtime.py":
        script, args = tokens[0], tokens[1:]
    else:
        return False

    try:
        script_path = Path(script).expanduser()
        if not script_path.is_absolute():
            script_path = Path.cwd() / script_path
        if script_path.resolve() != trusted_runtime:
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    if not args or args[0] != "approve-plan":
        return False

    options = args[1:]
    values: dict[str, str] = {}
    allowed_options = {"--state-dir", "--approved-by", "--quote", "--plan-path"}
    index = 0
    while index < len(options):
        option = options[index]
        if option not in allowed_options or option in values or index + 1 >= len(options):
            return False
        value = options[index + 1]
        if not value or value.startswith("-"):
            return False
        values[option] = value
        index += 2

    if {"--state-dir", "--approved-by", "--quote"} - values.keys():
        return False
    if values["--approved-by"] != "user" or not values["--quote"].strip():
        return False

    try:
        state_dir = Path(values["--state-dir"]).expanduser()
        if not state_dir.is_absolute():
            state_dir = Path.cwd() / state_dir
        return state_dir.resolve() == state_path.parent.resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


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


def _is_mutating(
    payload: dict[str, Any],
    state_path: Path | None = None,
    trusted_runtime: Path | None = None,
) -> bool:
    name = _tool_name(payload)
    if name == "bash":
        command = _command(payload)
        # The --quote is an AUDIT RECORD, not authentication: a CLI cannot
        # authenticate a human. Actual enforcement is the human reading the transcript.
        if _approval_command(command, state_path, trusted_runtime):
            return False
        return _bash_is_mutating(command)
    return name not in READ_ONLY_TOOLS


def _current_state(
    office_dir: Path,
    repo_root: Path | None = None,
) -> tuple[Path, dict[str, Any], None] | tuple[None, None, None] | tuple[Path, None, str]:
    """Resolve the run selected by the repository's own run pointers.

    The pointer is authoritative for where the state lives.  In particular,
    the state directory created by ``office_runtime.py start`` is normally
    outside the repository, so discovery must not require the pointed-to
    directory to be beneath ``office_dir``.
    """
    runs = office_dir / "runs"
    if not runs.exists():
        return None, None, None
    try:
        run_id = os.environ.get("OFFICE_RUN_ID")
        all_refs = [
            path
            for path in sorted(runs.iterdir())
            if path.is_file() and path.suffix == ".ref"
        ]
    except OSError:
        return runs, None, "unreadable_run_state"

    def load_pointer(pointer: Path, expected_run_id: str) -> tuple[Path, dict[str, Any]] | tuple[Path, None]:
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
        except (OSError, RuntimeError, TypeError, ValueError):
            return pointer, None
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("run_id") != expected_run_id:
                return state_file, None
            state_repo_root = state.get("repo_root")
            if state_repo_root is not None:
                if not isinstance(state_repo_root, str) or not state_repo_root.strip():
                    return state_file, None
                try:
                    if repo_root is not None and Path(state_repo_root).expanduser().resolve() != repo_root.resolve():
                        return state_file, None
                except (OSError, RuntimeError, TypeError, ValueError):
                    return state_file, None
            return state_file, state
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return state_file, None

    run_id = os.environ.get("OFFICE_RUN_ID")
    if run_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
            return (runs, None, "unreadable_run_state") if all_refs else (None, None, None)
        pointer = runs / f"{run_id}.ref"
        if not pointer.is_file():
            return (runs, None, "unreadable_run_state") if all_refs else (None, None, None)
        state_path, state = load_pointer(pointer, run_id)
        if state is None:
            return state_path, None, "unreadable_run_state"
        return state_path, state, None

    if not all_refs:
        return None, None, None

    valid_states: list[tuple[Path, dict[str, Any]]] = []
    invalid_state_path: Path | None = None
    for pointer in all_refs:
        pointer_run_id = pointer.stem
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pointer_run_id):
            invalid_state_path = invalid_state_path or pointer
            continue
        state_path, state = load_pointer(pointer, pointer_run_id)
        if state is None:
            invalid_state_path = invalid_state_path or state_path
        else:
            valid_states.append((state_path, state))

    if len(valid_states) > 1:
        return runs, None, "ambiguous_run_state"
    if len(valid_states) == 1:
        return valid_states[0][0], valid_states[0][1], None
    return invalid_state_path or runs, None, "unreadable_run_state"


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return _block(
                "invalid_payload",
                "Auto Office mutation gate blocked: the hook payload is empty and cannot be interpreted.",
            )
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _block(
            "invalid_payload",
            "Auto Office mutation gate blocked: the hook payload is not valid JSON and cannot be interpreted.",
        )
    if not isinstance(payload, dict):
        return _block(
            "invalid_payload",
            "Auto Office mutation gate blocked: the hook payload must be a JSON object.",
        )

    repo_root, target_error, context_root, explicit_target = _target_repo_root(payload)
    if target_error is not None:
        try:
            context_office_dir = _office_dir(context_root)
        except ValueError as exc:
            return _block(
                "state_configuration",
                f"Auto Office mutation gate blocked: {exc}.",
            )
        context_state_path, _, _ = _current_state(context_office_dir, context_root)
        if context_state_path is not None:
            return _block(
                "unresolvable_target",
                f"Auto Office mutation gate blocked: could not resolve the target repository from the hook payload; {target_error}.",
            )
        return 0
    if explicit_target and repo_root is None:
        return 0
    if repo_root is None:
        return 0

    try:
        office_dir = _office_dir(repo_root)
    except ValueError as exc:
        return _block(
            "state_configuration",
            f"Auto Office mutation gate blocked: {exc}.",
        )
    state_path, state, state_reason = _current_state(office_dir, repo_root)
    trusted_runtime = _trusted_runtime(repo_root)
    if not _is_mutating(payload, state_path, trusted_runtime):
        return 0
    if state_path is None:
        return 0
    if state is None:
        if state_reason == "ambiguous_run_state":
            return _block(
                state_reason,
                f"Auto Office mutation blocked: multiple valid run pointers were found in {office_dir / 'runs'}. "
                "Set OFFICE_RUN_ID to select exactly one run before mutating.",
                state_file=str(office_dir / "runs"),
            )
        return _block(
            state_reason or "unreadable_run_state",
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
                "but has no recorded non-empty approval.quote. Mutation is blocked until a non-empty "
                "approval audit record is recorded.",
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
            f"Mutation is blocked until an approval audit record is recorded for the plan. Run exactly: {command}",
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
