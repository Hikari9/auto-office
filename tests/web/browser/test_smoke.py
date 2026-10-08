"""The served page loads in a real browser without console errors."""
from __future__ import annotations


def test_page_loads_without_console_errors(page, web_url):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(web_url)
    page.wait_for_function("() => document.querySelector('[data-testid=rev]').textContent !== '-'", timeout=10000)
    assert page.text_content("[data-testid=fixture-marker]") == "FIXTURE MODE (small)"
    assert page.text_content("[data-testid=office-freshness]") == "live"
    assert errors == []
