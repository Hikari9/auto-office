"""Shared pytest setup.

Tests outside tests/v31 exercise the helper scripts under scripts/ (hooks, monitor, reports).
"""
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, settings

# Subprocesses spawned by tests resolve `python3` through PATH. Put the interpreter running pytest first
# so they get its dependencies (jsonschema, ...) even when the invoking shell has not activated the venv.
_INTERPRETER_DIR = str(Path(sys.executable).parent)
_path_entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
if _path_entries[:1] != [_INTERPRETER_DIR]:
    os.environ["PATH"] = os.pathsep.join([_INTERPRETER_DIR, *_path_entries])

if sys.platform == "win32":
    # Tests script fake harnesses, hooks and tools as `#!` files marked executable. Windows runs neither
    # shebangs nor extensionless files, so `chmod +x` on such a file also writes a sibling `<name>.exe`
    # launcher (`shutil.which` and CreateProcess both find it). See tests/_win_shebang.py.
    from tests import _win_shebang  # noqa: E402
    _win_shebang.install()
    # Office runs in Python's UTF-8 mode on Windows (office/__main__.py). Every Office process a test starts, and
    # the pytest-xdist workers (started after this), inherit it; run `python -X utf8 -m pytest` without `-n`.
    os.environ["PYTHONUTF8"] = "1"
    # Git for Windows' system config turns on core.autocrlf, which rewrites line endings in the byte-exact repos the
    # tests build. Every git the tests run gets git's POSIX default instead.
    os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false"})

# Property tests must be reproducible: `derandomize` fixes the example sequence per test, and no
# example database means a laptop run and a hook run see the same inputs. A failure still prints
# `@reproduce_failure` (print_blob), and a shrunk counterexample worth keeping belongs in an
# `@example(...)` next to the property. `HYPOTHESIS_PROFILE=explore` randomises and widens the
# search for a one-off hunt; the gate never uses it.
_PROPERTY_SETTINGS = dict(deadline=None, print_blob=True, database=None,
                          suppress_health_check=[HealthCheck.function_scoped_fixture])
settings.register_profile("office", max_examples=100, derandomize=True, **_PROPERTY_SETTINGS)
settings.register_profile("explore", max_examples=1000, derandomize=False, **_PROPERTY_SETTINGS)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "office"))

_SCRUBBED_PREFIXES = ("OFFICE_", "HERDR_", "AUTO_OFFICE_")

# The host's own Hermes, resolved before any test edits PATH. Under a test's isolated HOME,
# "hermes auth list" can hang to its 20 s timeout on every routing call that asks for providers.
_HOST_HERMES = shutil.which("hermes")


@pytest.fixture(scope="session")
def _empty_office_user_config(tmp_path_factory):
    """Keep the invoking user's routing preferences out of test configuration."""
    path = tmp_path_factory.mktemp("office-user-config") / "config.yaml"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(scope="session", autouse=True)
def _libyaml_safe_load():
    """Parse YAML with libyaml for the whole session.

    Office loads the catalog, config and adapter YAML many times per command, and
    pure-Python PyYAML made that most of every `office start`. The C loader
    returns identical data.
    """
    if not getattr(yaml, "__with_libyaml__", False):
        yield
        return
    original = yaml.safe_load
    yaml.safe_load = lambda stream: yaml.load(stream, Loader=yaml.CSafeLoader)
    try:
        yield
    finally:
        yaml.safe_load = original


@pytest.fixture(scope="session", autouse=True)
def _no_host_hermes():
    """Never run the host's real Hermes CLI in process: it reads as absent ({}).

    A fake hermes a test puts on PATH, or passes explicitly, still runs.
    """
    from office import hermes_access
    original = hermes_access.auth_status

    def guarded(*, hermes=None):
        exe = hermes or shutil.which("hermes")
        if _HOST_HERMES and exe and os.path.realpath(exe) == os.path.realpath(_HOST_HERMES):
            return {}
        return original(hermes=hermes)

    hermes_access.auth_status = guarded
    try:
        yield
    finally:
        hermes_access.auth_status = original


@pytest.fixture(autouse=True)
def _clean_office_identity(request, monkeypatch, _empty_office_user_config):
    """Start every test with no Office/herdr identity from the launching shell.

    A test run started by an Office worker inherits OFFICE_RUN_ID and friends,
    which make hooks and helpers resolve that run instead of the test's own.
    User configuration is isolated too; individual tests can still override it.
    """
    for key in [k for k in os.environ if k.startswith(_SCRUBBED_PREFIXES)]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(_empty_office_user_config))
    if "v31" not in Path(str(request.fspath)).parts:
        monkeypatch.setenv("OFFICE_PINNED_LEGACY", "1")
    yield


# Tiers: the default run is unit-only. integration, legacy and slow are
# deselected unless --all is given or the user passes their own -m expression.
_NON_UNIT = ("integration", "legacy", "slow")


def pytest_addoption(parser):
    parser.addoption("--all", action="store_true", default=False,
                     help="run every test: disable the default deselection of integration, legacy and slow")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "env" in item.fixturenames:
            item.add_marker(pytest.mark.integration)
    if config.getoption("--all") or config.getoption("markexpr"):
        return
    kept, deselected = [], []
    for item in items:
        (deselected if any(item.get_closest_marker(m) for m in _NON_UNIT) else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
