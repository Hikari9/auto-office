"""Windows only: make the tests' `#!` fake executables runnable (#915).

Tests create fake harnesses, git hooks and tools as `#!` scripts and `chmod +x` them. Windows
honours neither the shebang nor the exec bit, so an extensionless `codex` is invisible to
`shutil.which` and raises WinError 193 when launched. `install()` wraps `os.chmod` (which
`Path.chmod` calls) so that making a `#!` file executable also writes `<file>.exe` next to it: a
distlib console launcher whose embedded `__main__` runs the script with the interpreter its shebang
names. `shutil.which("codex")` resolves `codex.exe` through PATHEXT, and CreateProcess appends
`.exe` to an extensionless path, so both lookups and direct launches reach it. An .exe launcher
(not a .cmd shim) keeps argv intact, including multi-line prompts.

Python shebangs run in the launcher's own interpreter; `sh`/`bash` shebangs run under Git for
Windows' bash. Requires `distlib` (the `test` extra pulls it in on Windows).
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path

_MAIN = '''\
import os, runpy, subprocess, sys
TARGET = {target!r}
KIND = {kind!r}
INTERP = {interp!r}
if KIND == "python":
    sys.argv[0] = TARGET
    sys.path[0] = os.path.dirname(TARGET)
    runpy.run_path(TARGET, run_name="__main__")
else:
    sys.exit(subprocess.call([*INTERP, TARGET, *sys.argv[1:]]))
'''

_launcher: bytes | None = None
_launchers: set[Path] = set()  # every launcher written, so deleting its fake deletes it too
_bash: str | None = None


def _git_bash() -> str | None:
    global _bash
    if _bash is None:
        git = shutil.which("git")
        for root in Path(git).resolve().parents if git else ():
            for candidate in (root / "usr" / "bin" / "bash.exe", root / "bin" / "bash.exe"):
                if candidate.is_file():
                    _bash = str(candidate)
                    return _bash
        _bash = ""
    return _bash or None


def _interpreter(shebang: str) -> tuple[str, list[str]] | None:
    words = shebang[2:].strip().split()
    if not words:
        return None
    if Path(words[0]).name == "env":
        words = [w for w in words[1:] if not w.startswith("-")]  # `env -S python3 -u`
    if not words:
        return None
    name = Path(words[0]).name.lower()
    if name.startswith("python") or Path(words[0]).resolve() == Path(sys.executable).resolve():
        return "python", []
    if name in ("sh", "bash", "sh.exe", "bash.exe"):
        bash = _git_bash()
        return ("shell", [bash, *words[1:]]) if bash else None
    found = shutil.which(words[0]) or shutil.which(name)
    return ("other", [found, *words[1:]]) if found else None


def _shebang_argv(path: Path) -> list[str]:
    """`[interpreter..., path]` for a `#!` script, or `[path]` when it is not one (or names nothing found)."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return [str(path)]
    found = _interpreter(first.decode("utf-8", "replace")) if first.startswith(b"#!") else None
    if found is None:
        return [str(path)]
    kind, interp = found
    return [sys.executable, str(path)] if kind == "python" else [*interp, str(path)]


def write_launcher(path: str | os.PathLike) -> Path | None:
    """Write `<path>.exe` for the `#!` script at `path`; None when it is not one Windows can run."""
    global _launcher
    path = Path(path)
    if path.suffix.lower() in (".exe", ".cmd", ".bat", ".com") or not path.is_file():
        return None
    try:
        with open(path, "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    found = _interpreter(first.decode("utf-8", "replace"))
    if found is None:
        return None
    kind, interp = found
    if _launcher is None:
        from distlib.scripts import ScriptMaker
        _launcher = ScriptMaker(None, None)._get_launcher("t")
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr("__main__.py", _MAIN.format(target=str(path.resolve()), kind=kind, interp=interp))
    exe = path.with_name(path.name + ".exe")
    python = sys.executable
    shebang = (f'#!"{python}"' if " " in python else f"#!{python}").encode() + b"\n"
    try:
        exe.write_bytes(_launcher + shebang + body.getvalue())
    except OSError:  # a launcher still running from an earlier write of the same fake
        return None
    _launchers.add(exe)
    return exe


def python3_dir() -> str:
    """A directory holding `python3.exe`, which forwards to this interpreter. Plans and fakes in the tests say
    `python3`, which on Windows is at best the Microsoft Store alias and never the venv running the tests."""
    import atexit
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="office-test-python3-"))
    atexit.register(shutil.rmtree, root, True)
    shim = root / "python3"
    shim.write_text("#!python\nimport subprocess, sys\n"
                    "sys.exit(subprocess.call([sys.executable, *sys.argv[1:]]))\n", encoding="utf-8")
    write_launcher(shim)
    return str(root)


def _mirror_home() -> None:
    """Tests isolate the user's home by setting HOME. Windows reads it from USERPROFILE (`expanduser`,
    `Path.home()` and every child process), so a monkeypatched HOME is mirrored there."""
    import pytest
    setenv, delenv = pytest.MonkeyPatch.setenv, pytest.MonkeyPatch.delenv

    def mirrored_setenv(self, name, value, prepend=None):
        setenv(self, name, value, prepend)
        if name == "HOME":
            setenv(self, "USERPROFILE", value)

    def mirrored_delenv(self, name, raising=True):
        delenv(self, name, raising)
        if name == "HOME":
            delenv(self, "USERPROFILE", raising=False)

    pytest.MonkeyPatch.setenv, pytest.MonkeyPatch.delenv = mirrored_setenv, mirrored_delenv


PYTHON3_DIR: str | None = None


def unix_tool_dirs() -> list[str]:
    """Git for Windows' `usr\\bin` (sh, true, sleep, env), last on PATH as its "Unix tools from the Command Prompt"
    option puts it: tests script fakes and suites with those tools, and System32 still wins every name it has."""
    bash = _git_bash()
    return [str(Path(bash).parent)] if bash else []


def install() -> None:
    global PYTHON3_DIR
    _mirror_home()
    PYTHON3_DIR = python3_dir()
    os.environ["PATH"] = os.pathsep.join([os.environ["PATH"].split(os.pathsep)[0], PYTHON3_DIR,
                                          *os.environ["PATH"].split(os.pathsep)[1:], *unix_tool_dirs()])
    original = os.chmod

    def chmod(path, mode, *args, **kwargs):
        original(path, mode, *args, **kwargs)
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) and not isinstance(path, int):
            write_launcher(path)

    os.chmod = chmod
    original_utime = os.utime

    def utime(path, *args, **kwargs):
        # A test that ages or freshens a fake (an mtime-keyed cache) sees the launcher `which` returns.
        original_utime(path, *args, **kwargs)
        if not isinstance(path, int):
            launcher = Path(path).with_name(Path(path).name + ".exe")
            if launcher.is_file():
                original_utime(launcher, *args, **kwargs)

    os.utime = utime
    original_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        # A test that deletes a fake (`(bin / "claude").unlink()`) means the command is gone: so is its launcher,
        # which `which` would otherwise still find and which would then fail to run the missing script.
        original_unlink(path, *args, **kwargs)
        if not isinstance(path, int) and Path(path).suffix.lower() != ".exe":
            launcher = Path(path).with_name(Path(path).name + ".exe")
            if launcher in _launchers:
                try:
                    original_unlink(launcher)
                except OSError:
                    pass
                _launchers.discard(launcher)

    os.unlink = os.remove = unlink

    import subprocess
    original_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        env = kw.get("env")
        if env and env.get("HOME") and env.get("USERPROFILE") != env["HOME"]:
            # A test that hands a child an isolated HOME: Windows programs (Python's `expanduser`, the harnesses)
            # find the home through USERPROFILE, and without this they would write to the real user's home.
            kw["env"] = {**env, "USERPROFILE": env["HOME"]}
        # A test that runs a fake by its full extensionless path: Windows cannot execute the script itself.
        if isinstance(args, (list, tuple)) and args and not kw.get("shell"):
            head = Path(str(args[0]))
            launcher = head.with_name(head.name + ".exe")
            if not head.suffix and head.is_file() and launcher.is_file():
                args = [str(launcher), *args[1:]]
            elif head.suffix.lower() not in (".exe", ".cmd", ".bat", ".com") and head.is_file():
                # A repository script run by its path (`scripts/office_liveness.sh`, a `.mjs` hook): run it the way
                # Git Bash does, with the interpreter its `#!` line names.
                args = [*_shebang_argv(head), *args[1:]]
        original_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = popen_init
