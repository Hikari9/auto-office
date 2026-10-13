"""Browser contract for the #606-approved grid/inspector + mobile cards in #610."""
from __future__ import annotations

import json
import threading

import pytest
from playwright.sync_api import expect

from office.web import server


@pytest.fixture
def registry_site(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def navigate(page, url):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.locator(".product-rail").get_by_text("Settings").click()
    page.wait_for_selector("[data-testid=model-registry]", timeout=10000)
    return errors


def test_desktop_grid_and_persistent_inspector_use_distinct_policy_and_facts(page, registry_site):
    url, _ = registry_site
    page.set_viewport_size({"width": 1500, "height": 950})
    errors = navigate(page, url)
    table = page.locator(".model-table")
    assert table.is_visible()
    rows = page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]')
    assert rows.count() >= 1
    rows.first.get_by_role("button", name="Inspect").click()
    inspector = page.locator("[data-testid=model-inspector]")
    assert inspector.is_visible()
    expect(inspector).to_contain_text("gpt-6.1-sol", timeout=5000)
    assert "Allowed by you" in inspector.text_content()
    assert "Unknown — not probed" in inspector.text_content()
    assert "Effort-specific benchmark" in inspector.text_content()
    page.locator('[data-testid=model-search]').fill("luna")
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]')).to_have_count(0, timeout=5000)
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-6-luna"]')).not_to_have_count(0, timeout=5000)
    page.locator('[data-testid=model-search]').fill("")
    assert errors == []


def test_narrow_mobile_uses_expandable_cards_with_no_required_horizontal_scroll(page, registry_site):
    url, _ = registry_site
    page.set_viewport_size({"width": 390, "height": 844})
    errors = navigate(page, url)
    page.locator(".model-mobile-cards").wait_for(state="visible")
    assert not page.locator(".model-table").is_visible()
    card = page.locator('[data-testid=model-registry-card][data-model="gpt-6.1-sol"]').first
    card.get_by_role("button", name="Expand evidence & policy").click()
    expect(card.locator('[data-testid=model-card-evidence]')).to_be_visible(timeout=5000)
    assert "Allowed by you" in card.text_content()
    assert "Live quota" in card.text_content()
    card.get_by_role("button", name="Collapse").click()
    expect(card.locator('[data-testid=model-card-evidence]')).to_have_count(0, timeout=5000)
    dims = page.locator('[data-testid=model-registry]').evaluate(
        "(el) => ({width:el.getBoundingClientRect().width,screen:window.innerWidth})")
    assert dims["width"] <= dims["screen"] + 2
    assert errors == []


def test_confirmed_global_deny_uses_settings_command_and_never_optimistically_mutates(page, registry_site):
    url, svc = registry_site
    page.set_viewport_size({"width": 1500, "height": 950})
    sent = []
    page.on("request", lambda req: sent.append(json.loads(req.post_data)) if
            req.url.endswith("/api/commands") and req.method == "POST" else None)
    errors = navigate(page, url)
    page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]').first.get_by_role(
        "button", name="Inspect").click()
    inspector = page.locator('[data-testid=model-inspector]')
    inspector.get_by_role("button", name="Deny this model").click()
    confirm = page.locator("[data-testid=model-confirm]")
    expect(confirm).to_be_visible(timeout=5000)
    assert "all efforts" in confirm.text_content()
    assert not sent  # not mutated until user explicitly confirms
    confirm.get_by_role("button", name="Confirm").click()
    page.wait_for_timeout(300)
    assert len(sent) == 1
    request = sent[0]
    assert request["kind"] == "settings_set"
    assert request["target"] == {"tier": "machine", "key": "routing.user_policy.denied_models"}
    assert request["payload"]["value"] == ["codex/gpt-6.1-sol"]
    # This fixture's command executor records a receipt, but never changes config.
    # The UI must continue showing the re-read server value, not a fake success.
    page.wait_for_timeout(450)
    assert "Allowed by you" in inspector.text_content()
    assert errors == []
