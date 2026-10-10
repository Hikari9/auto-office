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


def atomic_write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", mode=mode)


# Process liveness is tri-state. "Could not tell" is never "dead": a claim is
# reclaimed, and work run again, only on proof that its owner is gone.
ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def pid_state(pid: int | None) -> str:
    """ALIVE, DEAD (no such process) or UNKNOWN (the probe itself failed)."""
    if not pid or pid <= 0:
        return DEAD
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return DEAD
        return ALIVE if exc.errno == errno.EPERM else UNKNOWN
    return ALIVE


def process_start(pid: int | None) -> str | None:
    """The process's start time as `ps` reports it, or None. With the pid it
    names one process: a pid reused by another process has another start.
    None means only that it could not be read (no such process, or `ps`
    failed or is not allowed here), never which of those."""
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


def claim_liveness(pid: int | None, claimed_by: str | None) -> tuple[str, str]:
    """(ALIVE | DEAD | UNKNOWN, why) for a job claimant known only by pid and
    `claimed_by`. A claim with no recorded start time (an older Office) is
    alive whenever the pid is, so nothing is reaped on a guess. A start time
    that cannot be read now is UNKNOWN, not a mismatch: `ps` can fail or be
    denied (an agent sandbox) while the claimant runs on (#403, #404)."""
    state = pid_state(pid)
    if state == DEAD:
        return DEAD, f"pid {pid} no longer exists"
    if state == UNKNOWN:
        return UNKNOWN, f"pid {pid} could not be probed"
    start = _claim_start(claimed_by)
    if start is None:
        return ALIVE, f"pid {pid} is alive (the claim records no start time)"
    now = process_start(pid)
    if now is None:
        return UNKNOWN, f"pid {pid} is alive but its start time could not be read"
    if now == start:
        return ALIVE, f"pid {pid} is the claimant"
    return DEAD, f"pid {pid} is now another process (started {now}, the claimant started {start})"


def claim_alive(pid: int | None, claimed_by: str | None) -> bool:
    """Whether a job claimant may still be running: anything but proof of
    death counts (see `claim_liveness`)."""
    return claim_liveness(pid, claimed_by)[0] != DEAD


def process_is(pid: int | None, start: str | None) -> bool:
    """Whether `pid` is still the process that had start time `start`. A
    missing start time proves nothing, so it is never a match."""
    return bool(start) and pid_alive(pid) and process_start(pid) == start


def claim_signalable(pid: int | None, claimed_by: str | None) -> bool:
    """Whether Office may signal a job claimant: only when the start time
    proves the pid is still the process that claimed the job."""
    return process_is(pid, _claim_start(claimed_by))


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)
