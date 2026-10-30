"""#920: a reinstall mid-run fails fast with a retry code, and root wake records one recovery.

The broken install is a fake: `version.current` raises ImportError, and a poll raises ImportError as a
half-written module would. The recovery is the same fake returning healthy. No real install, pane or model.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import approved_run
from test_root_wake_723 import setup_root

from office import cli, root_wake, state, version

RETRY_LINE = "office: install is being rebuilt, retry in 5 s"


def _broken(*_args, **_kwargs):
    raise ImportError("cannot import name 'x' from partially initialized module 'office.version'")


def _run(con) -> dict:
    return state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])


def _reinstalled(con, run) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT seq, summary FROM events WHERE run_id=? AND kind='office.reinstalled' ORDER BY seq", (run["id"],))]


class _Stop(BaseException):
    """Ends the watchdog loop; a BaseException so the loop's own `except Exception` cannot swallow it."""


# --- the CLI fails fast with a dedicated code while the install is being rebuilt -------------------------------


@pytest.mark.parametrize("failure", [ImportError("partially written"), OSError("No such file or directory")])
def test_main_exits_with_retry_code_when_the_install_cannot_resolve(monkeypatch, capsys, failure):
    def fail():
        raise failure
    monkeypatch.setattr(version, "current", fail)
    monkeypatch.setattr(cli, "_run", _broken)  # the command itself must never start

    assert cli.main(["status"]) == version.REBUILD_EXIT_CODE

    captured = capsys.readouterr()
    assert captured.err.strip() == RETRY_LINE
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


def test_console_script_entry_fails_fast_when_the_cli_import_is_mid_rebuild(monkeypatch, capsys):
    """The `office` console script calls `office.__main__:entry`, which guards the import of `office.cli`."""
    from office import __main__ as entrypoint

    monkeypatch.setitem(sys.modules, "office.cli", None)  # the import of office.cli fails, as mid-rebuild
    monkeypatch.setattr(version, "current", _broken)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.err.strip() == RETRY_LINE
    assert "Traceback" not in captured.err


def test_a_replacement_during_a_command_exits_with_the_retry_code(monkeypatch, capsys):
    """The install is replaced after startup: a command's lazy import fails and the fresh probe sees it."""
    from office import __main__ as entrypoint

    def lazy_import_fails():
        raise ImportError("partially written module")
    monkeypatch.setattr(cli, "main", lazy_import_fails)
    monkeypatch.setattr(version, "current", _broken)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    assert capsys.readouterr().err.strip() == RETRY_LINE


def test_a_cached_identity_does_not_hide_a_replacement_that_started_after_startup(monkeypatch, capsys):
    """`current` is cached per process. The first call succeeds at startup; the replacement comes later."""
    import functools
    from office import __main__ as entrypoint

    calls = []

    def identity():
        calls.append(1)
        if len(calls) > 1:
            raise ImportError("partially written module")
        return "3.2.0"
    monkeypatch.setattr(version, "current", functools.lru_cache(maxsize=1)(identity))
    version.current()  # startup caches the healthy identity

    def lazy_import_fails():
        raise ImportError("partially written module")
    monkeypatch.setattr(cli, "main", lazy_import_fails)

    assert entrypoint.entry() == version.REBUILD_EXIT_CODE
    assert capsys.readouterr().err.strip() == RETRY_LINE


def test_an_import_failure_with_a_healthy_install_keeps_its_traceback(monkeypatch):
    """A broken dependency with a healthy install is not a rebuild: the original error propagates."""
    from office import __main__ as entrypoint

    def missing_dependency():
        raise ModuleNotFoundError("No module named 'jsonschema'")
    monkeypatch.setattr(cli, "main", missing_dependency)
    monkeypatch.setattr(version, "current", lambda: "3.2.0")

    with pytest.raises(ModuleNotFoundError, match="jsonschema"):
        entrypoint.entry()


def test_console_script_points_at_the_guarded_entry():
    import tomllib

    with (version.SOURCE_ROOT / "pyproject.toml").open("rb") as fh:
        target = tomllib.load(fh)["project"]["scripts"]["office"]
    assert target == "office.__main__:entry"


def test_a_healthy_install_is_not_reported_as_rebuilding(monkeypatch):
    monkeypatch.setattr(version, "current", lambda: "3.2.0")
    assert version.rebuilding_reason() is None


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
    assert proc.stderr.strip() == RETRY_LINE
    assert "Traceback" not in proc.stderr


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


def test_a_crash_between_commit_and_unlink_does_not_record_twice(env, monkeypatch):
    """The event committed and the process died before the mark was removed: the next recovery only unlinks."""
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])
    token = root_wake._reinstall_mark(run["id"]).read_text().strip()
    state.emit(con, run, "office.reinstalled", "already recorded", payload={"mark": token})
    con.commit()

    assert root_wake._recover_reinstall(con, run) is True
    assert not root_wake._reinstall_mark(run["id"]).exists()
    assert len(_reinstalled(con, run)) == 1


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
    assert text.startswith("office.reinstalled: Office install was rebuilt")
    assert "resume" in text
    assert f"office --run {run['id']} context" in text
    assert signature


def test_each_recovery_has_its_own_actionable_signature(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    signatures = []
    for _ in range(2):
        root_wake._mark_reinstall(run["id"])
        root_wake._recover_reinstall(con, run)
    seqs = [row["seq"] for row in _reinstalled(con, run)]
    assert len(seqs) == 2
    for seq in seqs:
        signatures.append(root_wake._actionable_now(con, _run(con), [seq])[0])

    assert signatures[0] != signatures[1]
