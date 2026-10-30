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


def test_supervisor_survives_a_rebuild_and_records_one_office_reinstalled_per_recovery(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)
    mark = root_wake._reinstall_mark(run["id"])

    def broken_poll(con, run):
        raise ImportError("partially written module")
    monkeypatch.setattr(root_wake, "poll", broken_poll)
    assert root_wake.supervise(run["id"], once=True) == 0  # no traceback, the loop keeps going
    assert mark.exists()
    assert _reinstalled(con, run) == []  # unhealthy ticks record nothing

    monkeypatch.setattr(root_wake, "poll", lambda con, run: "no-action")
    assert root_wake.supervise(run["id"], once=True) == 0
    rows = _reinstalled(con, run)
    assert len(rows) == 1
    assert not mark.exists()

    # A later healthy tick with no rebuild in between records nothing more.
    assert root_wake.supervise(run["id"], once=True) == 0
    assert len(_reinstalled(con, run)) == 1


def test_watchdog_thread_marks_the_rebuild_and_records_the_recovery(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    ticks = iter([_broken, lambda con, run: "cleared"])

    def tick(con, run):
        step = next(ticks, None)
        if step is None:
            raise _Stop()
        return step(con, run)

    sleeps = []
    monkeypatch.setattr(root_wake, "watchdog_tick", tick)
    monkeypatch.setattr(root_wake.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(_Stop):
        root_wake._watchdog_loop(run["id"])

    assert len(sleeps) == 2
    assert len(_reinstalled(con, run)) == 1
    assert not root_wake._reinstall_mark(run["id"]).exists()


def test_repeat_recovery_calls_record_one_event(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    root_wake._mark_reinstall(run["id"])

    assert root_wake._recover_reinstall(con, run) is True
    assert root_wake._recover_reinstall(con, run) is False
    assert len(_reinstalled(con, run)) == 1


def test_mark_is_set_once_per_rebuild(env, monkeypatch):
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
