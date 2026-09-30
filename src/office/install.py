"""office install / uninstall: idempotent harness integration.

An entry is Office-managed iff its command contains `--office-managed`; that
single rule drives install, doctor and uninstall. Every modified config is
backed up first. Legacy install_hooks.sh entries are reported, and removed only
with --migrate-legacy-hooks, because some of them carry user safety policy.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from office import frontdoor, legacy, paths, read_scope, runtime_default
from office.result import Result

MARKER = "--office-managed"

# Portable event -> native event name, per harness with a verified JSON schema.
EVENTS = {
    "claude": {"session.start": "SessionStart", "prompt.submit": "UserPromptSubmit", "tool.pre": "PreToolUse"},
    "gemini": {"session.start": "SessionStart", "prompt.submit": "BeforeAgent", "tool.pre": "BeforeTool"},
}
CONFIG = {
    "claude": Path("~/.claude/settings.json"),
    "gemini": Path("~/.gemini/settings.json"),
}
UNVERIFIED = {
    "codex": "Codex hook config location and denial path are unverified (architecture R1/§8); explicit office commands cover it",
    "agy": "agy exposes no session-start event; the skill runs office status explicitly",
    "hermes": "Hermes hook schema is list-of-dicts and profile-scoped; not written by 3.1.0",
}
WRITE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|write_file|replace"


def office_command() -> str:
    exe = shutil.which("office")
    if exe:
        return exe
    shim = paths.data_home() / "bin" / "office"
    return str(shim)


def _write_shim() -> Path:
    """A stable `office` launcher for source checkouts that were not installed
    with uv; it runs the registered current runtime."""
    shim = paths.data_home() / "bin" / "office"
    argv, env = frontdoor.current_argv()
    lines = ["#!/bin/sh"]
    for k, v in env.items():
        lines.append(f'export {k}="{v}"')
    lines.append('exec ' + " ".join(f'"{a}"' for a in argv) + ' "$@"')
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("\n".join(lines) + "\n")
    shim.chmod(0o755)
    return shim


def _entry(harness: str, portable: str, command: str) -> dict:
    hook = {"type": "command", "command": f"{command} hook {portable} --harness {harness} {MARKER}", "timeout": 5}
    if portable == "tool.pre":
        return {"matcher": WRITE_MATCHER, "hooks": [hook]}
    return {"hooks": [hook]}


def _is_managed(entry: dict) -> bool:
    return any(MARKER in (h.get("command") or "") for h in (entry.get("hooks") or []))


def _is_legacy(entry: dict) -> bool:
    cmds = " ".join(h.get("command") or "" for h in (entry.get("hooks") or []))
    return "scripts/hooks/" in cmds and ("office" in cmds or "auto-office" in cmds)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def install(only: list[str] | None = None, dry_run: bool = False, migrate_legacy: bool = False) -> Result:
    res = Result()
    if not dry_run:
        entry = frontdoor.register_current()
        res.add(f"runtime {entry['office_version']} registered")
        if not shutil.which("office"):
            res.add(f"launcher written: {_write_shim()} (add its directory to PATH, or uv tool install auto-office)")
        retained = legacy.retained_runtime(runtime_default.LEGACY_V3_FINAL)
        res.add("retained Auto Office 3.0 runtime for pinned runs and rollback: " + ("ok" if retained else "unavailable"))
    command = office_command()
    for harness, events in EVENTS.items():
        if only and harness not in only:
            continue
        path = CONFIG[harness].expanduser()
        if not path.parent.exists():
            res.add(f"{harness}: not installed, skipped")
            continue
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            res.add(f"{harness}: {path} is not valid JSON; left untouched")
            continue
        hooks = data.setdefault("hooks", {})
        changed = False
        legacy_found = 0
        for portable, native in events.items():
            entries = hooks.setdefault(native, [])
            legacy_found += sum(1 for e in entries if _is_legacy(e))
            if migrate_legacy:
                kept = [e for e in entries if not _is_legacy(e)]
                if len(kept) != len(entries):
                    entries[:] = kept
                    changed = True
            want = _entry(harness, portable, command)
            managed = [e for e in entries if _is_managed(e)]
            if managed == [want]:
                continue
            entries[:] = [e for e in entries if not _is_managed(e)] + [want]
            changed = True
        rules_added = rules_removed = 0
        owned: list[str] = []
        if harness == "claude":
            added, removed, owned = read_scope.sync(data, read_scope.load_ledger())
            rules_added, rules_removed = len(added), len(removed)
            changed = changed or bool(added or removed)
        if changed and not dry_run:
            backup = _backup(path)
            path.write_text(json.dumps(data, indent=2) + "\n")
            if harness == "claude":
                read_scope.save_ledger(owned)
            res.add(f"{harness}: hooks installed ({', '.join(events.values())})" + (f"; backup {backup.name}" if backup else ""))
        else:
            res.add(f"{harness}: " + ("would install hooks" if changed else "hooks already current"))
        if harness == "claude":
            res.add(f"{harness}: reviewer read rules "
                    + (f"{'would add' if dry_run else 'added'} {rules_added}, removed {rules_removed}"
                       if rules_added or rules_removed else "already current"))
        if legacy_found and not migrate_legacy:
            res.add(f"{harness}: {legacy_found} legacy install_hooks.sh entr{'y' if legacy_found == 1 else 'ies'} left in place "
                    "(office install --migrate-legacy-hooks removes them)")
    for harness, why in UNVERIFIED.items():
        if not only or harness in only:
            res.add(f"{harness}: no hooks written ({why})")
    res.next = "office doctor"
    return res


def uninstall(purge: bool = False) -> Result:
    res = Result()
    for harness, path in CONFIG.items():
        path = path.expanduser()
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        hooks = data.get("hooks") or {}
        removed = 0
        rules = read_scope.strip(data, read_scope.load_ledger()) if harness == "claude" else 0
        for native, entries in list(hooks.items()):
            kept = [e for e in entries if not _is_managed(e)]
            removed += len(entries) - len(kept)
            hooks[native] = kept
        if removed or rules:
            _backup(path)
            path.write_text(json.dumps(data, indent=2) + "\n")
            if harness == "claude":
                read_scope.save_ledger([])
            res.add(f"{harness}: removed {removed} Office-managed hook entr{'y' if removed == 1 else 'ies'}"
                    + (f" and {rules} reviewer read rule{'s' if rules != 1 else ''}" if rules else ""))
    res.add("run state and runs.db kept" if not purge else "purge is not automatic: remove the data directory yourself")
    return res
