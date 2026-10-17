"""#664: native dropdowns and comma-bearing selectors are representable in visual states."""
import sys
from types import ModuleType

import pytest

from office import visual


def test_css_function_comma_is_not_a_step_boundary():
    data = visual.states({"states": "default; selected = click :is(.one, .two), wait main"})
    assert data[1]["steps"] == [
        {"verb": "click", "arg": ":is(.one, .two)"},
        {"verb": "wait", "arg": "main"},
    ]


def test_selector_list_comma_is_not_a_step_boundary():
    data = visual.states({"states": "chosen = hover #first, #second, wait main"})
    assert data[0]["steps"] == [
        {"verb": "hover", "arg": "#first, #second"},
        {"verb": "wait", "arg": "main"},
    ]


def test_double_semicolon_is_a_step_boundary_not_a_state_boundary():
    data = visual.states({"states": "default; chosen = select #room vip ;; click button:has-text('Enter') -> expect main"})
    assert len(data) == 2
    assert data[1]["name"] == "chosen"
    assert data[1]["steps"] == [
        {"verb": "select", "arg": "#room vip"},
        {"verb": "click", "arg": "button:has-text('Enter')"},
    ]
    assert data[1]["expect"] == "main"


def test_legacy_comma_separated_steps_still_work():
    data = visual.states({"states": "picked = click #a, type #b text, wait main, scroll 100"})
    assert [step["verb"] for step in data[0]["steps"]] == ["click", "type", "wait", "scroll"]


@pytest.mark.parametrize("choice,mode", [("vip", "value"), ("label:Big Heroes", "label"),
                                         ("Big Heroes", "fallback-label")])
def test_native_select_step_operates_real_page_contract(tmp_path, monkeypatch, choice, mode):
    """Fake browser ensures the capture pipeline invokes Playwright select_option."""
    calls = []
    class FakePWError(Exception):
        pass
    class Page:
        url = "http://localhost:5000"
        def on(self, *args): pass
        def goto(self, url, **kwargs):
            calls.append(("goto", url))
            return type("Response", (), {"status": 200})()
        def evaluate(self, expr, arg=None):
            if isinstance(expr, str) and expr.startswith("document.fonts"):
                return True
            if isinstance(expr, str) and "innerWidth" in expr:
                return {"innerWidth": 390, "scrollWidth": 390, "failedFonts": [], "elements": {}}
            return None
        def query_selector(self, selector):
            calls.append(("query", selector))
            return object()
        def select_option(self, selector, **kw):
            calls.append(("select_option", selector, kw))
            if mode == "fallback-label" and "value" in kw:
                raise FakePWError("option value not found")
            return [choice]
        def wait_for_timeout(self, *args): pass
        def screenshot(self, path, **kw):
            from pathlib import Path
            Path(path).write_bytes(b"X" * 1600)
    class Context:
        def new_page(self): return Page()
        def close(self): calls.append(("close",))
    class Browser:
        def new_context(self, **kw): return Context()
    # Run _capture_one's actual select control path without a real browser.
    fake = ModuleType("playwright.sync_api")
    fake.Error = FakePWError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    st = visual.states({"states": f"chosen = select #room {choice}"})[0]
    out = visual._capture_one(Browser(), "http://localhost:5000", 390, 844,
                              st, [], tmp_path, f"room-{mode}", {})
    assert not out.get("invalid") and not out.get("failures"), out
    options = [c for c in calls if c[0] == "select_option"]
    assert len(options) == (2 if mode == "fallback-label" else 1)
    assert all(c[1] == "#room" for c in options)
    key = "label" if mode in ("label", "fallback-label") else "value"
    selected = "Big Heroes" if mode in ("label", "fallback-label") else "vip"
    assert options[-1][2][key] == selected, options


def test_allow_select_verb_in_visual_state_contract():
    assert "select" in visual.ALLOWED_VERBS


def test_css_attribute_commas_and_quoted_contents_remain_one_selector():
    data = visual.states({"states": 'chosen = click button[data-label="yes, pick"], wait main'})
    assert data[0]["steps"] == [
        {"verb": "click", "arg": 'button[data-label="yes, pick"]'},
        {"verb": "wait", "arg": "main"},
    ]


def test_quote_closed_after_escaped_backslash_allows_next_step_boundary():
    bs = chr(92) * 2  # value ends in an escaped backslash, so the quote closes
    sel = f'[data-x="a{bs}"]'
    data = visual.states({"states": f"chosen = click {sel}, wait main ;; click #go"})
    assert data[0]["steps"] == [
        {"verb": "click", "arg": sel},
        {"verb": "wait", "arg": "main"},
        {"verb": "click", "arg": "#go"},
    ]


def test_escaped_quote_keeps_comma_inside_selector():
    sel = '[data-x="a' + chr(92) + '", b"]'
    data = visual.states({"states": f"chosen = click {sel}, wait main"})
    assert data[0]["steps"][0] == {"verb": "click", "arg": sel}


def test_select_option_explicit_label_supports_selector_spaces():
    assert visual._select_target('[data-name="Kids Room"] label:Big Heroes') == (
        '[data-name="Kids Room"]', "Big Heroes", "label")


def test_select_needs_both_selector_and_option_at_submit(monkeypatch):
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)
    tasks = [{"id": "T1", "visual": {"url": "http://localhost:3456", "start": "serve",
                                    "states": "chosen = select #room"}}]
    errors, warnings = visual.preflight(tasks)
    assert len(errors) == 1 and "selector and option" in errors[0]
    assert not warnings


@pytest.mark.integration
def test_real_chromium_select_changes_page_state_and_screenshots(tmp_path):
    """A real browser proves select_option reaches the intended visible state."""
    playwright = pytest.importorskip("playwright.sync_api")
    page = tmp_path / "index.html"
    page.write_text("""<!doctype html><html><head><style>
      body{font:16px Arial;background:#fafafa;color:#222;padding:24px}
      main{border:1px solid #aaa;padding:20px;margin:15px}
      main.ready{background:#ddeedd}
    </style></head><body>
      <select id=room><option value=''>Choose a room</option>
        <option value='champions'>Champions</option>
        <option value='big'>Big Heroes</option></select>
      <main id=result>No room chosen</main>
      <script>document.querySelector('#room').onchange = e => {
        const main=document.querySelector('main');
        main.classList.add('ready');
        main.textContent=e.target.selectedOptions[0].label;
      };</script>
    </body></html>""")
    spec = {"states": "default; chosen = select #room label:Big Heroes -> expect main.ready",
            "viewports": "mobile", "selectors": "main"}
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**visual.launch_args())
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright browser not installed (install chromium-headless-shell)")
            raise
        try:
            result = visual._capture_frames(browser, page.as_uri(), spec, tmp_path, None)
        finally:
            browser.close()
    default, chosen = result["frames"]
    assert default["state"] == "default" and chosen["state"] == "chosen"
    assert chosen.get("screenshot") and not chosen.get("invalid") and not chosen.get("failures"), chosen
    assert chosen.get("screenshot") != default.get("screenshot")
    assert (tmp_path / "mobile-chosen.png").stat().st_size > 1200
