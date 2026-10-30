"""OS portability layer: file locks, process groups, signals and process identity.

Office was written for POSIX. Everything that differs on native Windows lives
here so the rest of the runtime never imports `fcntl`, calls `os.killpg`, or
probes a pid with `os.kill(pid, 0)`. On Windows that last call does not probe:
it terminates the process. Use `pid_exists` instead.

POSIX behaviour is unchanged: every function delegates to the call it replaces.
"""
from __future__ import annotations

import contextlib
import errno
import os
import platform
import signal
import subprocess
import sys
from typing import IO

IS_WINDOWS = sys.platform == "win32"

# Windows has no SIGKILL: a forced kill is `taskkill /F` (see `killpg`).
SIGKILL: int = getattr(signal, "SIGKILL", 9)
SIGHUP: int | None = getattr(signal, "SIGHUP", None)

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    _k32.LockFileEx.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    _k32.UnlockFileEx.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_LOCK_VIOLATION = 33
    _LOCKFILE_FAIL_IMMEDIATELY = 1
    _LOCKFILE_EXCLUSIVE_LOCK = 2
    # Bytes far past any real EOF: Windows locks are mandatory, so locking byte 0
    # would also block other handles from reading the lock file's contents.
    _LOCK_OFFSET_HIGH = 0x7FFFFFF0

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]

    def _overlapped() -> _Overlapped:
        ov = _Overlapped()
        ov.OffsetHigh = _LOCK_OFFSET_HIGH
        return ov

    def _handle(target) -> int:
        import msvcrt
        fd = target if isinstance(target, int) else target.fileno()
        return msvcrt.get_osfhandle(fd)


def _fd(target: int | IO) -> int:
    return target if isinstance(target, int) else target.fileno()


# --- file locks (fcntl.flock replacement) ---------------------------------

def lock_exclusive(target: int | IO, *, blocking: bool = True) -> None:
    """Exclusive advisory lock on an open file or descriptor, like
    `fcntl.flock(f, LOCK_EX [| LOCK_NB])`. Non-blocking contention raises
    `BlockingIOError`, as flock does. Closing the file releases the lock."""
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(_fd(target), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    flags = _LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
    ov = _overlapped()
    if not _k32.LockFileEx(_handle(target), flags, 0, 1, 0, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        if err == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "file is locked")
        raise ctypes.WinError(err)


def unlock(target: int | IO) -> None:
    """Release a lock taken with `lock_exclusive` (`fcntl.flock(f, LOCK_UN)`)."""
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(_fd(target), fcntl.LOCK_UN)
        return
    ov = _overlapped()
    if not _k32.UnlockFileEx(_handle(target), 0, 1, 0, ctypes.byref(ov)):
        raise ctypes.WinError(ctypes.get_last_error())


# --- process groups and signals -------------------------------------------

def session_kwargs() -> dict:
    """`subprocess.Popen` kwargs that start the child as the leader of its own
    process group (POSIX: a new session), so `killpg` reaches it and everything
    it spawns without touching Office itself."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def getpgrp() -> int:
    """This process's group id; on Windows there is no inherited group, so 0 (never a pid)."""
    return 0 if IS_WINDOWS else os.getpgrp()


def getpgid(pid: int) -> int:
    """A process's group id. On Windows a group leader's id is its own pid, which
    is how `session_kwargs` creates every group Office signals."""
    if not IS_WINDOWS:
        return os.getpgid(pid)
    if not pid_exists(pid):
        raise ProcessLookupError(errno.ESRCH, "no such process")
    return pid


def _taskkill(pid: int, *, force: bool) -> None:
    argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    proc = subprocess.run(argv, capture_output=True, timeout=30)
    if proc.returncode == 128:  # "process not found"
        raise ProcessLookupError(errno.ESRCH, "no such process")
    if proc.returncode != 0:
        raise PermissionError(errno.EPERM, f"taskkill exited {proc.returncode}")


def kill(pid: int, sig: int) -> None:
    """Signal one process. Never use `os.kill(pid, 0)` for a probe: see `pid_exists`."""
    if not IS_WINDOWS:
        os.kill(pid, sig)
        return
    if sig == 0:
        if not pid_exists(pid):
            raise ProcessLookupError(errno.ESRCH, "no such process")
        return
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError as exc:
        if not pid_exists(pid):
            raise ProcessLookupError(errno.ESRCH, "no such process") from exc
        raise


def killpg(pgid: int, sig: int) -> None:
    """Signal a process group (`os.killpg`). On Windows the group is the process
    tree below `pgid`: SIGTERM asks the group leader to stop with CTRL_BREAK
    (and, where that is refused, a polite `taskkill /T`); SIGKILL is `taskkill /T /F`.
    `sig == 0` checks that the group exists. Raises `ProcessLookupError` when it is gone."""
    if not IS_WINDOWS:
        os.killpg(pgid, sig)
        return
    if sig == 0:
        kill(pgid, 0)
        return
    if sig == signal.SIGTERM:
        if not pid_exists(pgid):
            raise ProcessLookupError(errno.ESRCH, "no such process")
        try:
            os.kill(pgid, signal.CTRL_BREAK_EVENT)
            return
        except OSError:
            pass
        _taskkill(pgid, force=False)
        return
    _taskkill(pgid, force=True)


def pid_exists(pid: int | None) -> bool | None:
    """True if the process exists, False if it does not, None when that cannot be
    told (a probe that was denied). Safe on Windows, unlike `os.kill(pid, 0)`."""
    if not pid or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            return True if exc.errno == errno.EPERM else None
        return True
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_INVALID_PARAMETER:
            return False
        return True if err == _ERROR_ACCESS_DENIED else None
    try:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def windows_process_start(pid: int) -> str | None:
    """A Windows process's creation time as a stable string (FILETIME ticks), or None."""
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))):
            return None
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        _k32.CloseHandle(handle)


def node_name() -> str:
    """The host name (`os.uname().nodename`, which Windows lacks)."""
    return platform.node()


def loadavg() -> tuple[float, float, float] | None:
    """`os.getloadavg()`, or None where the OS has no load average (Windows)."""
    with contextlib.suppress(AttributeError, OSError):
        return os.getloadavg()
    return None


# `os.open` flags that exist only on some platforms. O_BINARY matters on Windows,
# where a descriptor opened without it translates newlines and stops at Ctrl-Z.
O_BINARY: int = getattr(os, "O_BINARY", 0)
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)

# The `office` launcher Office writes for a checkout not installed with uv: a `.cmd` on Windows.
SHIM_NAME = "office.cmd" if IS_WINDOWS else "office"


def hook_executable(exe: str) -> str:
    """`exe` spelled for a hook command line that a POSIX shell, PowerShell or cmd.exe may
    run: forward slashes (bash eats backslashes), double-quoted only if it has spaces."""
    if not IS_WINDOWS:
        return exe
    exe = exe.replace("\\", "/")
    return f'"{exe}"' if " " in exe else exe
