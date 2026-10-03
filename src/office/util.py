"""Small shared helpers: time, hashing, atomic files, process liveness."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_bytes(canonical_bytes(obj))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def new_run_id() -> str:
    # 8 hex characters is what agents and people read; the full id stays unique.
    return secrets.token_hex(4) + "-" + secrets.token_hex(12)


def short(run_id: str) -> str:
    return run_id.split("-", 1)[0][:8]


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def process_start(pid: int | None) -> str | None:
    """The process's start time as `ps` reports it, or None. With the pid it
    names one process: a pid reused by another process has another start."""
    if not pid or pid <= 0:
        return None
    import subprocess
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(proc.stdout.split()) or None


def claim_identity(pid: int) -> str:
    """`claimed_by` for a job claim: host, pid and the process start time."""
    return f"{os.uname().nodename}:{pid}@{process_start(pid) or ''}"


def _claim_start(claimed_by: str | None) -> str | None:
    return (claimed_by or "").partition("@")[2] or None


def claim_alive(pid: int | None, claimed_by: str | None) -> bool:
    """Whether a job claimant may still be running. A claim with no recorded
    start time (an older Office) counts as alive whenever the pid is, so
    nothing is reaped on a guess; one with a start time must match it."""
    if not pid_alive(pid):
        return False
    start = _claim_start(claimed_by)
    return start is None or process_start(pid) == start


def claim_signalable(pid: int | None, claimed_by: str | None) -> bool:
    """Whether Office may signal a job claimant: only when the start time
    proves the pid is still the process that claimed the job."""
    start = _claim_start(claimed_by)
    return start is not None and pid_alive(pid) and process_start(pid) == start


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)
