"""Routing inspection in a real browser: compact recorded decision, evidence drawer, legacy runs."""
from __future__ import annotations

import threading

import pytest

from office.web import server


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def open_agents(page, url):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.click("[data-surface=agents]")
    page.wait_for_selector("[data-testid=agent-node]", timeout=10000)
    page.check("[data-testid=agents-include-completed]")  # these tests may target completed agents


def inspect_task(page, svc, predicate):
    """Open the inspector of the current executor node whose task's route satisfies `predicate`; return (node, route)."""
    e = svc.snapshot_state["entities"]
    node = next(a for a in e["agents"].values() if a["column"] == "executors" and a.get("task")
                and predicate(e["tasks"][a["task"]]["route"]))
    page.locator(f'[data-testid=agent-node][data-id="{node["id"]}"]').click()
    page.wait_for_selector(f'[data-testid=agent-inspector][data-id="{node["id"]}"]', timeout=5000)
    return node, e["tasks"][node["task"]]["route"]


def text(page, testid):
    return page.text_content(f"[data-testid={testid}]")


def test_compact_view_shows_recorded_decision_and_fallback_taken(page, served):
    url, svc = served
    open_agents(page, url)
    _, route = inspect_task(page, svc, lambda r: r["available"] and r["fallbacks_taken"])
    assert text(page, "route-primary") == route["primary"]
    assert text(page, "route-fallbacks") == ", ".join(route["fallbacks"])
    assert text(page, "route-reason") == route["reason"]
    assert text(page, "route-strength") == route["strength"]
    assert text(page, "route-weakness") == route["weakness"]
    taken = route["fallbacks_taken"][0]
    assert f"{taken['route']} ({taken['reason']})" in text(page, "route-fallback-taken")
    assert not page.is_visible("[data-testid=route-audits]")  # evidence stays in the closed drawer


def test_no_fallback_taken_says_none(page, served):
    url, svc = served
    open_agents(page, url)
    inspect_task(page, svc, lambda r: r["available"] and not r["fallbacks_taken"])
    assert text(page, "route-fallback-taken") == "none"


def test_evidence_drawer_has_audits_provenance_override_and_history(page, served):
    url, svc = served
    open_agents(page, url)
    node, route = inspect_task(page, svc, lambda r: r["available"] and r["planner_override"])
    page.click("[data-testid=routing-evidence] summary")
    rows = page.locator("[data-testid=route-audits] tbody tr")
    assert rows.count() == len(route["audits"])
    assert rows.first.locator("td").first.text_content() == route["audits"][0]["id"]
    assert "runs.db route_audit" in text(page, "route-provenance")
    assert route["planner_override"]["why"] in text(page, "route-override")


def test_override_absent_says_none(page, served):
    url, svc = served
    open_agents(page, url)
    inspect_task(page, svc, lambda r: r["available"] and not r["planner_override"])
    page.click("[data-testid=routing-evidence] summary")
    assert text(page, "route-override") == "Planner override: none"


def test_legacy_run_shows_no_routing_audit_recorded(page, served):
    url, svc = served
    open_agents(page, url)
    inspect_task(page, svc, lambda r: not r["available"])
    assert text(page, "routing-legacy") == "no routing audit recorded"
    assert page.locator("[data-testid=route-primary], [data-testid=routing-evidence]").count() == 0


def test_route_history_lists_earlier_dispatches_of_the_task(page, served):
    url, svc = served
    open_agents(page, url)
    e = svc.snapshot_state["entities"]
    node = next(a for a in e["agents"].values() if a["column"] == "executors" and a.get("task")
                and e["tasks"][a["task"]]["route"]["available"]
                and any(d["task"] == a["task"] for d in e["runs"][a["run"]]["history"]))
    earlier = [d for d in e["runs"][node["run"]]["history"] if d["task"] == node["task"]]
    page.locator(f'[data-testid=agent-node][data-id="{node["id"]}"]').click()
    page.wait_for_selector(f'[data-testid=agent-inspector][data-id="{node["id"]}"]', timeout=5000)
    page.click("[data-testid=routing-evidence] summary")
    items = page.locator("[data-testid=route-history] li").all_text_contents()
    assert len(items) == len(earlier) > 0
    for text_, d in zip(items, earlier):
        assert f"{d['harness']}/{d['model']}@{d['effort']}" in text_ and d["started_at"] in text_
