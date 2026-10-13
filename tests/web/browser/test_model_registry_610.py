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
    # The test server keeps an SSE stream open; DOM readiness plus the actual
    # issue-row/registry assertions is the deterministic navigation gate.
    page.goto(url, wait_until="domcontentloaded")
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
    expect(rows.first.get_by_role("button", name="Inspect")).to_have_attribute("aria-pressed", "true")
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


def test_grid_compares_named_effort_benchmark_provenance_and_factual_state(page, registry_site):
    from office.web import model_registry

    url, _ = registry_site
    page.set_viewport_size({"width": 1500, "height": 950})
    errors = navigate(page, url)
    headings = page.locator(".model-table thead").inner_text()
    for heading in ("Benchmark sample", "Provenance", "Factual availability", "My policy"):
        assert heading in headings
    record = next(g for g in model_registry.view()["generations"] if g["model_id"] == "gpt-6.1-sol")
    sample = next(e for e in record["efforts"] if e["effort"] == "medium")
    row = page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]').first
    bench = row.locator('[data-testid=model-grid-benchmark]')
    expect(bench).to_contain_text(f'{sample["benchmark_score"]} · medium')
    expect(bench).to_contain_text(sample["benchmark_index"])
    provenance = row.locator('[data-testid=model-grid-provenance]')
    expect(provenance).to_contain_text(sample["source_name"])
    expect(provenance).to_contain_text(sample["source_identifier"])
    expect(provenance).to_contain_text(sample["source_metadata"]["fetched_at"][:10])
    assert provenance.locator("a").get_attribute("href") == sample["source_metadata"]["url"]
    fact = row.locator('[data-testid=model-grid-factual]')
    assert fact.inner_text().strip() and "per-effort" in fact.inner_text().lower()
    assert row.locator('[data-testid=model-grid-policy]').inner_text() == "Allowed by you"
    assert row.locator("th[scope=row]").evaluate("el => getComputedStyle(el).position") == "sticky"
    wrap = page.locator(".model-table-wrap")
    assert wrap.evaluate("el => el.scrollWidth > el.clientWidth")
    wrap.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    assert abs(row.locator("th[scope=row]").bounding_box()["x"] - wrap.bounding_box()["x"]) < 4
    assert errors == []


def test_registry_filters_independent_policy_from_runtime_facts(page, registry_site):
    import os
    from pathlib import Path

    url, _ = registry_site
    Path(os.environ["OFFICE_USER_CONFIG"]).write_text(
        "routing:\n  user_policy:\n    version_ceilings:\n      openai/gpt-sol: '5.6'\n")
    page.set_viewport_size({"width": 1500, "height": 950})
    errors = navigate(page, url)
    policy = page.locator('[data-testid=model-policy-filter]')
    factual = page.locator('[data-testid=model-factual-filter]')
    policy.select_option("denied")
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-5.6-sol"]')).to_have_count(1)
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]')).to_have_count(0)
    policy.select_option("allowed")
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-5.6-sol"]')).to_have_count(0)
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]')).to_have_count(1)
    policy.select_option("")  # factual filters must operate independently of user denials
    facts = factual.evaluate("el => [...el.options].filter(o => o.value).map(o => ({value:o.value,label:o.textContent}))")
    assert facts, "At least one factual state must be filterable independently"
    factual.select_option(facts[0]["value"])
    # ctx.rerender replaces the grid after the select event; wait for the new
    # rows before taking a static locator snapshot with all().
    factual_cells = page.locator('[data-testid=model-grid-factual]')
    expect(factual_cells.first).to_contain_text(facts[0]["label"], timeout=5000)
    cells = factual_cells.all()
    assert cells, "Selecting an available factual state should find a model"
    for cell in cells:
        assert facts[0]["label"] in cell.inner_text()
    assert errors == []


def test_mobile_no_match_is_visible_and_cards_show_evidence_without_expansion(page, registry_site):
    url, _ = registry_site
    page.set_viewport_size({"width": 390, "height": 844})
    errors = navigate(page, url)
    card = page.locator('[data-testid=model-registry-card][data-model="gpt-6.1-sol"]').first
    expect(card).to_be_visible()
    assert "Benchmark" in card.inner_text()
    assert "Source:" in card.inner_text()
    assert "Allowed by you" in card.inner_text()
    page.locator('[data-testid=model-search]').fill("nonexistent-model-9999")
    expect(page.locator('[data-testid=model-empty]')).to_be_visible()
    expect(page.locator('[data-testid=model-registry-card]')).to_have_count(0)
    page.locator('[data-testid=model-search]').fill("")
    expect(card).to_be_visible()
    policy = page.locator('[data-testid=model-policy-filter]')
    policy.select_option("denied")
    expect(page.locator('[data-testid=model-empty]')).to_be_visible()
    policy.select_option("")
    expect(card).to_be_visible()
    dims = page.locator('[data-testid=model-registry]').evaluate(
        "el => ({width:el.getBoundingClientRect().width,screen:window.innerWidth})")
    assert dims["width"] <= dims["screen"] + 2
    assert errors == []


def test_scoped_manual_only_is_not_mislabeled_fully_allowed_or_globally_manual(page, registry_site):
    import os
    from pathlib import Path

    url, _ = registry_site
    Path(os.environ["OFFICE_USER_CONFIG"]).write_text(
        "routing:\n  user_policy:\n    overkill_rules:\n      - route: codex/gpt-6.1-sol\n        roles: [executor]\n")
    page.set_viewport_size({"width": 1500, "height": 950})
    errors = navigate(page, url)
    row = page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]').first
    expect(row.locator('[data-testid=model-grid-policy]')).to_contain_text("Scoped manual-only by you")
    page.locator('[data-testid=model-policy-filter]').select_option("scoped-manual")
    expect(page.locator('[data-testid=model-registry-row]')).to_have_count(1)
    expect(page.locator('[data-testid=model-registry-row][data-model="gpt-6.1-sol"]')).to_have_count(1)
    assert errors == []
