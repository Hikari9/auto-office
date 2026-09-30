"""The new-runs default names the installed release, and any 3.1+ setting is accepted."""
from __future__ import annotations

import pytest

from office import runtime_default, version
from office.state import OfficeError


def test_default_is_the_installed_release(monkeypatch, tmp_path):
    monkeypatch.delenv("OFFICE_NEW_RUNS", raising=False)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    assert runtime_default.new_runs_setting() == version.release_line(version.current())
    runtime_default.require_new_run_runtime()


@pytest.mark.parametrize("value", ["3.1", "3.2", "3.2.0"])
def test_current_line_settings_are_accepted(monkeypatch, value):
    monkeypatch.setenv("OFFICE_NEW_RUNS", value)
    runtime_default.require_new_run_runtime()


def test_nonsense_is_rejected(monkeypatch):
    monkeypatch.setenv("OFFICE_NEW_RUNS", "banana")
    with pytest.raises(OfficeError):
        runtime_default.require_new_run_runtime()
