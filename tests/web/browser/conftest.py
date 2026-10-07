"""Playwright fixtures: the web service in fixture mode on a free loopback port."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from office.web import server


HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:  # this hook sees the whole session: mark only the browser tests
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def web_url(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    svc.close()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = None
        for channel in (None, "chrome"):  # Playwright's own build, else an installed Chrome
            try:
                browser = pw.chromium.launch(channel=channel)
                break
            except Exception as exc:  # noqa: BLE001 - no browser binary installed
                reason = str(exc).splitlines()[0]
        if browser is None:
            pytest.skip(f"chromium unavailable: {reason}")
        try:
            yield browser.new_page()
        finally:
            browser.close()
