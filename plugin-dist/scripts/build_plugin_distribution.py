"""Build or check the isolated 16-skill plugin from canonical repository resources.

The root source tree intentionally keeps retired skills for pinned Office runs.
Claude and portable OpenAI plugins auto-discover skills/ regardless of extra
manifest paths, so their marketplace source must point at this curated copy.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = "plugin-dist"


def file_map(root: Path) -> dict[str, bytes]:
    """Expected distributed paths -> canonical bytes (no separate authored copies)."""
    cfg = json.loads((root / "plugin-distribution.json").read_text(encoding="utf-8"))
    visible = cfg["public_skills"]
    assert len(visible) == len(set(visible)) == 16
    assert set(visible).isdisjoint(cfg["retired_skills"])
    result: dict[str, bytes] = {}

    def include(path: Path, destination: str | None = None) -> None:
        if not path.is_file() or any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            return
        if path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            return
        key = destination or path.relative_to(root).as_posix()
        result[key] = path.read_bytes()

    for name in visible:
        if name == "auto-office":
            include(root / "SKILL.md", "skills/auto-office/SKILL.md")
            result["skills/auto-office/agents/openai.yaml"] = (
                b'interface:\n  display_name: "auto-office"\n'
                b'  short_description: "Original full Auto Office orchestrator; explicit invocation only"\n'
            )
            continue
        skill = root / "skills" / name
        if not (skill / "SKILL.md").is_file():
            raise ValueError(f"missing canonical skill: {name}")
        for path in skill.rglob("*"):
            include(path)

    for name in cfg["support_files"]:
        path = root / name
        if not path.is_file():
            raise ValueError(f"missing support file: {name}")
        include(path)
    for name in cfg["support_roots"]:
        directory = root / name
        if not directory.is_dir():
            raise ValueError(f"missing support directory: {name}")
        for path in directory.rglob("*"):
            include(path)

    return result


def check(root: Path = ROOT) -> list[str]:
    expected = file_map(root)
    dist = root / DIST
    actual = {
        p.relative_to(dist).as_posix(): p.read_bytes()
        for p in dist.rglob("*") if p.is_file()
    } if dist.is_dir() else {}
    errors = [f"plugin-dist: missing {p}" for p in sorted(expected.keys() - actual.keys())]
    errors += [f"plugin-dist: unexpected {p}" for p in sorted(actual.keys() - expected.keys())]
    errors += [f"plugin-dist: stale copy {p}" for p in sorted(expected.keys() & actual.keys())
               if actual[p] != expected[p]]
    skills = sorted(p.removeprefix("skills/").removesuffix("/SKILL.md")
                    for p in actual if p.startswith("skills/") and p.endswith("/SKILL.md")
                    and p.count("/") == 2)
    cfg = json.loads((root / "plugin-distribution.json").read_text(encoding="utf-8"))
    if skills != sorted(cfg["public_skills"]):
        errors.append(f"plugin-dist: visible set mismatch {skills}")
    for name in cfg["retired_skills"]:
        if not (root / "skills" / name / "SKILL.md").is_file():
            errors.append(f"canonical retired resource missing: skills/{name}/SKILL.md")
    claude = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    codex = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    if claude["plugins"][0].get("source") != "./plugin-dist":
        errors.append("Claude marketplace must install ./plugin-dist")
    if codex["plugins"][0].get("source", {}).get("path") != "./plugin-dist":
        errors.append("Codex marketplace must install ./plugin-dist")
    return errors


def sync(root: Path = ROOT) -> int:
    expected = file_map(root)
    dist = root / DIST
    # Only the generated distribution is replaceable. Canonical source and
    # historical internal runtime resources are never deleted or moved.
    if dist.exists():
        shutil.rmtree(dist)
    for path, content in expected.items():
        target = dist / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return len(expected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync", action="store_true")
    group.add_argument("--check", action="store_true")
    options = parser.parse_args()
    if options.sync:
        count = sync()
        print(f"Synced {count} canonical files to {DIST}/ (16 public skills)")
    else:
        failures = check()
        for failure in failures:
            print("FAIL:", failure)
        if not failures:
            print("PASS: exactly 16 plugin-visible skills; source copies byte-for-byte; retired skills preserved")
        raise SystemExit(bool(failures))
