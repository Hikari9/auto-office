"""Shared pytest setup.

Tests outside tests/v31 exercise the retained Auto Office 3.0 helper surface
(scripts/). They run as that pinned runtime, which is what the 3.1 guard in
scripts/office_runtime.py expects of a 3.0 run's own helper process.
"""
import os
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, settings

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


@pytest.fixture(autouse=True)
def _clean_office_identity(request, monkeypatch):
    """Start every test with no Office/herdr identity from the launching shell.

    A test run started by an Office worker inherits OFFICE_RUN_ID and friends,
    which make hooks and helpers resolve that run instead of the test's own.
    """
    for key in [k for k in os.environ if k.startswith(_SCRUBBED_PREFIXES)]:
        monkeypatch.delenv(key)
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
