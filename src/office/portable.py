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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

IS_WINDOWS = sys.platform == "win32"
# Explicit Office outbox launches may detach from an agent job.
AGENT_JOB_ENV = "OFFICE_WINDOWS_AGENT_JOB"

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


def lock_shared(target: int | IO, *, blocking: bool = True) -> None:
    """Shared advisory lock, like `fcntl.flock(f, LOCK_SH [| LOCK_NB])`. Any number
    of shared holders coexist; an exclusive holder excludes them. Non-blocking
    contention raises `BlockingIOError`. Release with `unlock`."""
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(_fd(target), fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
        return
    flags = 0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY
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


if IS_WINDOWS:
    class _JobBasicAccounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                    ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]

    class _JobBasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                   "OtherBytes")]

    class _JobExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    _k32.OpenJobObjectW.restype = wt.HANDLE
    _k32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
    _CREATE_SUSPENDED = 0x4
    _JOB_BASIC_ACCOUNTING = 1
    _JOB_EXTENDED_LIMIT = 9
    _JOB_KILL_ON_CLOSE = 0x2000
    _JOB_BREAKAWAY_OK = 0x800
    _JOB_QUERY_TERMINATE = 0x4 | 0x8
    _ERROR_ALREADY_EXISTS = 183

# Windows: the job object holding each group this process started with `popen_group`, by leader pid.
_JOBS: dict[int, int] = {}


class GroupCleanupError(OSError):
    """The suspended launch could not be proven stopped. Never retry past it."""


def new_group_name() -> str:
    """A unique, session-local Windows job name (never a numeric PID)."""
    import uuid
    return "Local\\AutoOffice-" + uuid.uuid4().hex


def popen_group(*args, job_name: str | None = None, on_ready=None,
                allow_breakaway: bool = False, **kwargs) -> subprocess.Popen:
    """Start a process group; on Windows prove containment before executing code.

    The child stays suspended until its kill-on-close job is configured and
    assigned. on_ready records the assigned job before the child can run.
    """
    if not IS_WINDOWS:
        return subprocess.Popen(*args, **{**kwargs, **session_kwargs()})
    flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    proc = subprocess.Popen(*args, creationflags=flags, **kwargs)
    job = None
    try:
        ctypes.set_last_error(0)
        job = _k32.CreateJobObjectW(None, job_name)
        err = ctypes.get_last_error()
        if not job or (job_name and err == _ERROR_ALREADY_EXISTS):
            raise ctypes.WinError(err)
        limit = _JobExtendedLimit()
        limit.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE | (_JOB_BREAKAWAY_OK if allow_breakaway else 0)
        if not _k32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT, ctypes.byref(limit), ctypes.sizeof(limit)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _k32.AssignProcessToJobObject(job, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if proc.pid in _JOBS:
            raise OSError(errno.EBUSY, "a previous job still owns this process id")
        if on_ready is not None:
            on_ready()
        if _ntdll.NtResumeProcess(int(proc._handle)) != 0:
            raise OSError(errno.EPERM, "the new process could not be resumed")
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GroupCleanupError(errno.EIO, "could not confirm suspended child stopped") from exc
        finally:
            if job:
                _k32.CloseHandle(job)
        raise
    _JOBS[proc.pid] = job
    return proc


def _active_job(job: int) -> int:
    info = _JobBasicAccounting()
    if not _k32.QueryInformationJobObject(job, _JOB_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
        raise PermissionError(errno.EPERM, "the process group's job could not be queried")
    return info.ActiveProcesses


def _job_active(pgid: int) -> int | None:
    job = _JOBS.get(pgid)
    return _active_job(job) if job else None


def _drain_job(job: int, timeout: float = 10.0) -> bool:
    """Terminate the exact job by handle and confirm it has no active members."""
    try:
        if not _active_job(job):
            return True
        if not _k32.TerminateJobObject(job, 1):
            return False
        deadline = time.monotonic() + timeout
        while _active_job(job):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True
    except OSError:
        return False


def finish_group(pgid: int) -> bool:
    """Drain and release an owned Windows job; unknown is not empty."""
    job = _JOBS.get(pgid)
    if not IS_WINDOWS or not job or not _drain_job(job):
        return False
    release_group(pgid)
    return True


def finish_named_group(name: str) -> bool:
    """Reopen the exact recorded job; a missing job name is not proof of exit."""
    if not IS_WINDOWS:
        return False
    job = _k32.OpenJobObjectW(_JOB_QUERY_TERMINATE, False, name)
    if not job:
        return False
    try:
        return _drain_job(job)
    finally:
        _k32.CloseHandle(job)


def release_group(pgid: int) -> None:
    """Forget a `popen_group` group once it is confirmed empty (closes its Windows job handle)."""
    job = _JOBS.pop(pgid, None)
    if job:
        _k32.CloseHandle(job)


def owns_group(pgid: int) -> bool:
    """True when this process holds `pgid`'s group by handle (a Windows `popen_group` job). The handle keeps
    naming that group after its leader exits and the pid is reused; POSIX has only the id, so False."""
    return IS_WINDOWS and pgid in _JOBS


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
        exists = pid_exists(pid)
        if exists is False:
            raise ProcessLookupError(errno.ESRCH, "no such process")
        if exists is None:
            raise PermissionError(errno.EPERM, "process liveness is unknown")
        return
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError as exc:
        if not pid_exists(pid):
            raise ProcessLookupError(errno.ESRCH, "no such process") from exc
        raise


def killpg(pgid: int, sig: int) -> None:
    """Signal a process group (`os.killpg`). On Windows the group is the process
    tree below `pgid`: SIGTERM is a polite `taskkill /T`; SIGKILL is `taskkill /T /F`.
    `sig == 0` checks that the group exists. Raises `ProcessLookupError` when it is gone.
    A group this process started with `popen_group` is its job object instead. Both stop signals address
    that exact job by handle, never a console group id that may have been reused."""
    if not IS_WINDOWS:
        os.killpg(pgid, sig)
        return
    active = _job_active(pgid)
    if active is not None:
        if active == 0:
            raise ProcessLookupError(errno.ESRCH, "no such process group")
        if sig == 0:
            return
        if not _k32.TerminateJobObject(_JOBS[pgid], 1):
            raise PermissionError(errno.EPERM, "the process group's job could not be terminated")
        return
    if sig == 0:
        kill(pgid, 0)
        return
    if sig == signal.SIGTERM:
        if not pid_exists(pgid):
            raise ProcessLookupError(errno.ESRCH, "no such process")
        # No CTRL_BREAK here: sent to an id that is not a console process-group leader, Windows delivers it to
        # every process on the console, Office included. Only a `popen_group` leader (above) is known to be one.
        # A console program usually refuses a polite taskkill; callers then escalate to SIGKILL.
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


if IS_WINDOWS:
    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessEntry32)]
    _k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessEntry32)]
    _TH32CS_SNAPPROCESS = 0x2
    _INVALID_HANDLE = wt.HANDLE(-1).value


def windows_ticks_now() -> int:
    """Now, in the units `windows_process_start` uses (100ns ticks since 1601), to compare with a start."""
    import time
    return int((time.time() + 11644473600) * 10_000_000)


def orphan_filter(gone: dict[int, int], seen: dict, started: dict) -> "callable":
    """For a tracker that keeps walking a recorded process after it exits (Windows leaves its pid as its
    children's ppid): note in `gone` when each recorded pid was first missing or reused, and return
    `follows(parent, child)`. A gone parent's child is followed only if it started before the parent was
    seen gone; a later one is the child of whatever reused the pid. POSIX reparents orphans, so there it
    always follows."""
    if not IS_WINDOWS:
        return lambda parent, child: True
    now = windows_ticks_now()
    for pid, start in seen.items():
        if started.get(pid) != start:
            gone.setdefault(pid, now)

    def follows(parent: int, child: int) -> bool:
        cutoff = gone.get(parent)
        if cutoff is None:
            return True
        try:
            return int(started.get(child) or "") < cutoff
        except ValueError:
            return False

    return follows


def windows_process_table() -> list[tuple[int, int, str, str]] | None:
    """(pid, ppid, start, image name) for every live process on Windows, or None when the snapshot fails.

    `start` is `windows_process_start`, so it matches a recorded identity. A process whose start cannot be read
    (a protected system process) is listed with start "": it can be neither a recorded root nor its descendant's
    proof. Windows keeps a dead parent's pid in `ppid`, so a child is linked to a parent only when it started no
    earlier than that parent: an older process cannot be the child of a pid that was reused after it started.
    Windows does not show another process's environment, so the image name stands in for the command."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return None
    rows: list[tuple[int, int, str, str]] = []
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    started = {pid: windows_process_start(pid) or "" for pid, _ppid, _name in rows}
    table = []
    for pid, ppid, name in rows:
        mine, theirs = started.get(pid, ""), started.get(ppid, "")
        if ppid == pid or (mine and theirs and int(theirs) > int(mine)):
            ppid = 0  # the recorded parent pid now names a younger, unrelated process
        table.append((pid, ppid, mine, name))
    return table if any(r[0] == os.getpid() for r in table) else None


if IS_WINDOWS:
    class _ProcessBasicInformation(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    _ntdll_q = ctypes.WinDLL("ntdll")
    _ntdll_q.NtQueryInformationProcess.restype = ctypes.c_long
    _ntdll_q.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                                   ctypes.POINTER(wt.ULONG)]
    _k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _PROCESS_BASIC_INFORMATION = 0
    _PROCESS_WOW64_INFORMATION = 26
    # (PEB -> ProcessParameters, ProcessParameters -> CurrentDirectory.DosPath, pointer size) per bitness.
    _PEB_LAYOUT = {64: (0x20, 0x38, 8), 32: (0x10, 0x24, 4)}


class _Unreadable(Exception):
    pass


class _Denied(Exception):
    pass


def _process_cwd(pid: int) -> str | None:
    """The current directory of a live process, read from its process parameters. None when the process is
    gone or this user is denied it (cannot open it, or a guard refuses reads of an opened one); `_Unreadable`
    when it is open but its memory cannot be read for another reason."""
    handle = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        try:
            return _read_cwd(handle, pid)
        except _Denied:
            return None
    finally:
        _k32.CloseHandle(handle)


def _read_cwd(handle, pid: int) -> str | None:
    """`RTL_USER_PROCESS_PARAMETERS.CurrentDirectory` of an opened process (its 32-bit copy for a WOW64 one)."""
    def read(address: int, size: int) -> bytes:
        buf, got = ctypes.create_string_buffer(size), ctypes.c_size_t()
        if address and _k32.ReadProcessMemory(handle, address, buf, size, ctypes.byref(got)) and got.value == size:
            return buf.raw
        if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
            raise _Denied(pid)  # guarded (anti-cheat, a protected service): not a worker Office started
        raise _Unreadable(pid)

    wow_peb = ctypes.c_void_p()
    if _ntdll_q.NtQueryInformationProcess(handle, _PROCESS_WOW64_INFORMATION, ctypes.byref(wow_peb),
                                          ctypes.sizeof(wow_peb), None) != 0:
        raise _Unreadable(pid)
    if wow_peb.value:  # a 32-bit process keeps its working directory in its 32-bit parameters
        peb, (params_at, cwd_at, ptr) = wow_peb.value, _PEB_LAYOUT[32]
    else:
        info = _ProcessBasicInformation()
        if _ntdll_q.NtQueryInformationProcess(handle, _PROCESS_BASIC_INFORMATION, ctypes.byref(info),
                                              ctypes.sizeof(info), None) != 0:
            raise _Unreadable(pid)
        peb, (params_at, cwd_at, ptr) = info.PebBaseAddress, _PEB_LAYOUT[64]
    if not peb:
        return None  # a pico (WSL1) or minimal process: no PEB and no Win32 working directory
    params =int.from_bytes(read(peb + params_at, ptr), "little")
    if not params:
        return None  # still being created: it has no working directory yet
    header = read(params + cwd_at, 2 * ptr)  # UNICODE_STRING: Length, MaximumLength, padding, Buffer
    length = int.from_bytes(header[:2], "little")
    text = int.from_bytes(header[ptr:2 * ptr], "little")
    return read(text, length).decode("utf-16-le") if length else ""


def windows_cwds() -> dict[int, str] | None:
    """{pid: current directory} for every process this user can open, or None when the list cannot be trusted
    (a 32-bit Office, a failed snapshot, or a process that opened but whose memory could not be read). A
    process this user cannot open belongs to another account or to the system: a worker Office started runs
    as this user, and so can be opened."""
    if sys.maxsize <= 2**32:
        return None
    table = windows_process_table()
    if table is None:
        return None
    found = {}
    for pid, _ppid, _start, _name in table:
        if pid in (0, 4):  # the idle and system processes have no user-mode parameters
            continue
        try:
            cwd = _process_cwd(pid)
        except _Unreadable:
            if pid_exists(pid):
                return None
            continue  # exited during the listing
        if cwd:
            found[pid] = cwd
    return found


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


def rmtree(path, ignore_errors: bool = False) -> None:
    """`shutil.rmtree` that also removes read-only files. Git writes its objects read-only, and on Windows a
    read-only file cannot be deleted, so a plain rmtree of a worktree or clone fails there (or, with
    `ignore_errors`, silently leaves it behind)."""
    import shutil
    import stat

    if not IS_WINDOWS:  # POSIX deletes read-only files already: keep plain rmtree semantics there
        shutil.rmtree(path, ignore_errors=ignore_errors)
        return

    def retry(func, target, exc):
        exc = exc[1] if isinstance(exc, tuple) else exc  # onerror passes exc_info, onexc the exception
        try:
            if not isinstance(exc, PermissionError) or os.path.islink(target):
                raise exc
            os.chmod(target, os.lstat(target).st_mode | stat.S_IWRITE)
            func(target)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


_GIT_ROOT: list = []


def _git_root() -> Path | None:
    """The Git for Windows install holding `usr/bin/bash.exe`, found from `git` on PATH, or None. Never `bash` on
    PATH: System32's bash.exe is the WSL launcher, which cannot see this filesystem's paths."""
    if not _GIT_ROOT:
        found = None
        git = shutil.which("git") if IS_WINDOWS else None
        for root in Path(git).resolve().parents if git else ():
            if (root / "usr" / "bin" / "bash.exe").is_file():
                found = root
                break
        _GIT_ROOT.append(found)
    return _GIT_ROOT[0]


def shell_run_args(command: str, env: dict | None = None) -> dict:
    """`subprocess` keyword arguments (args, shell, env) that run one shell command line. Plans and repository
    config write POSIX shell (`scripts/validate.sh --pr`, `&&`, `VAR=1 cmd`), so on Windows it runs under Git for
    Windows' bash with Git's tools first on PATH, as a Git Bash prompt (and Claude Code's Bash tool) has them; only
    without Git does it fall back to cmd.exe. Elsewhere it is `shell=True`, the system shell, as before."""
    root = _git_root() if IS_WINDOWS else None
    if root is None:
        return {"args": command, "shell": True, "env": env}
    env = dict(os.environ if env is None else env)
    tools = [str(root / "mingw64" / "bin"), str(root / "usr" / "bin")]
    shim = _python3_shim(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join([*([shim] if shim else []), *tools, env.get("PATH", "")])
    return {"args": [str(root / "usr" / "bin" / "bash.exe"), "-c", command], "shell": False, "env": env}


def _real_exe(name: str, path: str) -> str | None:
    found = shutil.which(name, path=path)
    return found if found and "windowsapps" not in found.lower() else None  # WindowsApps' python*.exe: Store stub


def _python3_shim(path: str) -> str | None:
    """A directory holding a `python3` that runs PATH's real `python`, for checks written as `python3 ...`: Windows
    installs (python.org, uv) ship no python3, and WindowsApps' python3.exe only opens the Store. None when PATH
    already has a real python3, or has no python to stand in."""
    if _real_exe("python3", path) or not (python := _real_exe("python", path)):
        return None
    import hashlib
    import shlex
    import tempfile
    shim = Path(tempfile.gettempdir()) / f"office-python3-{hashlib.sha256(python.encode()).hexdigest()[:12]}"
    text = f'#!/bin/sh\nexec {shlex.quote(Path(python).as_posix())} "$@"\n'
    script = shim / "python3"
    try:
        if not script.is_file() or script.read_text() != text:
            shim.mkdir(exist_ok=True)
            tmp = shim / f".python3.{os.getpid()}"
            tmp.write_text(text, newline="\n")
            replace(tmp, script)
    except OSError:
        return None
    return str(shim)


def shell_join(argv) -> str:
    """argv as one command line for `shell_run_args`: `shlex.join` for a POSIX shell (Git Bash on Windows), and on
    Windows without Git `list2cmdline`, the double-quoting cmd.exe and the C runtime parse (it has no single quotes)."""
    argv = [str(a) for a in argv]
    if IS_WINDOWS and _git_root() is None:
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


def replace(src, dst) -> None:
    """`os.replace`. On Windows a rename onto a file that another handle has open fails with "access denied" until
    that handle closes (Python opens files without delete sharing), so concurrent readers of state that is rewritten
    atomically would make the writer fail. Retry there for a short while; elsewhere it never fails that way."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    import time
    deadline = time.monotonic() + 5
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def execve(argv, env: dict) -> None:
    """Replace this process with `argv` (`os.execve`); never returns.

    Windows has no exec. Its `os.execve` starts a detached process and exits at once, so the caller's wait and
    exit code are lost; it joins the arguments unquoted, so a path with a space splits; and on current Windows
    runtimes it ends in an access violation. There the command runs as a child, and this process exits with
    its code without running anything further, as after an exec."""
    if not IS_WINDOWS:
        os.execve(argv[0], argv, env)
    try:
        code = subprocess.call(resolve_argv(argv, env), env=env)
    except KeyboardInterrupt:
        code = 130
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def resolve_argv(argv, env: dict | None = None) -> list:
    """`argv` with a bare program name resolved through PATH on Windows, where CreateProcess finds only `.exe`
    files by itself: an npm-installed harness (`codex`, `opencode`, `claude`) is a `.cmd` shim, which Windows runs
    only from its full path. POSIX gets `argv` unchanged; an unresolvable name is left for the launch to report."""
    argv = list(argv)
    if not IS_WINDOWS or not argv:
        return argv
    head = str(argv[0])
    if os.sep in head or "/" in head or os.path.splitext(head)[1]:
        return argv
    import shutil
    found = shutil.which(head) if env is None else shutil.which(head, path=env.get("PATH"))
    return [found, *argv[1:]] if found else argv
