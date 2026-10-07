"""Layout of the dense tables at 1100x800, 1440x900 and 1920x1080, asserted from DOM measurements.

Each test also saves a screenshot per viewport (OFFICE_WEB_SHOTS, else the test's tmp directory).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from office.web import fixtures, server

VIEWPORTS = [(1100, 800), (1440, 900), (1920, 1080)]
IDS = [f"{w}x{h}" for w, h in VIEWPORTS]
EPS = 0.75
SHOTS = os.environ.get("OFFICE_WEB_SHOTS")  # read at import: the autouse fixture scrubs OFFICE_* per test
LONG_REPO = "synth-org-9/a-repository-with-a-deliberately-very-long-name"  # its issues have long titles too


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    monkeypatch.setitem(fixtures.EXTRA_REPOS, LONG_REPO, {"archived": False, "has_issues": True})
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    svc.close()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def shot(tmp_path, page):
    base = Path(SHOTS) if SHOTS else tmp_path / "shots"
    base.mkdir(parents=True, exist_ok=True)

    def take(name: str) -> Path:
        path = base / f"{name}.png"
        page.screenshot(path=str(path))
        assert path.stat().st_size > 2000, path
        return path
    return take


def open_surface(page, url, width, height, surface=None, wait="[data-testid=issue-row]"):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    if surface:
        page.click(f"[data-surface={surface}]")
        page.wait_for_selector(wait, timeout=10000)
    return errors


def no_sideways_page_scroll(page, width):
    assert page.evaluate("document.documentElement.scrollWidth") <= width


# ------------------------------------------------------------------ Issues

HEADERS = """() => [...document.querySelectorAll('[role=columnheader]')].map((el) => {
    const text = document.createRange(); text.selectNodeContents(el);
    const t = text.getBoundingClientRect(), b = el.getBoundingClientRect();
    return { label: el.textContent, left: b.left, right: b.right, textLeft: t.left, textRight: t.right,
             clipped: el.scrollWidth > el.clientWidth + 1, title: el.getAttribute('title'),
             ellipsis: getComputedStyle(el).textOverflow, overflow: getComputedStyle(el).overflowX };
})"""

# [text element, the element whose box clips it]: an inline <span> has no box of its own to measure.
TRUNCATION = """([selector, box]) => [...document.querySelectorAll('[data-testid=issue-row]')].flatMap((row) =>
    [...row.querySelectorAll(selector)].map((el) => {
        const clip = el.closest(box), css = getComputedStyle(clip);
        return { text: el.textContent, title: el.getAttribute('title'), cut: clip.scrollWidth > clip.clientWidth + 1,
                 overflow: css.overflowX, ellipsis: css.textOverflow };
    }))"""


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=IDS)
def test_issue_headers_do_not_overlap_and_long_text_ends_in_an_ellipsis(page, served, shot, width, height):
    url, _ = served
    errors = open_surface(page, url, width, height)
    heads = page.evaluate(HEADERS)
    assert [h["label"] for h in heads][5:8] == ["Priority", "Authorization · Auto queue", "Run / command"]
    for head, nxt in zip(heads, heads[1:]):
        assert head["right"] <= nxt["left"] + EPS, (head["label"], nxt["label"])  # boxes never overlap
        assert head["textRight"] <= nxt["left"] + EPS, f"{head['label']} text runs into {nxt['label']}"
    assert all(h["title"] == h["label"] for h in heads)  # a label a narrower font clips ends in an ellipsis, in full as title
    assert all(h["ellipsis"] == "ellipsis" and h["overflow"] == "hidden" for h in heads if h["clipped"])

    # Every body cell stays inside its row: the last column does not spill past it.
    spill = page.evaluate("""() => [...document.querySelectorAll('[data-testid=issue-row]')].filter((row) =>
        row.lastElementChild.getBoundingClientRect().right > row.getBoundingClientRect().right + 1).length""")
    assert spill == 0

    page.click(f'[data-testid="repo-{LONG_REPO}"]')  # the long names sort below the fold of the unfiltered table
    page.wait_for_function("(slug) => [...document.querySelectorAll('[data-testid=issue-row] .td.repo')].length > 0 && "
                           "[...document.querySelectorAll('[data-testid=issue-row] .td.repo')].every(e => e.textContent === slug)",
                           arg=LONG_REPO)
    for selector, box in ((".td.issue .t", ".t"), (".td.repo span", ".td.repo")):
        cells = page.evaluate(TRUNCATION, [selector, box])
        cut = [c for c in cells if c["cut"]]
        assert cut, f"nothing in {selector} is truncated: the test would prove nothing"
        for cell in cells:
            assert cell["title"] == cell["text"], cell  # the full text is always available
        for cell in cut:
            assert cell["overflow"] == "hidden" and cell["ellipsis"] == "ellipsis", cell
    owners = page.evaluate("""() => [...document.querySelectorAll('[data-testid=issue-row] .td:nth-child(3) span')]
        .map((el) => [el.textContent, el.getAttribute('title')])""")
    assert owners and all(text == title for text, title in owners)
    no_sideways_page_scroll(page, width)
    assert errors == []
    shot(f"issues-{width}x{height}")


# ------------------------------------------------------------------ Allocation

WORK_WORDS = """() => [...document.querySelectorAll('[data-testid=alloc-row] .c.work')].flatMap((cell) =>
    [...cell.querySelectorAll('.t, .s')].flatMap((el) => {
        const node = el.firstChild;
        if (!node || node.nodeType !== Node.TEXT_NODE) return [];
        const words = [...node.textContent.matchAll(/[^\\s\\-\\/#]+/g)]; // a break after - / # is legal
        return words.map((m) => {
            const range = document.createRange();
            range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
            return { word: m[0], lines: new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size };
        });
    }))"""

ACTIVE = '[data-testid=alloc-active-group]'

REACH = """(group) => {
    const table = document.querySelector(`${group} .atable`), box = table.getBoundingClientRect();
    const heads = Object.fromEntries([...table.querySelectorAll('.arow.head [role=columnheader]')]
        .map((el) => [el.textContent, el.getBoundingClientRect()]));
    const inside = (r) => r.left >= box.left - 1 && r.right <= box.right + 1;
    const buttons = [...table.querySelectorAll('[data-testid=alloc-row] .c.ctl button')].map((b) => b.getBoundingClientRect());
    return { scrollLeft: table.scrollLeft, over: table.scrollWidth - table.clientWidth,
             reachable: ['Auto', 'Decision', 'Controls'].map((name) => !!heads[name] && inside(heads[name])),
             buttonsInside: buttons.length > 0 && buttons.every(inside) };
}"""


def scroll_to_end(page, hint_selector):
    """Click the hint's right button until it reports the end; each click waits for the scroll to settle."""
    hint = page.locator(hint_selector)
    for _ in range(8):
        if hint.get_attribute("data-more") == "left":
            return
        before = hint.get_attribute("data-more")
        hint.locator("[data-testid=scroll-right]").click()
        page.wait_for_function("([sel, was]) => { const h = document.querySelector(sel); return h.dataset.more !== was "
                               "|| h.querySelector('[data-testid=scroll-right]').getAttribute('aria-disabled') === 'true'; }",
                               arg=[hint_selector, before])
        page.wait_for_timeout(120)  # a smooth step ends in a later frame than its first scroll event
    pytest.fail("the scroll button never reached the end")


def add_item(svc, item_id, title, ref="synth-org-0/repo-00#3"):
    con = sqlite3.connect(svc.db_path, timeout=30)
    try:
        con.execute("INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) VALUES"
                    "(?,'issue',?,?,'normal','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')", (item_id, ref, title))
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=IDS)
def test_allocation_active_table_columns_are_reachable_and_work_never_breaks_mid_word(page, served, shot, width, height):
    url, svc = served
    add_item(svc, "issue:wordy", "Reconcile the scheduler fixtures for the quarterly allocation review",
             ref="synth-org-0/repo-00#3")
    errors = open_surface(page, url, width, height, "allocation", "[data-testid=alloc-row]")
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:wordy"]', timeout=10000)
    words = page.evaluate(WORK_WORDS)
    assert len(words) > 20 and not [w for w in words if w["lines"] > 1], "a word of the work cell broke across lines"

    hint = page.locator(f"{ACTIVE} [data-testid=scroll-hint]")
    assert page.locator("[data-testid=surface-allocation]").evaluate("el => el.scrollWidth <= el.clientWidth"), \
        "the surface scrolls instead of the table"
    no_sideways_page_scroll(page, width)
    reach = page.evaluate(REACH, ACTIVE)
    if width == 1100:
        assert reach["over"] > 1, "1100px is the width this scroll behavior exists for"
    if reach["over"] <= 1:  # the layout fits: nothing to scroll and no hint
        assert width > 1100 and all(reach["reachable"]) and reach["buttonsInside"]
        page.wait_for_selector(f"{ACTIVE} [data-testid=scroll-hint][data-more=none]", state="hidden")
    else:
        page.wait_for_selector(f"{ACTIVE} [data-testid=scroll-hint][data-more=right]")
        assert not all(reach["reachable"])  # Auto, Decision and Controls start off-screen ...
        hint.scroll_into_view_if_needed()
        shot(f"allocation-{width}x{height}-start")
        scroll_to_end(page, f"{ACTIVE} [data-testid=scroll-hint]")  # ... and a click or two brings them in.
        end = page.evaluate(REACH, ACTIVE)
        assert end["scrollLeft"] > 0 and all(end["reachable"]) and end["buttonsInside"]
        assert page.locator(f"{ACTIVE} [data-testid=scroll-left]").get_attribute("aria-disabled") == "false"
    assert errors == []
    shot(f"allocation-{width}x{height}")


def test_scroll_buttons_keep_focus_at_the_end_and_through_a_rebuild(page, served):
    url, svc = served
    open_surface(page, url, 1100, 800, "allocation", "[data-testid=alloc-row]")
    hint = f"{ACTIVE} [data-testid=scroll-hint]"
    page.wait_for_selector(f"{hint}[data-more=right]")
    right = page.locator(f"{hint} [data-testid=scroll-right]")
    right.focus()
    while page.locator(hint).get_attribute("data-more") != "left":
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
    assert page.evaluate("document.activeElement.dataset.testid") == "scroll-right"  # not dropped to <body> when it ends
    add_item(svc, "issue:focus", "Focus probe")
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:focus"]', timeout=10000)  # the surface was rebuilt
    assert page.evaluate("document.activeElement.dataset.key") == "scroll:alloc-active-group:right"


def test_allocation_scroll_position_survives_a_state_change(page, served):
    url, svc = served
    open_surface(page, url, 1100, 800, "allocation", "[data-testid=alloc-row]")
    page.wait_for_selector(f"{ACTIVE} [data-testid=scroll-hint][data-more=right]")
    scroll_to_end(page, f"{ACTIVE} [data-testid=scroll-hint]")
    before = page.evaluate(REACH, ACTIVE)["scrollLeft"]
    assert before > 0
    add_item(svc, "issue:layout", "Layout probe")
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:layout"]', timeout=10000)  # the surface was rebuilt
    assert page.evaluate(REACH, ACTIVE)["scrollLeft"] == pytest.approx(before, abs=1)


# ------------------------------------------------------------------ Agents

GRAPH = """() => {
    const graph = document.querySelector('[data-testid=agents-graph]'), box = graph.getBoundingClientRect();
    const cols = [...graph.querySelectorAll('[data-testid=role-column]')];
    const last = cols[cols.length - 1], r = last.getBoundingClientRect();
    return { columns: cols.map((c) => c.dataset.column), over: graph.scrollWidth - graph.clientWidth,
             scrollLeft: graph.scrollLeft, lastLeft: r.left - box.left, lastRight: r.right - box.right,
             titleVisible: (() => { const t = last.querySelector('.col-title').getBoundingClientRect();
                 return t.left >= box.left - 1 && t.right <= box.right + 1; })(),
             bottom: box.bottom };
}"""

AGENTS_HINT = "[data-testid=surface-agents] [data-testid=scroll-hint]"


def assert_last_column_reachable(page, must_scroll=False):
    g = page.evaluate(GRAPH)
    assert g["columns"][-1] == "visual_verifiers"
    hint = page.locator(AGENTS_HINT)
    if must_scroll:
        assert g["over"] > 1, "this width is where the graph has to scroll"
    if g["over"] <= 1:
        assert g["lastRight"] <= 1 and g["lastLeft"] >= 0 and g["titleVisible"]
        page.wait_for_selector(f"{AGENTS_HINT}[data-more=none]", state="hidden")
        return
    page.wait_for_selector(f"{AGENTS_HINT}[data-more=right]")
    assert g["lastRight"] > 1  # without scrolling the rightmost column is cut off
    scroll_to_end(page, AGENTS_HINT)
    g = page.evaluate(GRAPH)
    assert g["scrollLeft"] > 0 and g["lastRight"] <= 1 and g["lastLeft"] >= 0 and g["titleVisible"]
    assert hint.get_attribute("data-more") == "left"


@pytest.mark.parametrize("width,height", VIEWPORTS, ids=IDS)
def test_agents_rightmost_column_is_visible_or_reachable(page, served, shot, width, height):
    url, _ = served
    errors = open_surface(page, url, width, height, "agents", "[data-testid=agent-node]")
    g = page.evaluate(GRAPH)
    if width >= 1440:
        assert g["over"] <= 1, "the five columns fit without scrolling"
    shot(f"agents-{width}x{height}")
    assert_last_column_reachable(page, must_scroll=width == 1100)
    no_sideways_page_scroll(page, width)
    assert errors == []
    shot(f"agents-{width}x{height}-scrolled")


@pytest.mark.parametrize("width,height", VIEWPORTS[1:], ids=IDS[1:])
def test_agents_with_the_inspector_open_keeps_the_last_column_reachable(page, served, shot, width, height):
    url, _ = served
    open_surface(page, url, width, height, "agents", "[data-testid=agent-node]")
    page.locator("[data-testid=agent-node]").first.click()
    page.wait_for_selector("[data-testid=agent-inspector]")
    boxes = page.evaluate("""() => ({ graph: document.querySelector('[data-testid=agents-graph]').getBoundingClientRect().toJSON(),
        inspector: document.querySelector('[data-testid=agent-inspector]').getBoundingClientRect().toJSON() })""")
    assert boxes["graph"]["right"] <= boxes["inspector"]["left"] + EPS  # side by side, never overlapping
    assert boxes["graph"]["width"] >= 400 and boxes["inspector"]["right"] <= width
    assert_last_column_reachable(page)
    shot(f"agents-inspector-{width}x{height}")


def test_agents_scroll_position_survives_a_rebuild(page, served):
    url, _ = served
    open_surface(page, url, 1100, 800, "agents", "[data-testid=agent-node]")
    page.wait_for_selector(f"{AGENTS_HINT}[data-more=right]")
    scroll_to_end(page, AGENTS_HINT)
    before = page.evaluate(GRAPH)["scrollLeft"]
    assert before > 0
    page.evaluate("document.querySelector('[data-testid=agents-graph]').dataset.stale = '1'")
    page.locator("[data-testid=agents-include-completed]").check()  # rebuilds the surface
    page.wait_for_selector("[data-testid=agents-graph]:not([data-stale])")
    assert page.evaluate(GRAPH)["scrollLeft"] == pytest.approx(before, abs=1)
