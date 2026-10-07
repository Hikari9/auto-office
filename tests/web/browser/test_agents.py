"""The Agents surface in a real browser: role columns, edges, filters, evidence, worker handoff, route change, layout."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from office.web import server

COLUMNS = ["orchestrators", "plan_reviewers", "executors", "code_reviewers", "visual_verifiers"]


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    svc = server.build_fixture("small", home=tmp_path / "fx", seed_receipts=True).start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def open_agents(page, url, width=1440, height=900):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.click("[data-surface=agents]")
    page.wait_for_selector("[data-testid=agent-node]", timeout=10000)
    return errors


def node(page, node_id):
    return page.locator(f'[data-testid=agent-node][data-id="{node_id}"]')


def inspect(page, node_id):
    node(page, node_id).click()
    page.wait_for_selector(f'[data-testid=agent-inspector][data-id="{node_id}"]', timeout=5000)
    return page.locator("[data-testid=agent-inspector]")


def wait_ids(page, expected):
    """Wait until the graph shows exactly these node ids."""
    page.wait_for_function("""(ids) => { const got = [...document.querySelectorAll('[data-testid=agent-node]')]
        .map(n => n.dataset.id).sort(); return JSON.stringify(got) === JSON.stringify(ids); }""",
                           arg=sorted(expected), timeout=5000)


def pick(page, js):
    """The id of the first agent entity for which the JS predicate `a => ...` holds."""
    return page.evaluate(f"Object.values(window.officeStore.state.entities.agents).find({js}).id")


def test_five_role_columns_current_topology_and_edges(page, served):
    url, svc = served
    errors = open_agents(page, url)
    cols = page.locator("[data-testid=role-column]")
    assert [cols.nth(i).get_attribute("data-column") for i in range(cols.count())] == COLUMNS
    titles = page.locator(".col-title").all_text_contents()
    assert [t.split(" (")[0] for t in titles] == ["Orchestrators", "Plan Reviewers", "Executors", "Code Reviewers",
                                                 "Visual Verifiers"]
    agents = svc.snapshot_state["entities"]["agents"]
    shown = page.locator("[data-testid=agent-node]").count()
    assert shown == sum(1 for a in agents.values() if a["column"] in COLUMNS)
    # Historical dispatches are not nodes: they live in run history only.
    history = [d["id"] for r in svc.snapshot_state["entities"]["runs"].values() for d in r["history"]]
    assert history
    for hid in history[:5]:
        assert node(page, hid).count() == 0
    # Edges join each orchestrator to its own run's agents, and nothing else.
    page.wait_for_selector("[data-testid=agent-edge]", state="attached", timeout=5000)
    edges = page.eval_on_selector_all("[data-testid=agent-edge]", "es => es.map(e => [e.dataset.from, e.dataset.to])")
    expected = {(o, a) for o, oa in agents.items() if oa["column"] == "orchestrators"
                for a, aa in agents.items() if aa["run"] == oa["run"] and aa["column"] in COLUMNS[1:]}
    assert {tuple(e) for e in edges} == expected and expected
    assert errors == []


def test_repository_and_state_filters(page, served):
    url, svc = served
    open_agents(page, url)
    runs = svc.snapshot_state["entities"]["runs"]
    agents = svc.snapshot_state["entities"]["agents"].values()
    key = "repo:github.com/synth-org-0/repo-00"
    in_repo = {a["id"] for a in agents if runs[a["run"]]["repo"]["key"] == key and a["column"] in COLUMNS}
    terminal = {a["id"] for a in agents if runs[a["run"]]["liveness"] == "terminal" and a["column"] in COLUMNS}
    assert in_repo and terminal and len(in_repo) < len(list(agents))
    page.select_option("[data-testid=agents-repo]", key)
    wait_ids(page, in_repo)
    page.select_option("[data-testid=agents-repo]", "all")
    page.select_option("[data-testid=agents-state]", "terminal")
    wait_ids(page, terminal)


def test_node_evidence_and_telemetry_are_labelled_never_zero(page, served):
    url, _ = served
    open_agents(page, url)
    nid = pick(page, "a => a.state.reply_written_awaiting_ingestion")
    card = node(page, nid)
    assert "Reply written, awaiting ingestion: yes" in card.text_content()
    for m in ("CPU", "RAM", "CONTEXT", "QUOTA"):
        assert f"{m} unknown" in card.text_content()
    insp = inspect(page, nid)
    labels = insp.locator("[data-testid=agent-evidence] dt").all_text_contents()
    assert labels == ["Liveness", "Activity", "Paused", "Blocked", "Quota wait", "Reply written, awaiting ingestion",
                      "Complete", "Stale", "Unavailable"]
    assert insp.locator("[data-testid=ev-reply]").text_content() == "yes"
    for m in ("cpu", "ram", "context", "quota"):
        assert insp.locator(f"[data-testid=tm-{m}]").text_content() in ("unknown", "unavailable")
    assert " 0" not in insp.locator("[data-testid=agent-telemetry]").text_content()
    paused = pick(page, "a => a.state.paused && !a.state.complete")
    assert "Paused: yes" in node(page, paused).text_content()
    blocked = pick(page, "a => a.state.blocked && !a.state.complete")
    assert "Blocked: yes" in node(page, blocked).text_content()


def test_measured_metric_is_shown_as_value(page, served):
    url, svc = served
    open_agents(page, url)
    nid = pick(page, "a => a.column === 'executors' && !a.state.complete")
    page.evaluate("""(id) => { const a = window.officeStore.state.entities.agents[id];
        a.telemetry.cpu = {status: 'measured', value: 0, unit: 'percent'};
        a.telemetry.ram = {status: 'unavailable', value: null, unit: 'bytes'};
        window.officeStore.emit('delta'); }""", nid)
    page.wait_for_function("(id) => document.querySelector(`[data-id='${id}'] [data-metric=ram]`).textContent === 'RAM unavailable'", arg=nid)
    assert node(page, nid).locator("[data-metric=cpu]").text_content() == "CPU 0%"  # a measured 0 is a real value


def test_workers_have_no_text_entry_and_offer_herdr_handoff(page, served):
    url, _ = served
    open_agents(page, url)
    for column in COLUMNS[1:4]:
        nid = pick(page, f"a => a.column === '{column}'")
        inspect(page, nid)
        insp = page.locator("[data-testid=agent-inspector]")
        insp.wait_for()
        assert insp.locator("textarea, input[type=text], input:not([type])").count() == 0
        assert insp.locator("[data-testid=chat]").count() == 0
        raw = nid.removeprefix("dispatch:")
        assert insp.locator("[data-testid=handoff-command]").text_content() == f"herdr agent attach office-{raw.lower()}"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("[data-testid=handoff-copy]")
    assert page.evaluate("navigator.clipboard.readText()").startswith("herdr agent attach office-")


def test_inspector_run_history_lists_earlier_attempts(page, served):
    url, svc = served
    open_agents(page, url)
    run = next(r for r in svc.snapshot_state["entities"]["runs"].values() if r["history"])
    oid = pick(page, f"a => a.run === '{run['id']}' && a.column === 'orchestrators'")
    inspect(page, oid)
    items = page.eval_on_selector_all("[data-testid=history-item]", "is => is.map(i => i.dataset.id)")
    assert items == [d["id"] for d in run["history"]]


def _live_executor(svc):
    snap = svc.snapshot_state["entities"]
    return next(a for a in snap["agents"].values() if a["column"] == "executors" and a["state"]["process"] != "exited"
                and not a["state"]["complete"] and snap["runs"][a["run"]]["controls"]["change_route"]["allowed"])


def test_route_change_keeps_harness_and_shows_new_route_only_once_recorded(page, served):
    url, svc = served
    open_agents(page, url)
    a = _live_executor(svc)
    sent = []

    def accept(route):
        sent.append(json.loads(route.request.post_data))
        body = sent[-1]
        route.fulfill(status=202, content_type="application/json", body=json.dumps(
            {"ok": True, "receipt": {"id": body["id"], "kind": body["kind"], "status": "running", "error": None}}))
    page.route("**/api/commands", accept)
    inspect(page, a["id"])
    ctl = page.locator("[data-testid=route-change]")
    assert ctl.locator("[data-testid=route-harness]").text_content() == a["harness"]
    assert ctl.locator("select").count() == 2  # model and effort only: no harness choice, no free text
    new_effort = "low" if a["effort"] != "low" else "max"
    page.select_option("[data-testid=route-effort]", new_effort)
    page.click("[data-testid=route-apply]")
    page.wait_for_selector("[data-testid=route-pending-detail]")
    assert len(sent) == 1
    cmd = sent[0]
    assert cmd["kind"] == "change_route"
    assert cmd["target"]["dispatch_id"] == a["id"].removeprefix("dispatch:")
    assert cmd["payload"]["route"] == f"{a['harness']}/{a['model']}@{new_effort}"
    assert cmd["expect"]["route"] == f"{a['harness']}/{a['model']}@{a['effort']}"
    # Not recorded yet: the node still shows the old route, marked pending.
    assert page.text_content("[data-testid=insp-effort]") == a["effort"]
    assert "pending" in node(page, a["id"]).text_content()
    assert page.is_disabled("[data-testid=route-apply]")
    # Office records it: the snapshot reports the new effort, and only then does the UI show it.
    con = sqlite3.connect(svc.db_path)
    con.execute("UPDATE dispatches SET effort=? WHERE id=?", (new_effort, a["id"].removeprefix("dispatch:")))
    con.commit()
    con.close()
    with svc.lock:
        svc.poll(force=True)
    page.wait_for_function("(e) => document.querySelector('[data-testid=insp-effort]').textContent === e",
                           arg=new_effort, timeout=10000)
    assert page.locator("[data-testid=route-pending-detail]").count() == 0
    assert "pending" not in node(page, a["id"]).text_content()


def test_route_change_absent_without_capability_and_for_orchestrators(page, served):
    url, svc = served
    open_agents(page, url)
    a = _live_executor(svc)
    page.evaluate("""(run) => { window.officeStore.state.entities.runs[run].controls.change_route =
        {allowed: false, reason: 'legacy runtime'}; window.officeStore.emit('delta'); }""", a["run"])
    inspect(page, a["id"])
    page.locator("[data-testid=agent-inspector]").wait_for()
    assert page.locator("[data-testid=route-change]").count() == 0
    oid = pick(page, "a => a.column === 'orchestrators'")
    inspect(page, oid)
    assert page.locator("[data-testid=route-change]").count() == 0
    done = pick(page, "a => a.state.complete")
    inspect(page, done)
    assert page.locator("[data-testid=route-change]").count() == 0


def test_keyboard_moves_through_nodes_and_into_composer(page, served):
    url, _ = served
    open_agents(page, url)
    first = page.locator('[data-testid=role-column][data-column=orchestrators] [data-testid=agent-node]').first
    first.focus()
    page.keyboard.press("ArrowDown")
    second = page.evaluate("document.activeElement.dataset.id")
    assert second == page.locator('[data-column=orchestrators] [data-testid=agent-node]').nth(1).get_attribute("data-id")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.column") == "plan_reviewers"
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("document.activeElement.dataset.column") == "orchestrators"
    page.keyboard.press("Enter")
    page.locator("[data-testid=chat-input]").wait_for()
    page.locator("[data-testid=chat-input]").focus()
    page.keyboard.type("hi")
    assert page.input_value("[data-testid=chat-input]") == "hi"
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=agent-inspector]", state="detached")
    assert page.evaluate("document.activeElement.dataset.id") == second


def test_1100px_reduces_secondary_detail_and_stays_usable(page, served):
    url, _ = served
    errors = open_agents(page, url, 1100, 800)
    card = page.locator("[data-testid=agent-node]").first
    assert not card.locator(".node-telemetry").is_visible()
    assert card.locator("[data-testid=node-route]").is_visible()
    insp = inspect(page, card.get_attribute("data-id"))
    assert insp.is_visible() and insp.locator("[data-testid=agent-telemetry]").is_visible()
    box = insp.bounding_box()
    assert box["x"] + box["width"] <= 1100 + 1
    assert errors == []


@pytest.mark.parametrize("size", [(1440, 900), (1920, 1080)])
def test_desktop_widths_show_every_column_without_errors(page, served, size):
    url, _ = served
    errors = open_agents(page, url, *size)
    for c in COLUMNS:
        assert page.locator(f"[data-testid=role-column][data-column={c}]").is_visible()
    assert errors == []
