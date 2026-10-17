"""#651: doctor checks the browser capture can actually launch."""
import sys
from types import ModuleType

from office import doctor


def fake_playwright(monkeypatch, *, failure=None):
    calls = []
    module = ModuleType("playwright.sync_api")

    class Browser:
        def close(self):
            calls.append("close")

    class Chromium:
        executable_path = "/not-the-headless-shell/chrome"

        def launch(self, **kwargs):
            calls.append(("launch", kwargs))
            if failure:
                raise RuntimeError(failure)
            return Browser()

    class Context:
        def __enter__(self):
            return type("API", (), {"chromium": Chromium()})()

        def __exit__(self, *_):
            return False

    module.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return calls


def test_missing_headless_shell_provides_install_command(monkeypatch):
    calls = fake_playwright(
        monkeypatch, failure="BrowserType.launch: Executable doesn't exist at "
        "/cache/chromium_headless_shell-1248/chrome-headless-shell")
    message, problem = doctor._visual_capture_status()
    assert problem and "headless shell missing" in message
    assert "playwright install chromium-headless-shell" in message
    assert calls == [("launch", {"headless": True, "timeout": 10000})]


def test_launch_and_close_means_available(monkeypatch):
    calls = fake_playwright(monkeypatch)
    message, problem = doctor._visual_capture_status()
    assert not problem and "available" in message.lower()
    assert calls == [("launch", {"headless": True, "timeout": 10000}), "close"]


def test_missing_optional_python_package_stays_optional(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    message, problem = doctor._visual_capture_status()
    assert not problem and "playwright missing" in message.lower()


def test_other_launch_failure_cannot_claim_available(monkeypatch):
    fake_playwright(monkeypatch, failure="Host is missing required shared libraries")
    message, problem = doctor._visual_capture_status()
    assert problem and "cannot launch" in message
    assert "shared libraries" in message


def test_doctor_matches_visual_capture_chrome_override(monkeypatch, tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("mock")
    monkeypatch.setenv("OFFICE_CHROME", str(chrome))
    calls = fake_playwright(monkeypatch)
    message, problem = doctor._visual_capture_status()
    assert not problem
    assert calls == [
        ("launch", {"headless": True, "executable_path": str(chrome), "timeout": 10000}),
        "close",
    ]
