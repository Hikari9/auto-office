"""Visual gate with real headless Chrome (skipped when Playwright is absent)."""
from __future__ import annotations

import socket
from importlib.util import find_spec

import pytest

requires_playwright = pytest.mark.skipif(
    find_spec("playwright") is None,
    reason="install the visual extra to run browser capture integration tests",
)


import pytest as _pytest  # noqa: E402

pytestmark = [_pytest.mark.integration, _pytest.mark.review_contract("v3.1")]


@pytest.fixture(scope="module", autouse=True)
def _shared_browser():
    """One Chromium per module, reused by every capture in it (each capture still gets
    its own browser context). Production leaves the hook unset and launches its own browser."""
    if find_spec("playwright") is None:
        yield
        return
    from playwright.sync_api import sync_playwright

    from office import visual
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**visual.launch_args())
        visual._shared_browser = browser
        try:
            yield
        finally:
            visual._shared_browser = None
            browser.close()


EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

PAGE = """<!doctype html><html><head><style>
body{margin:0;font-family:Helvetica,Arial,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;height:56px;padding:0 16px;background:#123}
header h1{color:#fff;font-size:20px;margin:0}
#menu{width:44px;height:44px;margin-right:0}
nav{display:none;background:#eee;padding:12px}
nav.open{display:block}
</style></head><body>
<header><h1>Acme</h1><button id="menu" data-test="menu">≡</button></header>
<nav id="nav"><a href="#">Home</a> <a href="#">About</a></nav>
<main><p>Hello</p></main>
<script>document.getElementById('menu').onclick=()=>document.getElementById('nav').classList.toggle('open')</script>
</body></html>"""

CLIPPED = PAGE.replace("#menu{width:44px;height:44px;margin-right:0}", "#menu{width:44px;height:44px;margin-right:-60px}")
BROKEN = PAGE.replace("<script>document.getElementById('menu').onclick=()=>document.getElementById('nav').classList.toggle('open')</script>", "")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _plan(port: int, reference: str | None = "design/prototype.html", extra: str = "") -> str:
    ref = f"  reference: {reference}\n" if reference else ""
    return f"""# Plan

## Requirements
done:
- the header menu works on desktop and mobile
blast_radius: repo

## Tasks
### T1: Header with menu
scope: site/**
depends: none
checks: none
accept:
- the menu button opens the navigation on desktop and mobile portrait
visual:
  url: http://127.0.0.1:{port}/site/index.html
  start: python3 -m http.server {port} --bind 127.0.0.1
{ref}  viewports: desktop, mobile
  states: default; menu-open = click [data-test=menu] -> expect nav.open
  selectors: header, #menu
{extra}"""


def _setup(env, page: str, *, port: int, reference: str | None = "design/prototype.html", visual_reply=None,
           probe="auto", extra=""):
    env.trust()
    (env.repo / "design").mkdir(exist_ok=True)
    (env.repo / "design" / "prototype.html").write_text(PAGE)
    env.git("add", "-A")
    env.git("commit", "-qm", "reference")
    env.script(probe=[{"reply": probe}],
               visual_reviewer=[{"reply": visual_reply or "EVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS"}])
    code, out = env.office("start", "header", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan(port, reference, extra))
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree FROM dispatches WHERE role='executor'").fetchone())
    from pathlib import Path
    wt = Path(d["worktree"])
    (wt / "site").mkdir(exist_ok=True)
    (wt / "site" / "index.html").write_text(page)
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"]}
    code, out = env.office("submit", cwd=wt, env=wenv)
    return con, out, wt, wenv


def _gate(con):
    return dict(con.execute("SELECT * FROM gates WHERE kind='visual' ORDER BY created_at DESC LIMIT 1").fetchone())


def _task(con):
    return dict(con.execute("SELECT * FROM tasks WHERE id='T1'").fetchone())


def _capture_inputs(monkeypatch, tmp_path, *, port: int, start: str):
    import os
    import subprocess
    from contextlib import nullcontext
    from pathlib import Path

    from office import visual

    worktree = tmp_path / "repo"
    (worktree / "site").mkdir(parents=True)
    (worktree / ".gitignore").write_text("ignored.out\n")
    original = "<!doctype html><title>submitted</title>\n"
    (worktree / "site" / "index.html").write_text(original)

    def git(*args):
        return subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    git("init")
    git("config", "user.name", "Visual Test")
    git("config", "user.email", "visual-test@example.test")
    git("add", "-A")
    git("commit", "-m", "submitted revision")
    commit = git("rev-parse", "HEAD")

    run_dir = tmp_path / "office-run"
    monkeypatch.setattr(visual.paths, "run_dir", lambda _run_id: run_dir)
    monkeypatch.setattr(visual.gates, "_check_env", lambda _run: dict(os.environ))
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)
    monkeypatch.setattr(visual, "reference_path", lambda _run, _task, _worktree: None)
    monkeypatch.setattr(visual, "_playwright_capture",
                        lambda _url, _spec, _evdir, _ref: {"environment": {"engine": "test"}, "frames": []})
    monkeypatch.setattr(visual.db, "transaction", lambda _con: nullcontext())
    monkeypatch.setattr(visual.state, "record_evidence", lambda *_args, **_kwargs: None)

    run = {"id": "run-1", "office_version": "test"}
    task = {"id": "T1", "title": "visual capture", "visual": {
        "url": f"http://127.0.0.1:{port}/site/index.html", "start": start}}
    rev = {"id": "R1", "commit_sha": commit}
    gate = {"id": "G1", "recaptures": 0}
    return visual, worktree, original, run, task, rev, gate


def test_start_rewrite_is_restored_and_recorded_without_invalidating_capture(monkeypatch, tmp_path):
    import json
    import shlex
    import sys
    from pathlib import Path

    port = _port()
    script = tmp_path / "rewrite_and_serve.py"
    script.write_text(f"""from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
path = Path('site/index.html')
path.write_text(path.read_text() + '<!-- serve rewrite -->\\n')
Path('generated.out').write_text('untracked output')
Path('ignored.out').write_text('ignored output')
ThreadingHTTPServer(('127.0.0.1', {port}), SimpleHTTPRequestHandler).serve_forever()
""")
    start = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    visual, worktree, original, run, task, rev, gate = _capture_inputs(
        monkeypatch, tmp_path, port=port, start=start)

    result = visual.capture_all(object(), run, task, rev, gate, worktree)

    assert result["evidence_status"] == "COMPARABLE", result
    assert (worktree / "site" / "index.html").read_text() == original
    assert (worktree / "generated.out").read_text() == "untracked output"
    assert (worktree / "ignored.out").read_text() == "ignored output"
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["restored_paths"] == ["site/index.html"]


def test_worktree_edit_before_capture_stays_invalid_and_is_not_restored(monkeypatch, tmp_path):
    import shlex
    import sys

    port = _port()
    marker = tmp_path / "server-started"
    script = tmp_path / "start_marker.py"
    script.write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('started')\n")
    start = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    visual, worktree, _original, run, task, rev, gate = _capture_inputs(
        monkeypatch, tmp_path, port=port, start=start)
    changed = "edited after submit, before capture\n"
    (worktree / "site" / "index.html").write_text(changed)

    result = visual.capture_all(object(), run, task, rev, gate, worktree)

    assert result["evidence_status"] == "INVALID_COMPARISON", result
    assert result["cause"] == "worktree changed after submit (stale capture)"
    assert (worktree / "site" / "index.html").read_text() == changed
    assert not marker.exists()


@requires_playwright
def test_matching_capture_passes_with_measured_dom(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port())
    g = _gate(con)
    assert g["verdict"] == "PASS" and g["evidence_status"] == "COMPARABLE", g
    assert _task(con)["status"] == "accepted"
    shots = con.execute("SELECT COUNT(*) FROM evidence WHERE kind='screenshot'").fetchone()[0]
    assert shots >= 8  # 2 viewports x 2 states x (candidate + reference)
    proofs = {(r["harness"], r["model"]): (r["result"], r["details"]) for r in
              con.execute("SELECT * FROM capability_proofs WHERE capability='vision'")}
    route = g["route"]  # the route that judged the capture
    judge = con.execute("SELECT harness, model FROM dispatches WHERE role='visual_reviewer' AND kind='reviewer'").fetchone()
    assert proofs[(judge["harness"], judge["model"])][0] == "pass", proofs
    # First preference (latest Gemini Flash via agy at medium effort), qualified by its own probe.
    assert (judge["harness"], judge["model"]) == ("agy", "gemini-3.8-flash-medium"), dict(judge)
    receipt = con.execute("SELECT path FROM evidence WHERE kind='capture_receipt'").fetchone()[0]
    import json
    data = json.loads(open(receipt).read())
    assert data["measurement_method"] == "dom" and data["reference"]["sha256"].startswith("sha256:")
    assert any(m.get("property") == "width" for f in data["frames"] for m in f["measurements"])


@requires_playwright
def test_broken_interaction_is_a_product_failure_not_invalid(env):
    con, out, wt, wenv = _setup(env, BROKEN, port=_port())
    g = _gate(con)
    assert g["verdict"] == "CHANGES_REQUIRED" and g["evidence_status"] == "COMPARABLE", g
    f = con.execute("SELECT summary FROM findings WHERE gate_kind='visual'").fetchone()[0]
    assert "did not produce nav.open" in f
    assert not [c for c in env.calls() if c["role"] == "visual_reviewer"]  # no judgment spent


@requires_playwright
def test_clipped_element_is_measured_material_drift(env):
    con, out, wt, wenv = _setup(env, CLIPPED, port=_port())
    g = _gate(con)
    assert g["verdict"] == "CHANGES_REQUIRED", g
    rows = [r[0] for r in con.execute("SELECT measurement_json FROM findings WHERE gate_kind='visual'")]
    assert any("viewport clearance" in (r or "") or "scrollWidth" in (r or "") for r in rows), rows


@requires_playwright
def test_wrong_state_is_invalid_comparison_then_blocks_after_one_recapture(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), extra="  auth: [data-test=signed-in]\n")
    g = _gate(con)
    assert g["evidence_status"] == "INVALID_COMPARISON" and g["verdict"] == "UNAVAILABLE", g
    assert g["recaptures"] == 1
    assert _task(con)["status"] == "blocked"


@requires_playwright
def test_no_reference_passes_with_fidelity_unmeasured(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), reference=None)
    g = _gate(con)
    assert g["verdict"] == "PASS" and "fidelity unmeasured" in (g["summary"] or ""), g


@requires_playwright
def test_route_without_image_capability_never_passes(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), probe="PROBE NO_IMAGE")
    g = _gate(con)
    assert g["verdict"] == "UNAVAILABLE", g
    assert _task(con)["status"] == "blocked"
    assert con.execute("SELECT COUNT(*) FROM capability_proofs WHERE result='pass'").fetchone()[0] == 0


@requires_playwright
def test_unrelated_edit_reuses_visual_evidence_but_reference_change_invalidates(env):
    port = _port()
    con, out, wt, wenv = _setup(env, BROKEN, port=port)
    assert _gate(con)["verdict"] == "CHANGES_REQUIRED"
    (wt / "site" / "index.html").write_text(PAGE)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert _gate(con)["verdict"] == "PASS"
    captures_before = con.execute("SELECT COUNT(*) FROM evidence WHERE kind='capture_receipt'").fetchone()[0]
    # A non-presentation edit: the prior visual verdict is reused.
    con.execute("UPDATE leases SET released_at=NULL WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    (wt / "site" / "notes.py").write_text("x = 1\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert "visual evidence reused" in out, out
    assert con.execute("SELECT COUNT(*) FROM evidence WHERE kind='capture_receipt'").fetchone()[0] == captures_before


def test_visual_preflight_rejects_unknown_verbs():
    from office import visual
    tasks = [{"id": "T1", "visual": {"url": "http://127.0.0.1:8000/", "start": "python3 -m http.server",
                                      "states": "default; bad = dance #box"}}]
    errors, warnings = visual.preflight(tasks)
    assert any("unknown verb 'dance'" in e for e in errors), errors

    # Allowed verbs pass
    tasks_ok = [{"id": "T1", "visual": {
        "url": "http://127.0.0.1:8000/", "start": "python3 -m http.server",
        "states": "default; s1 = navigate /sub, click #b, hover #h, type #t txt, wait #w, scroll 100"}}]
    errors_ok, _ = visual.preflight(tasks_ok)
    assert errors_ok == []


def test_visual_preflight_navigate_rejected_and_allowed_forms():
    from office import visual
    base_url = "http://127.0.0.1:8000/site/index.html"

    # Rejected forms: file:, protocol-relative //host, metadata IP, non-local origin, data:, javascript:
    rejected_cases = [
        "file:///Users/u/.ssh/id_rsa",
        "//evil.example/x",
        "http://169.254.169.254/latest/meta-data/",
        "https://internal.corp/",
        "data:text/plain;base64,aGVsbG8=",
        "javascript:alert(1)",
    ]
    for target in rejected_cases:
        tasks = [{"id": "T1", "visual": {
            "url": base_url, "start": "python3 -m http.server",
            "states": f"default; nav = navigate {target}"}}]
        errors, _ = visual.preflight(tasks)
        assert len(errors) == 1, (target, errors)
        assert "invalid navigate" in errors[0], (target, errors)

    # Allowed forms: relative URL and absolute local URL
    allowed_cases = [
        "subpage.html",
        "/site/subpage.html",
        "http://127.0.0.1:8000/site/subpage.html",
        "http://localhost:8000/site/subpage.html",
    ]
    for target in allowed_cases:
        tasks = [{"id": "T1", "visual": {
            "url": base_url, "start": "python3 -m http.server",
            "states": f"default; nav = navigate {target}"}}]
        errors, _ = visual.preflight(tasks)
        assert errors == [], (target, errors)

    # allow_remote_preview allows remote navigate targets
    remote_tasks = [{"id": "T1", "visual": {
        "url": "https://preview.example.com/", "allow_remote_preview": True, "start": "python3 -m http.server",
        "states": "default; nav = navigate https://preview.example.com/other"}}]
    errors, _ = visual.preflight(remote_tasks)
    assert errors == []


@requires_playwright
def test_playwright_empty_error_splitlines_does_not_raise(monkeypatch, tmp_path):
    from office import visual

    class DummyPage:
        url = "http://127.0.0.1:8000/"

        def on(self, *args, **kwargs):
            pass

        def goto(self, *args, **kwargs):
            return None

        def evaluate(self, *args, **kwargs):
            return True

        def query_selector(self, sel):
            return object()

        def click(self, *args, **kwargs):
            from playwright.sync_api import Error as PWError
            raise PWError("")

        def wait_for_timeout(self, *args):
            pass

    class DummyContext:
        def new_page(self):
            return DummyPage()

        def close(self):
            pass

    class DummyBrowser:
        def new_context(self, **kwargs):
            return DummyContext()

    st = {"name": "test-state", "steps": [{"verb": "click", "arg": "#btn"}]}
    evdir = tmp_path / "evidence"
    evdir.mkdir()
    res = visual._capture_one(DummyBrowser(), "http://127.0.0.1:8000/", 1440, 900, st, [], evdir, "test", {})
    assert "failures" in res
    assert res["failures"][0]["summary"] == "interaction 'click #btn' failed: "


@requires_playwright
def test_capture_one_navigate_rejected_and_allowed_forms(tmp_path):
    from office import visual

    navigated_urls = []

    class MockPage:
        def __init__(self):
            self.url = ""

        def on(self, *args, **kwargs):
            pass

        def goto(self, target, *args, **kwargs):
            navigated_urls.append(target)
            self.url = target
            return None

        def evaluate(self, *args, **kwargs):
            return {"innerWidth": 1440, "innerHeight": 900, "scrollWidth": 1440, "fontStatus": "loaded",
                    "failedFonts": [], "elements": {}}

        def query_selector(self, sel):
            return object()

        def wait_for_timeout(self, *args):
            pass

        def screenshot(self, path, **kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"x" * 2000)

    class MockContext:
        def new_page(self):
            return MockPage()

        def close(self):
            pass

    class MockBrowser:
        def new_context(self, **kwargs):
            return MockContext()

    evdir = tmp_path / "ev"
    evdir.mkdir()
    base_url = "http://127.0.0.1:8000/site/index.html"

    # Rejected forms in _capture_one produce cause: "spec" and invalid
    rejected_cases = [
        "file:///Users/u/.ssh/id_rsa",
        "//evil.example/x",
        "http://169.254.169.254/latest/meta-data/",
        "https://internal.corp/",
        "data:text/plain;base64,aGVsbG8=",
        "javascript:alert(1)",
    ]
    for target in rejected_cases:
        st = {"name": "test", "steps": [{"verb": "navigate", "arg": target}]}
        res = visual._capture_one(MockBrowser(), base_url, 1440, 900, st, [], evdir, "test", {})
        assert res.get("cause") == "spec", (target, res)
        assert "invalid navigate" in res.get("invalid", ""), (target, res)
        # Ensure page.goto was never called for rejected targets
        assert target not in navigated_urls

    # Reference capture leaves reference origin rejected
    ref_base = "file:///path/to/design/proto.html"
    ref_st_out = {"name": "test", "steps": [{"verb": "navigate", "arg": "http://127.0.0.1:8000/x"}]}
    res_ref = visual._capture_one(MockBrowser(), ref_base, 1440, 900, ref_st_out, [], evdir, "ref-test", {}, reference=True)
    assert res_ref.get("cause") == "spec"
    assert "leaves reference origin" in res_ref.get("invalid", "")

    # Allowed relative and absolute local URLs
    st_rel = {"name": "test", "steps": [{"verb": "navigate", "arg": "subpage.html"}]}
    res_rel = visual._capture_one(MockBrowser(), base_url, 1440, 900, st_rel, [], evdir, "test-rel", {})
    assert "invalid" not in res_rel
    assert res_rel["url"] == "http://127.0.0.1:8000/site/subpage.html"

    st_abs = {"name": "test", "steps": [{"verb": "navigate", "arg": "http://127.0.0.1:8000/other"}]}
    res_abs = visual._capture_one(MockBrowser(), base_url, 1440, 900, st_abs, [], evdir, "test-abs", {})
    assert "invalid" not in res_abs
    assert res_abs["url"] == "http://127.0.0.1:8000/other"


@requires_playwright
def test_navigate_resolves_relative_url_and_records_frame_url(env):
    port = _port()
    sub_page = PAGE.replace("Acme", "Subpage Acme")
    extra = "  states: default; sub = navigate subpage.html, click [data-test=menu] -> expect nav.open\n"
    env.trust()
    (env.repo / "design").mkdir(exist_ok=True)
    (env.repo / "design" / "prototype.html").write_text(PAGE)
    env.git("add", "-A")
    env.git("commit", "-qm", "reference")
    env.script(probe=[{"reply": "auto"}],
               visual_reviewer=[{"reply": "EVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS"}])
    code, out = env.office("start", "nav", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan(port, None, extra))
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree FROM dispatches WHERE role='executor'").fetchone())
    from pathlib import Path
    wt = Path(d["worktree"])
    (wt / "site").mkdir(exist_ok=True)
    (wt / "site" / "index.html").write_text(PAGE)
    (wt / "site" / "subpage.html").write_text(sub_page)
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"]}
    code, out = env.office("submit", cwd=wt, env=wenv)

    g = _gate(con)
    assert g["verdict"] == "PASS", (g, out)
    receipt_row = con.execute("SELECT path FROM evidence WHERE kind='capture_receipt'").fetchone()
    import json
    receipt_data = json.loads(Path(receipt_row[0]).read_text())
    frames_by_state = {f["state"]: f for f in receipt_data["frames"]}
    assert "default" in frames_by_state
    assert "sub" in frames_by_state
    assert frames_by_state["default"]["url"] == f"http://127.0.0.1:{port}/site/index.html"
    assert frames_by_state["sub"]["url"] == f"http://127.0.0.1:{port}/site/subpage.html"


@requires_playwright
def test_identical_non_default_screenshot_reported_as_cause_capture(env):
    # Non-default state where action does nothing, producing identical screenshot to default
    port = _port()
    # State 'noop' scrolls 0, producing exact same screenshot as default
    plan = _plan(port, reference=None, extra="""  states: default; noop = scroll 0
""")
    env.trust()
    (env.repo / "design").mkdir(exist_ok=True)
    (env.repo / "design" / "prototype.html").write_text(PAGE)
    env.git("add", "-A")
    env.git("commit", "-qm", "reference")
    code, out = env.office("start", "identical-test", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(plan)
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree FROM dispatches WHERE role='executor'").fetchone())
    from pathlib import Path
    wt = Path(d["worktree"])
    (wt / "site").mkdir(exist_ok=True)
    (wt / "site" / "index.html").write_text(PAGE)
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"]}
    code, out = env.office("submit", cwd=wt, env=wenv)

    g = _gate(con)
    # Identical screenshot produces cause: capture -> UNAVAILABLE, not CHANGES_REQUIRED
    assert g["verdict"] == "UNAVAILABLE"
    assert g["evidence_status"] == "INVALID_COMPARISON"
    assert "identical to default" in g["summary"]
    # Task is blocked, not changes_required on the producer
    assert _task(con)["status"] == "blocked"


@requires_playwright
def test_absent_selector_reported_as_cause_spec(env):
    # Action selector does not exist on page
    port = _port()
    plan = _plan(port, reference=None, extra="""  states: default; missing-btn = click [data-test=nonexistent]
""")
    env.trust()
    (env.repo / "design").mkdir(exist_ok=True)
    (env.repo / "design" / "prototype.html").write_text(PAGE)
    env.git("add", "-A")
    env.git("commit", "-qm", "reference")
    code, out = env.office("start", "absent-selector-test", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(plan)
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree FROM dispatches WHERE role='executor'").fetchone())
    from pathlib import Path
    wt = Path(d["worktree"])
    (wt / "site").mkdir(exist_ok=True)
    (wt / "site" / "index.html").write_text(PAGE)
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"]}
    code, out = env.office("submit", cwd=wt, env=wenv)

    g = _gate(con)
    # Absent selector produces cause: spec -> UNAVAILABLE, not CHANGES_REQUIRED
    assert g["verdict"] == "UNAVAILABLE"
    assert g["evidence_status"] == "INVALID_COMPARISON"
    assert "selector '[data-test=nonexistent]' absent" in g["summary"]
    assert _task(con)["status"] == "blocked"

