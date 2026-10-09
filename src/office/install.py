"""office install / uninstall: idempotent harness integration.

An entry is Office-managed iff its command contains `--office-managed`; that
single rule drives install, doctor and uninstall. Every modified config is
backed up first. Legacy install_hooks.sh entries are reported, and removed only
with --migrate-legacy-hooks, because some of them carry user safety policy.
"""
from __future__ import annotations

import copy
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
# Opt-in (office install --shell-guard): one more PreToolUse entry, on Bash only,
# that makes `sed -i` portable on macOS. Claude only: its updatedInput contract is verified.
SHELL_GUARD = {"claude": ("shell.pre", "PreToolUse", "Bash")}


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
    if portable == "shell.pre":
        return {"matcher": SHELL_GUARD[harness][2], "hooks": [hook]}
    return {"hooks": [hook]}


def _is_managed(entry: dict, portable: str | None = None) -> bool:
    """Office-managed; with `portable`, only the entry for that portable event."""
    for h in entry.get("hooks") or []:
        cmd = h.get("command") or ""
        if MARKER in cmd and (portable is None or f" hook {portable} " in cmd):
            return True
    return False


def _is_legacy(entry: dict) -> bool:
    cmds = " ".join(h.get("command") or "" for h in (entry.get("hooks") or []))
    return "scripts/hooks/" in cmds and ("office" in cmds or "auto-office" in cmds)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


# Claude only: the reviewer read rules office install keeps in permissions.allow.
READ_RULES = "read-rules"


def install_items(harness: str) -> list[str]:
    """What `office install --only <harness> --item <id>` can apply one at a time."""
    return [*EVENTS.get(harness, {}), *([READ_RULES] if harness == "claude" else [])]


def integration_status(harness: str) -> dict:
    """The Office-managed integration of one harness, read without writing:
    `installed` (every item current), `missing` (none present), `partial` or
    `stale` (some missing, or present with an old command), `unsupported` (no
    verified hook mapping), `harness-missing` (its config directory is absent)
    or `unreadable`. Each item names the exact change install would make."""
    out: dict = {"harness": harness, "items": [], "legacy_entries": 0}
    if harness not in EVENTS:
        why = UNVERIFIED.get(harness) or f"Office has no verified hook mapping for {harness}"
        return {**out, "state": "unsupported", "reason": why}
    path = CONFIG[harness].expanduser()
    out["path"] = str(path)
    if not path.parent.exists():
        return {**out, "state": "harness-missing", "reason": f"{path.parent} does not exist"}
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {**out, "state": "unreadable", "reason": f"{path} is not valid JSON; office install leaves it untouched"}
    hooks = data.get("hooks") or {}
    command = office_command()
    for portable, native in EVENTS[harness].items():
        entries = hooks.get(native) or []
        out["legacy_entries"] += sum(1 for e in entries if _is_legacy(e))
        managed = [e for e in entries if _is_managed(e, portable)]
        want = _entry(harness, portable, command)
        state = "current" if managed == [want] else "missing" if not managed else "stale"
        out["items"].append({"id": portable, "state": state, "kind": "hook", "native_event": native,
                             "change": f"{'add' if state == 'missing' else 'replace' if state == 'stale' else 'keep'} "
                                       f"{native} hook in {path}: {want['hooks'][0]['command']}"
                                       + (f" (matcher {want['matcher']})" if want.get("matcher") else "")})
    if harness == "claude":
        added, removed, _ = read_scope.sync(copy.deepcopy(data), read_scope.load_ledger())
        state = "current" if not (added or removed) else "missing" if len(added) == len(read_scope.allow_rules()) else "stale"
        out["items"].append({"id": READ_RULES, "state": state, "kind": "permissions",
                             "change": (f"keep reviewer read rules in {path} permissions.allow" if state == "current" else
                                        f"in {path} permissions.allow: add {len(added)} read-only rule(s) for Office state "
                                        f"and guidance, remove {len(removed)} rule(s) Office no longer needs"),
                             "rules": added})
    states = {i["state"] for i in out["items"]}
    out["state"] = ("installed" if states == {"current"} else "missing" if states == {"missing"}
                    else "partial" if "missing" in states else "stale")
    return out


def install(only: list[str] | None = None, dry_run: bool = False, migrate_legacy: bool = False,
            shell_guard: bool | None = None, items: list[str] | None = None, hooks: bool = True) -> Result:
    """`shell_guard` True adds the Bash guard, False removes it, None keeps
    whatever is installed (refreshing its command). `items` limits the write to
    those `install_items(harness)` ids (onboarding's "review individually"); other
    entries are left exactly as they are. `hooks` False registers the runtime
    and writes no harness config at all."""
    res = Result()
    if items is not None:
        known = {i for h in EVENTS for i in install_items(h)}
        unknown = sorted(set(items) - known)
        if unknown:
            from office.state import OfficeError
            raise OfficeError("usage", f"unknown install item(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})",
                              exit_code=2)
    if not dry_run:
        entry = frontdoor.register_current()
        res.add(f"runtime {entry['office_version']} registered")
        if not shutil.which("office"):
            res.add(f"launcher written: {_write_shim()} (add its directory to PATH, or uv tool install auto-office)")
        retained = legacy.retained_runtime(runtime_default.LEGACY_V3_FINAL)
        res.add("retained Auto Office 3.0 runtime for pinned runs and rollback: " + ("ok" if retained else "unavailable"))
    if not hooks:
        res.add("hooks: none written (--no-hooks); /auto-office onboarding offers the current harness's integration")
        res.next = "office doctor"
        return res
    command = office_command()
    wanted = set(items) if items is not None else None
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
        had_hooks = "hooks" in data
        hooks_cfg = data.setdefault("hooks", {})
        changed = False
        legacy_found = 0
        applied = []
        for portable, native in events.items():
            if wanted is not None and portable not in wanted:
                continue
            applied.append(native)
            entries = hooks_cfg.setdefault(native, [])
            legacy_found += sum(1 for e in entries if _is_legacy(e))
            if migrate_legacy:
                kept = [e for e in entries if not _is_legacy(e)]
                if len(kept) != len(entries):
                    entries[:] = kept
                    changed = True
            want = _entry(harness, portable, command)
            managed = [e for e in entries if _is_managed(e, portable)]
            if managed == [want]:
                continue
            entries[:] = [e for e in entries if not _is_managed(e, portable)] + [want]
            changed = True
        guard_line = None
        if harness in SHELL_GUARD and (wanted is None or shell_guard is not None):
            portable, native, _ = SHELL_GUARD[harness]
            entries = hooks_cfg.setdefault(native, [])
            present = [e for e in entries if _is_managed(e, portable)]
            keep = bool(present) if shell_guard is None else shell_guard
            want = [_entry(harness, portable, command)] if keep else []
            if present != want:
                entries[:] = [e for e in entries if not _is_managed(e, portable)] + want
                changed = True
            guard_line = f"{harness}: shell guard (sed -i on macOS) " + ("on" if keep else "off (office install --shell-guard)")
        rules_added = rules_removed = 0
        owned: list[str] = []
        if harness == "claude" and (wanted is None or READ_RULES in wanted):
            added, removed, owned = read_scope.sync(data, read_scope.load_ledger())
            rules_added, rules_removed = len(added), len(removed)
            changed = changed or bool(added or removed)
        if not hooks_cfg and not had_hooks:
            del data["hooks"]  # an --item write that touched no hook adds no empty block
        if changed and not dry_run:
            backup = _backup(path)
            path.write_text(json.dumps(data, indent=2) + "\n")
            if harness == "claude" and (wanted is None or READ_RULES in wanted):
                read_scope.save_ledger(owned)
            res.add(f"{harness}: " + (f"hooks installed ({', '.join(applied)})" if applied else "settings updated")
                    + (f"; backup {backup.name}" if backup else ""))
        else:
            res.add(f"{harness}: " + ("would install hooks" if changed else "hooks already current"))
        if guard_line:
            res.add(guard_line)
        if harness == "claude" and (wanted is None or READ_RULES in wanted):
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
