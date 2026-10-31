"""#920: a reinstall mid-run fails fast with a retry code, and root wake records one recovery.

The broken install is a fake: `version.current` raises ImportError, and a poll raises ImportError or OSError as a
half-written module would. The recovery is the same fake returning healthy. No real install, pane or model.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import approved_run
from test_root_wake_723 import setup_root

from office import cli, root_wake, state, version

def retry_line(cause: str) -> str:
    return f"office: install is being rebuilt, retry in 5 s ({cause})"


def _broken(*_args, **_kwargs):
    raise ImportError("cannot import name 'x' from partially initialized module 'office.version'")


def _run(con) -> dict:
    return state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])


def _reinstalled(con, run) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT seq, summary, payload_json FROM events WHERE run_id=? AND kind='office.reinstalled' ORDER BY seq",
        (run["id"],))]


class _Stop(BaseException):
    """Ends the watchdog loop; a BaseException so the loop's own `except Exception` cannot swallow it."""


class _ReplacedModule:
    """Stands in for a module whose file is being replaced: reading any attribute fails."""

    def __init__(self, exc):
        self._exc = exc

    def __getattr__(self, name):
        raise self._exc


# --- the CLI fails fast with a dedicated code while the install is being rebuilt -------------------------------


@pytest.mark.parametrize(("failure", "cause"), [
    (ImportError("partially written"), "ImportError: office"),
    (OSError("No such file or directory"), "OSError: office"),
])
def test_main_exits_with_retry_code_when_the_install_cannot_resolve(monkeypatch, capsys, failure, cause):
    def fail():
        raise failure
    monkeypatch.setattr(version, "current", fail)
    monkeypatch.setattr(cli, "_run", _broken)  # the command itself must never start

    assert cli.main(["status"]) == version.REBUILD_EXIT_CODE

    captured = capsys.readouterr()
    assert captured.err.strip() == retry_line(cause)
    assert "Traceback" not in captured.err and captured.out == ""


def test_a_bad_version_override_is_configuration_and_still_raises(monkeypatch):
    """Retrying cannot fix OFFICE_VERSION_OVERRIDE, so it is not classified as a rebuild."""
    def bad():
        raise RuntimeError("OFFICE_VERSION_OVERRIDE='dev' is not an exact PEP 440 version")
    monkeypatch.setattr(version, "current", bad)

    with pytest.raises(RuntimeError, match="OFFICE_VERSION_OVERRIDE"):
        version.rebuilding_reason()
    with pytest.raises(RuntimeError, match="OFFICE_VERSION_OVERRIDE"):
        cli.main(["status"])


def test_python_dash_m_office_fails_fast_when_the_cli_import_is_mid_rebuild(tmp_path):
    """Real interpreter: `office.cli` cannot import and the version probe is broken, as during `uv tool install`."""
    script = tmp_path / "rebuilding.py"
    script.write_text(
        "import runpy, sys\n"
        "import office.version as v\n"
        "def boom():\n"
        "    raise ImportError('partially written')\n"
        "v.current = boom\n"
        "sys.modules['office.cli'] = None\n"
        "runpy.run_module('office', run_name='__main__', alter_sys=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(version.SOURCE_ROOT) / "src")}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30, env=env)

    assert proc.returncode == version.REBUILD_EXIT_CODE, proc.stderr
    assert proc.stderr.strip() == retry_line("ModuleNotFoundError: office.cli")
    assert "Traceback" not in proc.stderr


def test_console_script_entry_fails_fast_when_the_cli_import_is_mid_rebuild(monkeypatch, capsys):
    """The `office` console script calls `office.__main__:entry`, which guards the import of `office.cli`."""
    from office import __main__ as entrypoint

    monkeypatch.setitem(sys.modules, "office.cli", None)  # the import of office.cli fails, as mid-rebuild
    monkeypatch.setattr(version, "current", _broken)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.err.strip() == retry_line("ModuleNotFoundError: office.cli")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(("failure", "cause"), [
    (OSError(2, "replaced", str(version.PACKAGE_ROOT / "cli.py")), "FileNotFoundError: office.cli"),
    (SyntaxError("truncated version.py", (str(version.PACKAGE_ROOT / "version.py"), 1, 1, "")), "SyntaxError: office.version"),
])
def test_a_replaced_cli_module_exits_with_the_retry_code(monkeypatch, capsys, failure, cause):
    """An OSError or SyntaxError while the CLI module loads is the same replacement, not a traceback."""
    from office import __main__ as entrypoint

    monkeypatch.setitem(sys.modules, "office.cli", _ReplacedModule(failure))
    monkeypatch.setattr(version, "current", _broken)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    assert capsys.readouterr().err.strip() == retry_line(cause)


def test_an_unloadable_version_module_at_startup_exits_with_the_retry_code(monkeypatch, capsys):
    """version.py is stdlib-only, so when it cannot load its file is being replaced or damaged (#920)."""
    import office
    from office import __main__ as entrypoint

    monkeypatch.setitem(sys.modules, "office.cli", None)
    monkeypatch.setattr(office, "version", _ReplacedModule(SyntaxError("truncated version.py")))

    assert entrypoint.entry() == 75
    assert capsys.readouterr().err.strip() == retry_line("ModuleNotFoundError: office.cli")


def test_a_dependency_import_failure_exits_1_with_its_cause_and_a_reinstall_hint(monkeypatch, capsys):
    """A module outside the office package does not heal by retrying: exit 1, not the retry code (A15)."""
    from office import __main__ as entrypoint

    missing = ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
    monkeypatch.setitem(sys.modules, "office.cli", _ReplacedModule(missing))
    probed = []
    monkeypatch.setattr(version, "current", lambda: probed.append("probe") or "3.2.0")

    assert entrypoint.entry() == 1
    err = capsys.readouterr().err
    assert "ModuleNotFoundError: jsonschema" in err
    assert "uv tool install --force auto-office" in err and "--editable" in err and "retry after" in err
    assert "rebuilt" not in err and "Traceback" not in err
    assert probed == []


def test_a_nameless_import_failure_raised_by_a_dependency_exits_1(monkeypatch, capsys):
    """An ImportError with no module name, raised by third-party code, is a dependency failure, not a rebuild (A15)."""
    from office import __main__ as entrypoint

    class Dependency:
        def __getattr__(self, name):
            raise ImportError("libssl.so: cannot open shared object file")

    monkeypatch.setitem(sys.modules, "office.cli", Dependency())

    assert entrypoint.entry() == 1
    err = capsys.readouterr().err
    assert err.startswith("office: cannot start, a dependency failed to import (ImportError: ")
    assert "rebuilt" not in err and "Traceback" not in err


def test_a_syntax_error_in_a_dependency_file_exits_1(monkeypatch, capsys):
    from office import __main__ as entrypoint

    class Dependency:
        def __getattr__(self, name):
            raise SyntaxError("invalid syntax", ("/site-packages/dep.py", 3, 1, ""))

    monkeypatch.setitem(sys.modules, "office.cli", Dependency())

    assert entrypoint.entry() == 1
    err = capsys.readouterr().err
    assert "SyntaxError: dep.py" in err and "rebuilt" not in err


def test_a_nameless_import_failure_inside_the_package_still_exits_75(monkeypatch, capsys):
    from office import __main__ as entrypoint

    partial = ImportError("partially written")
    partial.filename = str(version.PACKAGE_ROOT / "cli.py")
    monkeypatch.setitem(sys.modules, "office.cli", _ReplacedModule(partial))

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    assert capsys.readouterr().err.strip() == retry_line("ImportError: office.cli")


def test_python_dash_m_office_exits_1_for_a_dependency_failure(tmp_path):
    """Real interpreter: a dependency outside office fails inside `office.cli`, so the process exits 1 with its cause."""
    script = tmp_path / "missing_dependency.py"
    script.write_text(
        "import runpy, sys\n"
        "class Broken:\n"
        "    def __getattr__(self, name):\n"
        "        raise ModuleNotFoundError(\"No module named 'jsonschema'\", name='jsonschema')\n"
        "sys.modules['office.cli'] = Broken()\n"
        "runpy.run_module('office', run_name='__main__', alter_sys=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(version.SOURCE_ROOT) / "src")}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30, env=env)

    assert proc.returncode == 1, proc.stderr
    assert "ModuleNotFoundError: jsonschema" in proc.stderr and "uv tool install --force auto-office" in proc.stderr
    assert "rebuilt" not in proc.stderr and "Traceback" not in proc.stderr


def test_a_startup_import_failure_never_traces_back_even_when_the_version_probe_raises(monkeypatch, capsys):
    """The entry does not probe the version, so a bad OFFICE_VERSION_OVERRIDE cannot escape it as a traceback."""
    from office import __main__ as entrypoint

    probed = []

    def bad_override():
        probed.append("probe")
        raise RuntimeError("OFFICE_VERSION_OVERRIDE='dev' is not an exact PEP 440 version")

    partial = ImportError("partially initialized module", name="office.cli")
    monkeypatch.setitem(sys.modules, "office.cli", _ReplacedModule(partial))
    monkeypatch.setattr(version, "current", bad_override)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.err.strip() == retry_line("ImportError: office.cli")
    assert "Traceback" not in captured.err
    assert probed == []


def test_python_dash_m_office_exits_with_the_retry_code_when_the_probe_is_healthy(tmp_path):
    """Real interpreter: `office.cli` cannot import inside the office package while the version probe resolves."""
    script = tmp_path / "healthy_probe.py"
    script.write_text(
        "import runpy, sys\n"
        "import office.version as v\n"
        "v.current = lambda: '3.2.0'\n"
        "sys.modules['office.cli'] = None\n"
        "runpy.run_module('office', run_name='__main__', alter_sys=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(version.SOURCE_ROOT) / "src")}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30, env=env)

    assert proc.returncode == version.REBUILD_EXIT_CODE, proc.stderr
    assert proc.stderr.strip() == retry_line("ModuleNotFoundError: office.cli")
    assert "Traceback" not in proc.stderr


def test_console_script_points_at_the_guarded_entry():
    import tomllib

    with (version.SOURCE_ROOT / "pyproject.toml").open("rb") as fh:
        target = tomllib.load(fh)["project"]["scripts"]["office"]
    assert target == "office.__main__:entry"


def test_the_wake_supervisor_skips_the_startup_probe(monkeypatch):
    """The supervisor is a background process started on every wake; the probe would delay it (#920)."""
    probed = []
    monkeypatch.setattr(version, "rebuilding_reason", lambda: probed.append(1) or "ImportError: partial")
    monkeypatch.setattr(root_wake, "supervise", lambda run_id: 0)

    assert cli.main(["_wake_supervise", "some-run"]) == 0
    assert probed == []


def test_a_healthy_install_is_not_reported_as_rebuilding(monkeypatch):
    monkeypatch.setattr(version, "current", lambda: "3.2.0")
    assert version.rebuilding_reason() is None


# --- the supervisor keeps running through a rebuild and records one recovery ------------------------------------


@pytest.mark.parametrize("failure", [ImportError("partially written module"), OSError("No such file or directory")])
def test_supervisor_survives_a_rebuild_and_records_one_office_reinstalled_per_recovery(env, monkeypatch, failure, capsys):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)
    mark = root_wake._reinstall_mark(run["id"])

    def broken_poll(con, run):
        raise failure
    monkeypatch.setattr(root_wake, "poll", broken_poll)
    assert root_wake.supervise(run["id"], once=True) == 0  # no traceback, the loop keeps going
    assert mark.exists()
    assert _reinstalled(con, run) == []  # unhealthy ticks record nothing
    assert type(failure).__name__ in capsys.readouterr().err

    monkeypatch.setattr(root_wake, "poll", lambda con, run: "no-action")
    assert root_wake.supervise(run["id"], once=True) == 0
    rows = _reinstalled(con, run)
    assert len(rows) == 1
    assert not mark.exists()

    # A later healthy tick with no failure in between records nothing more.
    assert root_wake.supervise(run["id"], once=True) == 0
    assert len(_reinstalled(con, run)) == 1


def test_a_permanent_import_failure_is_logged_with_its_cause_and_not_called_a_rebuild(env, monkeypatch, capsys):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)

    def missing_dependency(con, run):
        raise ModuleNotFoundError("No module named 'jsonschema'")
    monkeypatch.setattr(root_wake, "poll", missing_dependency)
    assert root_wake.supervise(run["id"], once=True) == 0

    err = capsys.readouterr().err
    assert "ModuleNotFoundError" in err and "jsonschema" in err
    assert "rebuilt" not in err


def test_a_failed_mark_write_never_stops_the_supervisor(env, monkeypatch):
    """Disk full while the episode is marked: the tick reports nothing and the loop keeps running."""
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)

    def broken_poll(con, run):
        raise ImportError("partially written module")
    monkeypatch.setattr(root_wake, "poll", broken_poll)

    def no_space(*_args, **_kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(root_wake.os, "link", no_space)
    assert root_wake.supervise(run["id"], once=True) == 0
    assert not root_wake._reinstall_mark(run["id"]).exists()
    assert list(root_wake._reinstall_mark(run["id"]).parent.glob("*.tmp")) == []


def test_watchdog_thread_marks_the_failure_and_records_the_recovery(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    ticks = iter([ImportError("partially written module"), None])

    def tick(con, run):
        step = next(ticks, "stop")
        if step == "stop":
            raise _Stop()
        if step is not None:
            raise step
        return "cleared"

    sleeps = []
    monkeypatch.setattr(root_wake, "watchdog_tick", tick)
    monkeypatch.setattr(root_wake.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(_Stop):
        root_wake._watchdog_loop(run["id"])

    assert len(sleeps) == 2
    assert len(_reinstalled(con, run)) == 1
    assert not root_wake._reinstall_mark(run["id"]).exists()


def test_watchdog_thread_treats_an_os_error_as_a_failed_tick(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    ticks = iter([OSError("resource replaced"), None])

    def tick(con, run):
        step = next(ticks, "stop")
        if step == "stop":
            raise _Stop()
        if step is not None:
            raise step
        return "cleared"

    monkeypatch.setattr(root_wake, "watchdog_tick", tick)
    monkeypatch.setattr(root_wake.time, "sleep", lambda s: None)
    with pytest.raises(_Stop):
        root_wake._watchdog_loop(run["id"])

    assert len(_reinstalled(con, run)) == 1


def test_repeat_recovery_calls_record_one_event(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])

    assert root_wake._recover_reinstall(con, run) is True
    assert root_wake._recover_reinstall(con, run) is False
    assert len(_reinstalled(con, run)) == 1


def test_a_failed_unlink_after_commit_does_not_record_twice(env, monkeypatch):
    """The event committed, then removing the mark failed. The retry removes the mark without a second event."""
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])
    mark = root_wake._reinstall_mark(run["id"])
    token = mark.read_text().strip()

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == mark:
            raise OSError("interrupted after commit")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError, match="interrupted"):
        root_wake._recover_reinstall(con, run)
    assert len(_reinstalled(con, run)) == 1  # the event is committed before the unlink

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert root_wake._recover_reinstall(con, run) is True
    assert not mark.exists()
    rows = _reinstalled(con, run)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["mark"] == token


def test_a_recovery_after_the_run_ends_clears_the_mark_without_an_event(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])
    monkeypatch.setattr(root_wake.state, "is_terminal", lambda _run: True)

    assert root_wake._recover_reinstall(con, run) is True
    assert not root_wake._reinstall_mark(run["id"]).exists()
    assert _reinstalled(con, run) == []


def test_a_failed_record_keeps_the_mark_for_the_next_healthy_tick(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])

    real_emit = root_wake.state.emit

    def failing_emit(*_args, **_kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(root_wake.state, "emit", failing_emit)
    with pytest.raises(RuntimeError, match="disk full"):
        root_wake._recover_reinstall(con, run)
    assert root_wake._reinstall_mark(run["id"]).exists()
    assert _reinstalled(con, run) == []

    monkeypatch.setattr(root_wake.state, "emit", real_emit)
    assert root_wake._recover_reinstall(con, run) is True
    assert len(_reinstalled(con, run)) == 1


def test_the_mark_is_never_observable_without_its_token(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    mark = root_wake._reinstall_mark(run["id"])

    assert root_wake._mark_reinstall(run["id"]) is True
    assert re.fullmatch(r"[0-9a-f]{32}", mark.read_text())
    assert list(mark.parent.glob(f"{mark.name}.*.tmp")) == []


def test_mark_is_set_once_per_episode(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)

    assert root_wake._mark_reinstall(run["id"]) is True
    assert root_wake._mark_reinstall(run["id"]) is False


# --- the recovery reaches the root as an actionable wake that says to resume --------------------------------------


def test_office_reinstalled_is_actionable_and_its_wake_text_says_to_resume(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])
    root_wake._recover_reinstall(con, run)
    seq = _reinstalled(con, run)[0]["seq"]

    assert "office.reinstalled" in root_wake.ACTIONABLE
    signature, text, _human = root_wake._actionable_now(con, _run(con), [seq])
    assert text.startswith("office.reinstalled: Office install was unavailable")
    assert "resume" in text
    assert f"office --run {run['id']} context" in text
    assert signature


def test_each_recovery_has_its_own_actionable_signature(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    for _ in range(2):
        root_wake._mark_reinstall(run["id"])
        root_wake._recover_reinstall(con, run)
    seqs = [row["seq"] for row in _reinstalled(con, run)]
    assert len(seqs) == 2
    signatures = [root_wake._actionable_now(con, _run(con), [seq])[0] for seq in seqs]

    assert signatures[0] != signatures[1]
