"""Exact Office version identity.

Every run and every runtime-owned packet carries this string, so it has to name
exactly one runtime. A released wheel reports its distribution version
("3.1.0"). A source checkout reports a PEP 440 local version that names the
commit and, when runtime files are modified, a digest of that modification
("3.1.0+g1a2b3c4d5e6f.d0badc0de"). The word "dev" is never an identity.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parents[1]

# Paths whose content changes runtime behaviour. Docs and tests do not.
RUNTIME_PATHS = ("src/office", "config", "schemas", "catalog", "adapters", "skills", "SKILL.md", "pyproject.toml")

_PEP440 = re.compile(
    r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?(\+[a-z0-9]+(\.[a-z0-9]+)*)?$"
)


def is_exact(version: str) -> bool:
    """True for a PEP 440 version string; rejects placeholders like 'dev'."""
    return bool(version) and bool(_PEP440.match(version))


def base_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version as dist_version
        try:
            if not _is_source_checkout():
                return dist_version("auto-office")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - stdlib on 3.11+
        pass
    for candidate in (SOURCE_ROOT / "VERSION", PACKAGE_ROOT / "_resources" / "VERSION"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    raise RuntimeError("Auto Office cannot determine its own version")


def _is_source_checkout() -> bool:
    return (SOURCE_ROOT / "pyproject.toml").is_file() and (SOURCE_ROOT / ".git").exists()


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(SOURCE_ROOT), *args], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _source_identity(base: str) -> str:
    head = (_git("rev-parse", "HEAD") or "").strip()
    if not head:
        return base
    status = _git("status", "--porcelain", "--", *RUNTIME_PATHS) or ""
    if not status.strip():
        tagged = (_git("tag", "--points-at", "HEAD") or "").split()
        if f"v{base}" in tagged:
            return base
        return f"{base}+g{head[:12]}"
    digest = hashlib.sha256()
    digest.update((_git("diff", "HEAD", "--binary", "--", *RUNTIME_PATHS) or "").encode())
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", *RUNTIME_PATHS) or ""
    for rel in sorted(untracked.split()):
        path = SOURCE_ROOT / rel
        if path.is_file():
            digest.update(rel.encode())
            digest.update(path.read_bytes())
    return f"{base}+g{head[:12]}.d{digest.hexdigest()[:10]}"


def _signature() -> str:
    """Cheap stat signature of the runtime files; changes whenever they do."""
    h = hashlib.sha256()
    for rel in RUNTIME_PATHS:
        root = SOURCE_ROOT / rel
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        else:
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(SOURCE_ROOT)}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()


def _cached_source_identity(base: str) -> str:
    head = (_git("rev-parse", "HEAD") or "").strip()
    sig = f"{base}:{head}:{_signature()}"
    cache = Path(os.environ.get("OFFICE_DATA_HOME") or Path.home() / ".local/share/auto-office") / "version-cache.json"
    try:
        import json
        data = json.loads(cache.read_text())
        if data.get("sig") == sig and data.get("root") == str(SOURCE_ROOT):
            return data["identity"]
    except (OSError, ValueError, KeyError):
        pass
    identity = _source_identity(base)
    try:
        import json
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"sig": sig, "root": str(SOURCE_ROOT), "identity": identity}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return identity


@functools.lru_cache(maxsize=1)
def current() -> str:
    """The exact identity of the runtime executing this process."""
    override = os.environ.get("OFFICE_VERSION_OVERRIDE")
    if override:
        if not is_exact(override):
            raise RuntimeError(f"OFFICE_VERSION_OVERRIDE={override!r} is not an exact PEP 440 version")
        return override
    base = base_version()
    identity = _cached_source_identity(base) if _is_source_checkout() else base
    if not is_exact(identity):
        raise RuntimeError(f"Auto Office version {identity!r} is not an exact PEP 440 version")
    return identity


def release_line(version: str) -> str:
    """'3.1.0+g…' -> '3.1'. Used for compatibility-window checks."""
    parts = re.split(r"[.+]", version)
    return ".".join(parts[:2])
