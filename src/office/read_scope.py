"""What a reviewer may read outside its checkout: Office state and global guidance.

One definition feeds two consumers:
- `add_dirs()` is appended to reviewer and vision launches for harnesses that
  need a flag to read a directory (claude and agy `--add-dir`, gemini
  `--include-directories`).
- `allow_rules()` are the `permissions.allow` entries `office install` writes to
  ~/.claude/settings.json. context-mode's ctx_execute_file refuses paths outside
  the project root unless a `Read(<glob>)` allow rule covers them, and no launch
  flag reaches it.

Rules are written in both absolute spellings because the two readers differ:
Claude Code takes `//abs` as absolute (`/abs` is project-relative), while
context-mode matches the glob literally against the absolute path.

Rules Office adds are recorded in a ledger so uninstall and re-install remove
exactly those, never a rule the user wrote.
"""
from __future__ import annotations

import json
from pathlib import Path

from office import paths

# Relative to the home directory. Directories grant `<dir>/**`, files grant the
# path or glob as written. Credentials (~/.claude/.credentials.json, ~/.codex/auth.json)
# are deliberately outside these.
GUIDANCE_DIRS = (".claude/rules", ".claude/skills")
GUIDANCE_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", ".claude/*.md", ".codex/AGENTS.md")

# Profile kinds that read the operator's checkout and state but never change them.
READER_KINDS = ("reviewer", "vision")


def state_dirs() -> list[Path]:
    return [paths.state_home(), paths.data_home()]


def guidance_dirs() -> list[Path]:
    home = Path.home()
    return [home / d for d in GUIDANCE_DIRS]


def guidance_files() -> list[Path]:
    home = Path.home()
    return [home / f for f in GUIDANCE_FILES]


def add_dirs() -> list[Path]:
    """Existing directories a reviewer's harness must be told it may read."""
    seen: list[Path] = []
    for d in state_dirs() + guidance_dirs():
        if d.is_dir() and d not in seen:
            seen.append(d)
    return seen


def with_read_dirs(kind: str, include_dirs: list[Path] | None) -> list[Path]:
    """`include_dirs` plus the shared read roots, for reviewer kinds only. A
    worker's argv is never widened."""
    base = list(include_dirs or [])
    if kind not in READER_KINDS:
        return base
    return base + [d for d in add_dirs() if d not in base]


def write_denials(cwd: Path, output: Path | None) -> list[str]:
    """Claude `Edit(<abs>/**)` deny rules (they also block the Write tool) for
    what a reviewer must not change: the checkout it runs in, Office's worktrees
    and its data directory. They cannot carve the dispatch dir out of the state
    root (deny beats allow), so claude may still write elsewhere under the
    state root; that gap is named in the docs."""
    out_dir = Path(output).parent if output else None
    targets = [paths.data_home(), paths.worktrees_dir()]
    if out_dir is None or Path(cwd) != out_dir:
        targets.insert(0, Path(cwd))
    return [f"Edit(/{t}/**)" for t in dict.fromkeys(targets)]


def allow_rules() -> list[str]:
    rules: list[str] = []
    targets = [f"{d}/**" for d in state_dirs() + guidance_dirs()] + [str(f) for f in guidance_files()]
    for target in targets:
        for rule in (f"Read({target})", f"Read(/{target})"):
            if rule not in rules:
                rules.append(rule)
    return rules


# ---------------------------------------------------------------- settings file

def ledger_path() -> Path:
    return paths.data_home() / "read-rules.json"


def load_ledger() -> list[str]:
    try:
        data = json.loads(ledger_path().read_text())
    except (OSError, ValueError):
        return []
    rules = data.get("claude") if isinstance(data, dict) else None
    return [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []


def save_ledger(rules: list[str]) -> None:
    path = ledger_path()
    if rules:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claude": rules}, indent=2) + "\n")
    elif path.exists():
        path.unlink()


def _allow_list(data: dict, create: bool) -> list | None:
    perms = data.get("permissions")
    if perms is None and create:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        return None
    allow = perms.get("allow")
    if allow is None and create:
        allow = perms["allow"] = []
    return allow if isinstance(allow, list) else None


def sync(data: dict, owned: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Bring `data`'s permissions.allow to the wanted rules, in place. Returns
    (added, removed, owned): `owned` is the new ledger. A wanted rule the user
    already has stays theirs; an owned rule no longer wanted is removed. Settings
    whose permissions are not a dict/list are left untouched."""
    allow = _allow_list(data, create=True)
    if allow is None:
        return [], [], owned
    want = allow_rules()
    removed = [r for r in owned if r not in want and r in allow]
    allow[:] = [r for r in allow if r not in removed]
    added = [r for r in want if r not in allow]
    allow.extend(added)
    return added, removed, [r for r in want if r in owned or r in added]


def strip(data: dict, owned: list[str]) -> int:
    """Remove the owned rules from `data`, dropping containers Office emptied."""
    allow = _allow_list(data, create=False)
    if allow is None:
        return 0
    kept = [r for r in allow if r not in owned]
    removed = len(allow) - len(kept)
    if removed:
        allow[:] = kept
        perms = data["permissions"]
        if not allow:
            del perms["allow"]
        if not perms:
            del data["permissions"]
    return removed


def missing(data: dict) -> list[str]:
    allow = _allow_list(data, create=False) or []
    return [r for r in allow_rules() if r not in allow]
