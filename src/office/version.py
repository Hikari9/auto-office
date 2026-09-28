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


# A wheel carries these source paths under office/_resources (pyproject force-include).
_RESOURCE_PATHS = ("config", "schemas", "catalog", "adapters", "skills", "SKILL.md", "VERSION")


def install_source() -> Path | None:
    """The local directory this wheel was installed from, when there is one.

    `uv tool install <dir>` and `pipx install <dir>` record it in the wheel's
    direct_url.json. Editable installs and index installs return None."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        try:
            raw = distribution("auto-office").read_text("direct_url.json")
        except PackageNotFoundError:
            return None
    except ImportError:  # pragma: no cover - stdlib on 3.11+
        return None
    try:
        import json
        from urllib.parse import unquote, urlparse
        data = json.loads(raw or "")
    except ValueError:
        return None
    url = urlparse(data.get("url") or "")
    if url.scheme != "file" or (data.get("dir_info") or {}).get("editable"):
        return None
    src = Path(unquote(url.path))
    return src if (src / "src" / "office").is_dir() else None


def _tree_digests(root: Path, rel_to: Path) -> dict[str, str]:
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted(p for p in root.rglob("*") if p.is_file())
    else:
        files = []
    return {str(f.relative_to(rel_to)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in files if "__pycache__" not in f.parts and f.suffix != ".pyc"}


def install_drift(package_root: Path = PACKAGE_ROOT, source: Path | None = None) -> dict | None:
    """Compare an installed wheel's runtime files with its install source.

    A wheel reports its bare distribution version, so a wheel built before a
    fix landed and one built after it both say "3.1.0". Content is the only
    honest comparison. Returns None when there is nothing to compare (source
    checkout, or no local install source), else {"source", "head", "differ"}
    where "differ" lists runtime paths that are changed, missing, or extra."""
    if source is None:
        if _is_source_checkout():
            return None
        source = install_source()
    if source is None or not (source / "src" / "office").is_dir():
        return None
    res_root = package_root / "_resources"
    installed = {"src/office/" + k: v for k, v in _tree_digests(package_root, package_root).items()
                 if not k.startswith("_resources/")}
    wanted = {"src/office/" + k: v for k, v in _tree_digests(source / "src" / "office", source / "src" / "office").items()}
    for rel in _RESOURCE_PATHS:
        installed.update(_tree_digests(res_root / rel, res_root))
        wanted.update(_tree_digests(source / rel, source))
    differ = sorted(k for k in installed.keys() | wanted.keys() if installed.get(k) != wanted.get(k))
    try:
        proc = subprocess.run(["git", "-C", str(source), "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        head = proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        head = None
    return {"source": str(source), "head": head, "differ": differ}


def release_line(version: str) -> str:
    """'3.1.0+g…' -> '3.1'. Used for compatibility-window checks."""
    parts = re.split(r"[.+]", version)
    return ".".join(parts[:2])
