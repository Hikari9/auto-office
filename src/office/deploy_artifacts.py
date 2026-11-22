"""Durable rollback output, outside the run directory that `prune` removes.

This is evidence storage, not lifecycle authority. A deployment checkout may
only be removed once every untracked and ignored file has a verified copy.
Links are preserved as links; their targets are never read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from office import paths, portable
from office.state import Refused
from office.util import atomic_write_json, now_iso


def _root(run: dict) -> Path:
    return paths.state_home() / "deploy-artifacts" / run["id"]


def _safe_storage_path(path: Path) -> None:
    """Do not traverse a replaced checkout/archive directory or chmod its target."""
    home = paths.state_home()
    try:
        parts = path.relative_to(home).parts
    except ValueError as exc:
        raise OSError(f"path is outside the Office state home: {path}") from exc
    current = home
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise OSError(f"refusing symlinked checkout/archive path: {current}")


def checkouts(run: dict) -> list[Path]:
    root = paths.run_dir(run["id"]) / "checkouts"
    _safe_storage_path(root)
    found = sorted(root.glob("deploy-*"))
    for path in found:
        _safe_storage_path(path)
    return [p for p in found if p.is_dir()]


def _files(checkout: Path) -> list[str]:
    files = set()
    for flags in ([], ["--ignored"]):
        proc = subprocess.run(["git", "-C", str(checkout), "ls-files", "--others", "--exclude-standard",
                               "-z", *flags], capture_output=True)
        # Git can report an unreadable directory on stderr and still exit 0.
        # A partial inventory is never permission to delete its unseen files.
        if proc.returncode or proc.stderr.strip():
            raise OSError(f"cannot enumerate rollback output in {checkout}: "
                          f"{proc.stderr.decode(errors='replace').strip()[:160]}")
        files.update(os.fsdecode(name) for name in proc.stdout.split(b"\0") if name)
    return sorted(files)


def _entry(path: Path) -> dict:
    info = path.lstat()
    base = {"mode": stat.S_IMODE(info.st_mode)}
    if stat.S_ISLNK(info.st_mode):
        return {**base, "kind": "symlink", "target": os.readlink(path)}
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"cannot safely archive non-file output {path}")
    with os.fdopen(os.open(path, os.O_RDONLY | portable.O_NOFOLLOW | portable.O_BINARY), "rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    return {**base, "kind": "file", "size": info.st_size, "sha256": "sha256:" + digest}


def _sync_dir(path: Path) -> None:
    if portable.IS_WINDOWS:
        return  # a directory cannot be opened for fsync on Windows; NTFS journals the rename
    fd = os.open(path, os.O_RDONLY | portable.O_BINARY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def preserve(run: dict, checkout: Path) -> dict | None:
    """Verify all copies before atomic publication. Any failure keeps the source.

    A changing file or file inventory refuses cleanup. Private ancestor modes
    protect copied environment files even when their original mode is broad.
    """
    _safe_storage_path(checkout)
    files = _files(checkout)
    if not files:
        return None
    root = _root(run)
    _safe_storage_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root.parent, 0o700)
    os.chmod(root, 0o700)
    unique = f"{checkout.name}-{uuid.uuid4().hex[:12]}"
    stage, archive = root / f".{unique}.tmp", root / unique
    stage.mkdir(mode=0o700)
    entries = {}
    try:
        for name in files:
            rel = Path(name)
            source, dest = checkout / rel, stage / "files" / rel
            if rel.is_absolute() or ".." in rel.parts or not source.parent.resolve().is_relative_to(checkout.resolve()):
                raise OSError(f"rollback output escapes checkout: {name!r}")
            before = _entry(source)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if before["kind"] == "symlink":
                dest.symlink_to(before["target"])
            else:
                # Never follow a file changed into a link while copying.
                fd = os.open(source, os.O_RDONLY | portable.O_NOFOLLOW | portable.O_BINARY)
                with os.fdopen(fd, "rb") as inp, dest.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fchmod(out.fileno(), before["mode"])
                    os.fsync(out.fileno())
            if _entry(dest) != before or _entry(source) != before:
                raise OSError(f"rollback output changed while archiving: {name!r}")
            entries[name] = before
        if _files(checkout) != files or any(_entry(checkout / name) != entry for name, entry in entries.items()):
            raise OSError("rollback output changed while archiving; retry when deployment writers have stopped")
        for previous in receipt(run):
            if previous.get("checkout") != str(checkout) or previous.get("files") != entries:
                continue
            saved = Path(previous["archive"])
            if saved.parent != root:
                continue
            try:
                for name in entries:
                    _safe_storage_path((saved / "files" / name).parent)
                if all(_entry(saved / "files" / name) == entry for name, entry in entries.items()):
                    return previous
            except OSError:
                pass  # A missing/corrupt older archive never stands in for this copy.
        record = {"run_id": run["id"], "checkout": str(checkout), "archive": str(archive),
                  "created_at": now_iso(), "files": entries}
        atomic_write_json(stage / "manifest.json", record, mode=0o600)
        for directory, _, _ in os.walk(stage, topdown=False, followlinks=False):
            _sync_dir(Path(directory))
        os.replace(stage, archive)
        _sync_dir(root)
        _sync_dir(root.parent)
        _sync_dir(root.parent.parent)
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def receipt(run: dict) -> list[dict]:
    root = _root(run)
    _safe_storage_path(root)
    manifests = sorted(root.glob("*/manifest.json"))
    for path in manifests:
        _safe_storage_path(path)
    return [json.loads(p.read_text(encoding="utf-8")) for p in manifests]


def preserve_or_refuse(run: dict, checkout: Path) -> dict | None:
    try:
        return preserve(run, checkout)
    except (OSError, ValueError) as exc:
        detail = f"deploy checkout {checkout} kept: rollback output could not be archived ({exc})"
        raise Refused("deploy-artifacts-unpreserved", detail, preserved="the checkout and all rollback files",
                      next_step="make the archive storage writable, then retry office close; do not delete the checkout") from exc


def preserve_interrupted(run: dict) -> None:
    """Before close becomes terminal, save output from interrupted/older deploys."""
    try:
        for checkout in checkouts(run):
            preserve_or_refuse(run, checkout)
    except OSError as exc:
        raise Refused("deploy-artifacts-unpreserved", str(exc), preserved="the run and deploy checkouts",
                      next_step="restore the real checkout/archive directory, then retry; do not delete rollback files") from exc
