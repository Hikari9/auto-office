"""Issue #669: booting previews cannot spend a visual review round."""
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import ModuleType, SimpleNamespace
import sys
import urllib.error

from office import db, gates, paths, state, submit, visual


def _response(status):
    class Reply:
        def __enter__(self): return SimpleNamespace(status=status)
        def __exit__(self, *args): pass
    return Reply()


def test_probe_waits_through_502_404_until_200(monkeypatch):
    codes = iter([502, 404, 200])
    seen = []
    def urlopen(url, timeout):
        code = next(codes)
        seen.append(code)
        if code >= 400: raise urllib.error.HTTPError(url, code, "booting", {}, None)
        return _response(code)
    monkeypatch.setattr(visual.urllib.request, "urlopen", urlopen)
    assert visual._wait_url("http://127.0.0.1:5555/", 3, require_success=True) is None
    assert seen == [502, 404, 200]


def test_supplied_server_keeps_404_route_diagnosis(monkeypatch):
    monkeypatch.setattr(visual.urllib.request, "urlopen", lambda u, timeout:
                        (_ for _ in ()).throw(urllib.error.HTTPError(u, 404, "missing", {}, None)))
    assert visual._wait_url("http://127.0.0.1:5555/", 0.2) is None


def test_probe_does_not_stop_at_persistent_502(monkeypatch):
    calls = []
    def urlopen(url, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(url, 502, "booting", {}, None)
    monkeypatch.setattr(visual.urllib.request, "urlopen", urlopen)
    error = visual._wait_url("http://127.0.0.1:5555/", 0.3, require_success=True)
    assert "HTTP 502" in error and len(calls) >= 2


def _capture(monkeypatch, tmp_path, frames, *, ready=None, fail_browser=False):
    monkeypatch.setattr(paths, "run_dir", lambda *_: tmp_path)
    monkeypatch.setattr(submit, "matches_revision", lambda *a: True)
    monkeypatch.setattr(submit, "restore_tracked_paths", lambda *a: [])
    monkeypatch.setattr(visual, "_capture_port_conflict", lambda *a: None)
    monkeypatch.setattr(visual, "_wait_url", lambda *a, **kw: ready)
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)
    monkeypatch.setattr(visual, "reference_path", lambda *a: None)
    if fail_browser:
        monkeypatch.setattr(visual, "_playwright_capture", lambda *a:
                            (_ for _ in ()).throw(AssertionError("do not capture before ready")))
    else:
        monkeypatch.setattr(visual, "_playwright_capture", lambda *a:
                            {"environment": {"engine": "fake"}, "frames": frames})
    monkeypatch.setattr(gates, "_check_env", lambda *a: {})
    monkeypatch.setattr(state, "record_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(db, "transaction", lambda *a: nullcontext())
    class Server:
        pid = 556677
        def wait(self, **kwargs): return 0
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *a, **kw: Server())
    monkeypatch.setattr(visual.os, "killpg", lambda *a: None)
    return visual.capture_all(
        object(), {"id": "r"*32, "office_version": "test", "repo_root": str(tmp_path)},
        {"id": "T1", "title": "UI", "visual": {"url": "http://127.0.0.1:5555/", "start": "serve"}},
        {"id": "V1", "commit_sha": "abc"}, {"id": "G1", "recaptures": 0}, tmp_path)


def test_unready_server_never_launches_browser(monkeypatch, tmp_path):
    outcome = _capture(monkeypatch, tmp_path, [], ready="HTTP 502", fail_browser=True)
    assert outcome["evidence_status"] == "CAPTURE_BLOCKED"
    assert "HTTP 502" in outcome["cause"]


def test_all_boot_errors_include_missing_selectors_but_block_evidence(monkeypatch, tmp_path):
    frames = [
        {"viewport": "mobile", "state": "default", "http_status": 502,
         "cause": "spec", "invalid": "selector not present on boot page"},
        {"viewport": "mobile", "state": "other", "http_status": 404,
         "failures": [{"summary": "main not rendered"}]},
    ]
    outcome = _capture(monkeypatch, tmp_path, frames)
    assert outcome["evidence_status"] == "CAPTURE_BLOCKED", outcome
    assert outcome["product_failures"] == []


def test_healthy_route_with_one_502_remains_product_failure(monkeypatch, tmp_path):
    frames = [
        {"viewport": "mobile", "state": "default", "http_status": 200, "failures": []},
        {"viewport": "mobile", "state": "subpage", "http_status": 502,
         "failures": [{"summary": "page returned HTTP 502"}]},
    ]
    outcome = _capture(monkeypatch, tmp_path, frames)
    assert outcome["evidence_status"] == "COMPARABLE", outcome
    assert len(outcome["product_failures"]) == 1


def test_early_interaction_failure_retains_http_status(monkeypatch, tmp_path):
    fake = ModuleType("playwright.sync_api")
    fake.Error = type("PWError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    class Page:
        url = "http://localhost/"
        def on(self, *args): pass
        def goto(self, *args, **kw): return SimpleNamespace(status=502)
        def evaluate(self, *args): return True
        def query_selector(self, selector): return None
    class Context:
        def new_page(self): return Page()
        def close(self): pass
    class Browser:
        def new_context(self, **kw): return Context()
    frame = visual._capture_one(Browser(), "http://localhost/", 390, 844,
                                {"name": "step", "steps": [{"verb": "click", "arg": "#missing"}]},
                                [], tmp_path, "frame", {})
    assert frame["http_status"] == 502 and frame["cause"] == "spec"


def test_real_local_server_boot_sequence_is_retried():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = [502, 404, 200][min(len(seen), 2)]
            seen.append(status)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ready" if status == 200 else b"not ready")
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert visual._wait_url(f"http://127.0.0.1:{server.server_port}/", 4,
                                require_success=True) is None
        assert seen == [502, 404, 200]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
