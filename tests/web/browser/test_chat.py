"""Orchestrator chat in a real browser: exact target, per-target drafts, disabled reasons, delivery receipts, resend."""
from __future__ import annotations

import json
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
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.click("[data-surface=agents]")
    page.wait_for_selector("[data-testid=agent-node]", timeout=10000)
    page.check("[data-testid=agents-include-completed]")  # these tests may target completed agents
    return errors


def inspect(page, node_id):
    page.locator(f'[data-testid=agent-node][data-id="{node_id}"]').click()
    page.wait_for_selector(f'[data-testid=agent-inspector][data-id="{node_id}"]', timeout=5000)


def chat_orchestrators(svc):
    """Two chat-capable orchestrators on different runs."""
    snap = svc.snapshot_state["entities"]
    out = [a for a in snap["agents"].values() if a["column"] == "orchestrators"
           and snap["runs"][a["run"]]["controls"]["chat_send"]["allowed"]]
    assert len(out) >= 2
    return out[0], out[1]


def recorder(page, reply):
    """Intercept /api/commands; `reply(route, body)` answers. Returns the list of sent bodies."""
    sent = []

    def handle(route):
        body = json.loads(route.request.post_data)
        sent.append(body)
        reply(route, body)
    page.route("**/api/commands", handle)
    return sent


def answer(status, receipt_status=None, message=None):
    def reply(route, body):
        payload = {"ok": status < 400, "receipt": {"id": body["id"], "kind": body["kind"], "status": receipt_status,
                                                   "error": message} if receipt_status else None,
                   "reason": "refused", "message": message}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
    return reply


def test_composer_only_on_orchestrators_and_shows_exact_target(page, served):
    url, svc = served
    errors = open_agents(page, url)
    a, _ = chat_orchestrators(svc)
    run = svc.snapshot_state["entities"]["runs"][a["run"]]
    inspect(page, a["id"])
    target = page.locator("[data-testid=chat-target]")
    values = dict(zip(target.locator("dt").all_text_contents(), target.locator("dd").all_text_contents()))
    assert values == {"Host": svc.snapshot_state["scalars"]["host"]["id"], "Repository": run["repo"]["slug"],
                      "Run": run["run_id"], "Session": a["id"]}
    assert page.get_attribute("[data-testid=chat-input]", "aria-label") == f"Message to {a['id']}"
    worker = next(x for x in svc.snapshot_state["entities"]["agents"].values() if x["column"] != "orchestrators")
    inspect(page, worker["id"])
    assert page.locator("[data-testid=chat], [data-testid=chat-input]").count() == 0
    assert errors == []


def test_drafts_stay_with_their_exact_target(page, served):
    url, svc = served
    open_agents(page, url)
    a, b = chat_orchestrators(svc)
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "for A")
    inspect(page, b["id"])
    assert page.input_value("[data-testid=chat-input]") == ""  # A's draft did not follow the selection
    page.fill("[data-testid=chat-input]", "for B")
    inspect(page, a["id"])
    assert page.input_value("[data-testid=chat-input]") == "for A"
    inspect(page, b["id"])
    assert page.input_value("[data-testid=chat-input]") == "for B"
    # A live update re-renders the composer without losing or moving the draft.
    page.evaluate("document.querySelector('[data-testid=chat-input]').dataset.mark = 'old'")
    page.evaluate("window.officeStore.emit('delta')")
    page.wait_for_function("() => !document.querySelector('[data-testid=chat-input]').dataset.mark", timeout=5000)
    assert page.input_value("[data-testid=chat-input]") == "for B"


def test_send_goes_only_to_the_selected_drafts_target(page, served):
    url, svc = served
    open_agents(page, url)
    a, b = chat_orchestrators(svc)
    sent = recorder(page, answer(202, "completed"))
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "only for A")
    inspect(page, b["id"])
    page.fill("[data-testid=chat-input]", "for B")
    page.click("[data-testid=chat-send]")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=delivered]", timeout=5000)
    assert [(s["target"]["session"], s["payload"]["text"]) for s in sent] == [(b["id"], "for B")]
    sent.clear()
    inspect(page, a["id"])
    assert page.input_value("[data-testid=chat-input]") == "only for A"
    page.click("[data-testid=chat-send]")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=delivered]", timeout=5000)
    run = svc.snapshot_state["entities"]["runs"][a["run"]]
    assert len(sent) == 1
    assert sent[0]["kind"] == "chat_send"
    assert sent[0]["target"] == {"host": svc.snapshot_state["scalars"]["host"]["id"], "run_id": run["run_id"],
                                 "session": a["id"]}
    assert sent[0]["payload"] == {"text": "only for A"}
    assert page.input_value("[data-testid=chat-input]") == ""
    inspect(page, b["id"])
    assert page.locator("[data-testid=chat-receipt]").all_text_contents() == ["for Bdelivered"]  # receipts per target


def test_a_target_mismatch_at_send_time_sends_nothing(page, served):
    url, svc = served
    open_agents(page, url)
    a, b = chat_orchestrators(svc)
    sent = recorder(page, answer(202, "completed"))
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "for A")
    # The composer for A is still on screen, but the selection has moved to B before the click lands.
    page.evaluate("""(b) => { const btn = document.querySelector('[data-testid=chat-send]');
        document.querySelector(`[data-testid=agent-node][data-id="${b}"]`).click(); btn.click(); }""", b["id"])
    inspect(page, a["id"])
    assert page.input_value("[data-testid=chat-input]") == "for A"  # a real send would have cleared the draft
    assert page.locator("[data-testid=chat-receipt]").count() == 0
    assert sent == []


@pytest.mark.parametrize("cause", ["not-capable", "stale", "disconnected", "session-stale", "no-state"])
def test_composer_disabled_with_reason(page, served, cause):
    url, svc = served
    open_agents(page, url)
    a, _ = chat_orchestrators(svc)
    inspect(page, a["id"])
    assert page.is_enabled("[data-testid=chat-input]") and page.is_enabled("[data-testid=chat-send]")
    if cause == "not-capable":
        page.evaluate("""(run) => { window.officeStore.state.entities.runs[run].controls.chat_send =
            {allowed: false, reason: 'Auto Office 3.1 run: web controls need the 3.3 line'};
            window.officeStore.emit('delta'); }""", a["run"])
        want = "not chat-capable: Auto Office 3.1 run"
    elif cause == "session-stale":
        page.evaluate("""(id) => { window.officeStore.state.entities.agents[id].state.stale = true;
            window.officeStore.emit('delta'); }""", a["id"])
        want = "session is stale"
    elif cause == "no-state":
        page.evaluate("""(id) => { const a = window.officeStore.state.entities.agents[id]; delete a.state;
            window.officeStore.state.scalars.host = null; window.officeStore.emit('delta'); }""", a["id"])
        want = "host or run missing"
    elif cause == "stale":
        with svc.lock:
            svc.stale_after = -1
            svc._rebuild(reuse_entities=True)
        want = "Office data is stale"
    else:
        page.evaluate("window.officeStore.setStatus('disconnected')")
        want = "stream is disconnected"
    page.wait_for_selector("[data-testid=chat-blocked]", timeout=10000)
    assert want in page.text_content("[data-testid=chat-blocked]")
    assert page.is_disabled("[data-testid=chat-input]") and page.is_disabled("[data-testid=chat-send]")


def test_pending_then_failed_from_the_real_service(page, served):
    url, svc = served
    open_agents(page, url)
    a, _ = chat_orchestrators(svc)
    held = []
    page.route("**/api/commands", lambda route: held.append(route))
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "hello")
    page.click("[data-testid=chat-send]")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=pending]", timeout=5000)
    assert "pending" in page.text_content("[data-testid=chat-receipt]")
    held[0].continue_()  # the fixture knows no Herdr pane for this session: the service refuses delivery
    page.wait_for_selector("[data-testid=chat-receipt][data-status=failed]", timeout=10000)
    assert "failed" in page.text_content("[data-testid=chat-receipt]")
    assert "no Herdr pane" in page.get_attribute("[data-testid=chat-receipt]", "title")


def test_unknown_delivery_offers_only_confirmed_send_again_with_a_new_id(page, served):
    url, svc = served
    open_agents(page, url)
    a, _ = chat_orchestrators(svc)
    calls = {"n": 0}

    def reply(route, body):
        calls["n"] += 1
        if calls["n"] == 1:
            route.abort("connectionreset")  # no answer: the delivery is unknown
        else:
            answer(202, "completed")(route, body)
    sent = recorder(page, reply)
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "are you there")
    page.click("[data-testid=chat-send]")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=unknown]", timeout=5000)
    page.wait_for_timeout(1500)
    assert len(sent) == 1  # never retried automatically
    receipt = page.locator("[data-testid=chat-receipt][data-status=unknown]")
    assert receipt.locator("button").all_text_contents() == ["Send again"]
    page.click("[data-testid=chat-resend]")
    page.click("[data-testid=chat-resend-cancel]")  # cancelling sends nothing
    page.wait_for_timeout(200)
    assert len(sent) == 1
    page.click("[data-testid=chat-resend]")
    assert len(sent) == 1  # the first click only asks for confirmation
    page.click("[data-testid=chat-resend-confirm]")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=delivered]", timeout=5000)
    assert len(sent) == 2
    assert sent[1]["id"] != sent[0]["id"]
    assert sent[1]["payload"] == {"text": "are you there", "resend_of": sent[0]["id"]}
    assert sent[1]["target"] == sent[0]["target"]


def test_keyboard_shortcut_sends_from_the_composer(page, served):
    url, svc = served
    open_agents(page, url)
    a, _ = chat_orchestrators(svc)
    sent = recorder(page, answer(202, "completed"))
    inspect(page, a["id"])
    page.focus("[data-testid=chat-input]")
    page.keyboard.type("via keyboard")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("[data-testid=chat-receipt][data-status=delivered]", timeout=5000)
    assert [s["payload"]["text"] for s in sent] == ["via keyboard"]
