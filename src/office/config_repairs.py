"""Backed-up repairs for obsolete Office hooks and malformed Hermes hook entries."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from office import install


def _office_command(value: object) -> bool:
    return isinstance(value, str) and any(part in value for part in (
        "/office-skills/.office/hooks/", "/auto-office/scripts/hooks/", install.MARKER,
    ))


def gemini_legacy(fix: bool) -> tuple[list[str], int]:
    """Retire Office entries from the unused file, keeping other consumers intact."""
    path = Path("~/.gemini/config/hooks.json").expanduser()
    if not path.exists():
        return [], 0
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ["legacy Gemini hooks: unreadable JSON; left untouched"], 1
    if not isinstance(data, dict):
        return ["legacy Gemini hooks: expected an object; left untouched"], 1
    legacy_events = {"SessionStart", "SessionEnd", "Stop", "PreToolUse", "PostToolUse",
                     "UserPromptSubmit", "BeforeAgent", "AfterAgent", "BeforeTool", "AfterTool"}
    obsolete = [key for key, value in data.items() if key in legacy_events and _office_command(value)]
    # Old office-skills installers also left an inert namespace with empty hooks.
    namespace = data.get("office-skills")
    if isinstance(namespace, dict) and namespace and all(
        isinstance(entries, list) and all(isinstance(e, dict) and e == {"hooks": []} for e in entries)
        for entries in namespace.values()
    ):
        obsolete.append("office-skills")
    if not obsolete:
        return ["legacy Gemini hooks: no obsolete Office entries (other settings kept)"], 0
    if not fix:
        return ["known defect: obsolete Office entries in ~/.gemini/config/hooks.json are not read by Gemini; "
                "office doctor --fix retires them with a backup"], 1
    backup = install._backup(path)
    for key in obsolete:
        del data[key]
    path.write_text(json.dumps(data, indent=2) + "\n")
    return [f"legacy Gemini hooks: retired {len(obsolete)} obsolete Office entries; backup {backup}"], 0


def hermes_hooks(fix: bool) -> tuple[list[str], int]:
    """Replace scalar commands in-place; preserve comments and unrelated YAML bytes."""
    profiles = Path("~/.hermes/profiles").expanduser()
    configs = [Path("~/.hermes/config.yaml").expanduser(), *sorted(profiles.glob("*/config.yaml"))]
    lines, problems = [], 0
    for path in configs:
        if not path.exists():
            continue
        label = f"{path.parent.name}/config.yaml"
        try:
            text = path.read_text()
            data = yaml.safe_load(text)
            root = yaml.compose(text)
        except (OSError, yaml.YAMLError):
            problems += 1
            lines.append(f"Hermes {label}: unreadable YAML; left untouched")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
            continue
        edits = []
        for key, value in root.value:
            if key.value != "hooks" or not isinstance(value, yaml.MappingNode):
                continue
            for event, command in value.value:
                if event.value in ("output_spill", "outbound"):
                    continue  # Hermes reserves these for non-event configuration.
                if not isinstance(data["hooks"].get(event.value), str):
                    continue
                # Anchored/aliased scalars can live outside hooks. Do not edit shared nodes.
                raw = text[command.start_mark.index:command.end_mark.index]
                shared_hooks = text[value.start_mark.index:value.end_mark.index].startswith("&") or (
                    value.start_mark.index < key.end_mark.index
                )
                if shared_hooks or not isinstance(command, yaml.ScalarNode) or raw.startswith("&") or not (
                    value.start_mark.index <= command.start_mark.index < value.end_mark.index
                ) or not data["hooks"][event.value].strip():
                    problems += 1
                    lines.append(f"Hermes {label} hooks.{event.value}: shared or empty scalar; left untouched")
                    continue
                if fix:
                    replacement = json.dumps([{"command": data["hooks"][event.value]}], ensure_ascii=False)
                    # Block scalar spans include the newline before the next key.
                    if raw.endswith("\n"):
                        replacement += "\n"
                    edits.append((command.start_mark.index, command.end_mark.index, replacement))
                else:
                    problems += 1
                    lines.append(f"known defect: {label} hooks.{event.value} is a bare string; "
                                 "Hermes needs a list of dicts (office doctor --fix)")
        if edits:
            for start, end, replacement in sorted(edits, reverse=True):
                text = text[:start] + replacement + text[end:]
            # Validate the complete result before backing up or writing it.
            yaml.safe_load(text)
            backup = install._backup(path)
            path.write_text(text)
            lines.append(f"Hermes {label}: repaired {len(edits)} scalar hook(s); backup {backup}; "
                         "Hermes retains its hook approval requirement")
    return lines, problems
