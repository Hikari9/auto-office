"""Build or check the isolated 16-skill plugin from canonical repository resources.

The root source tree intentionally keeps retired skills for pinned Office runs.
Claude and portable OpenAI plugins auto-discover skills/ regardless of extra
manifest paths, so their marketplace source must point at this curated copy.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DIST = "plugin-dist"
SKILL_NAME = re.compile(r"[a-z0-9-]+")


def _config_path(root: Path, entry: str) -> Path:
    """A plugin-distribution.json path must be relative and stay inside the repository."""
    rel = PurePosixPath(entry)
    if not entry or rel.is_absolute() or ".." in rel.parts or "\\" in entry:
        raise ValueError(f"plugin-distribution.json entry escapes the repository: {entry!r}")
    return root.joinpath(*rel.parts)


def _reject_symlinks(root: Path, path: Path) -> None:
    """Refuse a symlink at any depth under root, so a linked directory cannot leak outside files."""
    rel = path.relative_to(root)
    for depth in range(1, len(rel.parts) + 1):
        if root.joinpath(*rel.parts[:depth]).is_symlink():
            raise ValueError(f"symlinks are not allowed in the plugin source: {rel.as_posix()}")


def file_map(root: Path) -> dict[str, bytes]:
    """Expected distributed paths -> canonical bytes (no separate authored copies)."""
    cfg = json.loads((root / "plugin-distribution.json").read_text(encoding="utf-8"))
    visible = cfg["public_skills"]
    if len(visible) != 16 or len(set(visible)) != 16:
        raise ValueError("plugin-distribution.json must list exactly 16 unique public skills")
    if not set(visible).isdisjoint(cfg["retired_skills"]):
        raise ValueError("a public skill is also listed as retired")
    for name in visible + cfg["retired_skills"]:
        if not SKILL_NAME.fullmatch(name):
            raise ValueError(f"invalid skill name in plugin-distribution.json: {name!r}")
    result: dict[str, bytes] = {}

    def include(path: Path, destination: str | None = None) -> None:
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            return
        if path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            return
        _reject_symlinks(root, path)
        if not path.is_file():
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
        path = _config_path(root, name)
        if not path.is_file():
            raise ValueError(f"missing support file: {name}")
        include(path)
    for name in cfg["support_roots"]:
        directory = _config_path(root, name)
        if not directory.is_dir():
            raise ValueError(f"missing support directory: {name}")
        _reject_symlinks(root, directory)
        for path in directory.rglob("*"):
            include(path)

    return result


def retired_reference_errors(dist: Path, retired: list[str]) -> list[str]:
    """Distributed skill docs must not name a retired skill the plugin does not ship."""
    errors = []
    for doc in sorted((dist / "skills").rglob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for name in retired:
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
                errors.append(f"{doc.relative_to(dist).as_posix()}: references retired skill {name}")
    return errors


def canonical_source(root: Path, path: str) -> Path | None:
    """Map a generated entry to its authored source; metadata is generated."""
    if path == "skills/auto-office/SKILL.md":
        return root / "SKILL.md"
    if path == "skills/auto-office/agents/openai.yaml":
        return None
    return root / path


def check(root: Path = ROOT) -> list[str]:
    try:
        expected = file_map(root)
    except ValueError as exc:
        return [f"plugin-dist: {exc}"]
    dist = root / DIST
    actual = {
        p.relative_to(dist).as_posix(): p.read_bytes()
        for p in dist.rglob("*") if p.is_file()
    } if dist.is_dir() else {}
    errors = [f"plugin-dist: missing {p}" for p in sorted(expected.keys() - actual.keys())]
    errors += [f"plugin-dist: unexpected {p}" for p in sorted(actual.keys() - expected.keys())]
    errors += [f"plugin-dist: stale copy {p}" for p in sorted(expected.keys() & actual.keys())
               if actual[p] != expected[p]]
    for path in sorted(expected.keys() & actual.keys()):
        source = canonical_source(root, path)
        if source is not None and source.exists():
            if bool(source.stat().st_mode & stat.S_IXUSR) != bool((dist / path).stat().st_mode & stat.S_IXUSR):
                errors.append(f"plugin-dist: executable bit mismatch {path}")
    skills = sorted(p.removeprefix("skills/").removesuffix("/SKILL.md")
                    for p in actual if p.startswith("skills/") and p.endswith("/SKILL.md")
                    and p.count("/") == 2)
    cfg = json.loads((root / "plugin-distribution.json").read_text(encoding="utf-8"))
    if skills != sorted(cfg["public_skills"]):
        errors.append(f"plugin-dist: visible set mismatch {skills}")
    for name in cfg["retired_skills"]:
        if not (root / "skills" / name / "SKILL.md").is_file():
            errors.append(f"canonical retired resource missing: skills/{name}/SKILL.md")
    errors += retired_reference_errors(dist, cfg["retired_skills"])
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
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError(f"refusing to write outside {DIST}/: {path}")
        target = dist / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        source = canonical_source(root, path)
        if source is not None and source.exists():
            target.chmod(stat.S_IMODE(source.stat().st_mode))
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
