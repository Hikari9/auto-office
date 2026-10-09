"""T6: visual states limited to viewports, dev overlays hidden, scripted-interaction failures as evidence.

A scripted state that cannot be reached is as likely a wrong script as a broken product, so under the convergence
contract it goes to the vision reviewer as evidence instead of ending the gate with a RECHECK that spends a round.
The reviewer still judges every reachable state, and a pinned visual reviewer judges it.
"""
from __future__ import annotations

import json
from importlib.util import find_spec

import pytest

from conftest import GOOD_ADD, PLAN_ONE, start_inline

requires_playwright = pytest.mark.skipif(find_spec("playwright") is None,
                                         reason="install the visual extra to run browser capture integration tests")
APPROVED = "VERDICT: APPROVED\nNEXT proceed"
VISUAL_OK = "EVIDENCE_STATUS: COMPARABLE\n" + APPROVED
SUBMIT = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
CODEX = "codex/gpt-6-luna@xhigh"
CLAUDE = "claude/claude-opus-5-5@high"
PLAN_VISUAL = PLAN_ONE.replace("visual: none", "visual:\n  url: http://localhost:3999/\n  viewports: desktop, mobile\n"
                               "  states: default; menu-open@mobile = click [data-test=menu] -> expect nav.open")


# ------------------------------------------------------------------ states limited to viewports (unit)

def test_a_state_can_name_the_viewports_it_is_captured_at():
    from office import visual
    parsed = visual.states({"states": "default; menu-open@mobile = click [data-test=menu] -> expect nav.open; "
                                      "wide@desktop,tablet = hover nav"})
    assert [(s["name"], s["viewports"]) for s in parsed] == [("default", None), ("menu-open", ["mobile"]),
                                                              ("wide", ["desktop", "tablet"])]
    assert parsed[1]["steps"] == [{"verb": "click", "arg": "[data-test=menu]"}] and parsed[1]["expect"] == "nav.open"


def test_a_state_without_a_limit_applies_everywhere_and_a_limit_covers_variants():
    from office import visual
    free, mobile = visual.states({"states": "default; open@mobile = click a"})
    assert all(visual.state_applies(free, v) for v in ("desktop", "mobile", "390x844"))
    assert visual.state_applies(mobile, "mobile") and visual.state_applies(mobile, "mobile-portrait")
    assert visual.state_applies(mobile, "mobile-landscape") and visual.state_applies(mobile, "Mobile")
    assert not visual.state_applies(mobile, "desktop") and not visual.state_applies(mobile, "tablet")
    assert not visual.state_applies(mobile, "mobilex"), "a prefix of a longer word is another viewport"


def test_the_default_state_list_is_unchanged():
    from office import visual
    assert visual.states({}) == [{"name": "default", "steps": [], "expect": None, "viewports": None}]


def test_preflight_refuses_a_state_for_a_viewport_the_block_never_captures():
    from office import visual
    task = {"id": "T1", "visual": {"url": "http://localhost:1/", "start": "x", "viewports": "desktop, mobile",
                                   "states": "default; open@tablet = click a"}}
    errors, _ = visual.preflight([task])
    assert errors == ["T1: visual state 'open@tablet' names a viewport the block does not capture (viewports: desktop, mobile)"]


def test_preflight_refuses_a_viewport_left_with_no_state():
    from office import visual
    task = {"id": "T1", "visual": {"url": "http://localhost:1/", "start": "x", "viewports": "desktop, mobile",
                                   "states": "open@mobile = click a"}}
    errors, _ = visual.preflight([task])
    assert errors == ["T1: visual viewport desktop has no state to capture (every state is limited to other viewports)"]


def test_preflight_accepts_limited_states_that_cover_every_viewport():
    from office import visual
    task = {"id": "T1", "visual": {"url": "http://localhost:1/", "start": "x", "viewports": "desktop, mobile-portrait",
                                   "states": "default; open@mobile = click a"}}
    assert visual.preflight([task])[0] == []


def test_submit_refuses_an_uncapturable_state_before_review(env):
    env.trust()
    env.office("start", "g", "--gear", "direct+review", "--planner", "inline", check=0)
    env.write_plan(PLAN_ONE.replace("visual: none", "visual:\n  url: http://localhost:3999/\n  start: serve\n"
                                    "  viewports: desktop\n  states: default; open@mobile = click a"))
    code, out = env.office("submit")
    assert code == 4 and "plan-visual-uncapturable" in out and "open@mobile" in out, out


# ------------------------------------------------------------------ dev overlays

def test_known_framework_dev_overlays_are_hidden_in_every_capture():
    from office import visual
    assert "nextjs-portal" in visual.DEV_OVERLAYS
    assert '["nextjs-portal", "#__next-build-watcher"]' in visual._HIDE_OVERLAYS_JS
    assert "display:none!important" in visual._HIDE_OVERLAYS_JS


PAGE = """<!doctype html><html><head><style>body{margin:0}header{height:56px;background:#123}
nav{display:none}nav.open{display:block}</style></head><body>
<header><button id="menu" data-test="menu" style="width:44px;height:44px">menu</button></header>
<nav id="nav">links</nav><main><p>Hello</p></main>
<script>document.getElementById('menu').onclick=()=>document.getElementById('nav').classList.add('open')</script>
%s</body></html>"""
OVERLAY = '<nextjs-portal style="position:absolute;left:0;top:0;width:3000px;height:40px;background:red;display:block"></nextjs-portal>'


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    from office import visual
    with sync_playwright() as pw:
        b = pw.chromium.launch(**visual.launch_args())
        try:
            yield b
        finally:
            b.close()


def _capture(browser, tmp_path, page, **spec):
    from office import visual
    html = tmp_path / "index.html"
    html.write_text(page)
    spec = {"viewports": "desktop, mobile", "selectors": "nextjs-portal, #menu", **spec}
    return visual._capture_frames(browser, html.as_uri(), spec, tmp_path, None)


@pytest.mark.integration
@requires_playwright
def test_a_state_limited_to_mobile_is_captured_at_mobile_only(browser, tmp_path):
    spec = {"states": "default; menu-open@mobile = click [data-test=menu] -> expect nav.open"}
    result = _capture(browser, tmp_path, PAGE % "", **spec)
    assert [(f["viewport"].split()[0], f["state"]) for f in result["frames"]] == [
        ("desktop", "default"), ("mobile", "default"), ("mobile", "menu-open")]
    assert all(not f["failures"] for f in result["frames"])
    assert (tmp_path / "mobile-menu-open.png").is_file() and not (tmp_path / "desktop-menu-open.png").exists()


@pytest.mark.integration
@requires_playwright
def test_an_unlimited_interaction_state_is_still_captured_everywhere(browser, tmp_path):
    result = _capture(browser, tmp_path, PAGE % "", states="default; menu-open = click [data-test=menu] -> expect nav.open")
    assert len(result["frames"]) == 4


@pytest.mark.integration
@requires_playwright
def test_a_nextjs_dev_overlay_is_hidden_so_it_neither_overflows_nor_shows(browser, tmp_path):
    result = _capture(browser, tmp_path, PAGE % OVERLAY)
    assert result["environment"]["dev_overlays_hidden"] == ["nextjs-portal", "#__next-build-watcher"]
    for frame in result["frames"]:
        assert frame["failures"] == [], frame["failures"]
        overlay = frame["probe"]["elements"]["nextjs-portal"]
        assert overlay is not None and overlay["visible"] is False and overlay["width"] == 0


CSP_PAGE = PAGE.replace("<head><style>", "<head><meta http-equiv=\"Content-Security-Policy\" content=\"style-src 'nonce-ok'\">"
                        "<style nonce=\"ok\">nextjs-portal{display:block;position:absolute;left:0;top:0;width:3000px;height:40px;"
                        "background:red}", 1)


@pytest.mark.integration
@requires_playwright
def test_the_overlay_is_hidden_on_a_page_whose_csp_blocks_inline_styles(browser, tmp_path):
    result = _capture(browser, tmp_path, CSP_PAGE % "<nextjs-portal></nextjs-portal>", viewports="mobile")
    for frame in result["frames"]:
        assert frame["failures"] == [], frame["failures"]
        assert frame["probe"]["elements"]["nextjs-portal"]["visible"] is False


@pytest.mark.integration
@requires_playwright
def test_other_wide_elements_still_overflow_so_the_hiding_is_not_blanket(browser, tmp_path):
    wide = OVERLAY.replace("nextjs-portal", "div")
    result = _capture(browser, tmp_path, PAGE % wide, viewports="mobile")
    assert any("horizontal overflow" in f["summary"] for fr in result["frames"] for f in fr["failures"])


@pytest.mark.integration
@requires_playwright
def test_a_failed_scripted_state_is_tagged_as_an_interaction_failure(browser, tmp_path):
    result = _capture(browser, tmp_path, PAGE.replace("classList.add('open')", "classList.add('nope')") % "",
                      viewports="desktop", states="default; menu-open = click [data-test=menu] -> expect nav.open")
    failed = [f for fr in result["frames"] for f in fr["failures"]]
    assert [f["source"] for f in failed] == ["interaction"] and "did not produce nav.open" in failed[0]["summary"]
    missing = _capture(browser, tmp_path, PAGE % "", viewports="desktop", states="default; gone = click #nowhere")
    assert [f["source"] for fr in missing["frames"] for f in fr["failures"]] == ["interaction"]
    overflow = _capture(browser, tmp_path, PAGE % OVERLAY.replace("nextjs-portal", "div"), viewports="mobile")
    assert all("source" not in f for fr in overflow["frames"] for f in fr["failures"]), "measured failures are not scripted"


# ------------------------------------------------------------------ the lane job

def _start(env, plan=PLAN_VISUAL, **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear="direct+review")
    env.office("approve", "plan", "--quote", "approved", check=0)


def _fake_capture(monkeypatch, tmp_path, failures=(), shots=True):
    """A capture whose states fail as `failures` say; with `shots` the default state is reachable."""
    from office import visual
    shot = tmp_path / "desktop-default.png"
    shot.write_bytes(b"\x89PNG" + b"0" * 2000)

    def capture_all(con, run, task, rev, gate, worktree):
        frames = ([{"viewport": "desktop 1440x900", "state": "default", "screenshot": str(shot)}] if shots else []) \
            + [{"viewport": "mobile 390x844", "state": "menu-open", "failures": [f]} for f in failures]
        receipt = tmp_path / f"receipt-{gate['id']}-{task['id']}.json"
        receipt.write_text(json.dumps({"reference": None, "frames": frames}))
        return {"evidence_status": "COMPARABLE", "cause": None, "receipt_path": str(receipt), "receipt_digest": "x",
                "product_failures": [{"code": f"U{i}", "severity": "material", **f} for i, f in enumerate(failures, 1)]}

    monkeypatch.setattr(visual, "capture_all", capture_all)
    monkeypatch.setattr(visual, "preflight", lambda tasks: ([], []))


SCRIPTED = {"location": "[data-test=menu] @ 390x844 state menu-open", "action": "the intended interaction must work",
            "summary": "interaction 'click [data-test=menu]' failed: element is not visible", "source": "interaction"}
OVERFLOW = {"location": "page @ 390x844", "action": "remove the overflow",
            "summary": "horizontal overflow: content is 3000 css-px wide in a 390 css-px viewport"}


def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _visual_briefs(env):
    return [p.read_text() for p in sorted(env.state.rglob("brief.md"), key=lambda p: p.stat().st_mtime)
            if p.read_text().startswith("ROLE independent visual reviewer")]


def _scope(env):
    return (json.loads(_q(env, "SELECT landing_json FROM runs")[0]["landing_json"]).get("convergence") or {}).get("L-T1") or {}


def test_scripted_interaction_failures_alone_go_to_the_vision_reviewer_and_spend_no_round(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path, failures=[SCRIPTED])
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": VISUAL_OK}],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    gate = _q(env, "SELECT * FROM gates WHERE kind='visual'")[0]
    assert gate["verdict"] == "APPROVED" and gate["round"] == 1 and gate["route"] != "deterministic-capture", gate
    assert [c["role"] for c in env.calls()].count("visual_reviewer") == 1, "the vision reviewer judged"
    brief, = _visual_briefs(env)
    assert "SCRIPTED INTERACTION FAILURES (evidence for you to judge, not findings" in brief
    assert "T1 [data-test=menu] @ 390x844 state menu-open: interaction 'click [data-test=menu]' failed" in brief
    assert "desktop 1440x900 state default" in brief, "the reachable state is what it judges"
    assert "Framework dev overlays (nextjs-portal) are hidden in the captures" in brief
    assert _q(env, "SELECT 1 FROM events WHERE kind='visual.scripted_failures'")
    assert _scope(env)["status"] == "approved" and _scope(env)["round"] == 1


def test_a_reviewer_who_finds_the_product_broken_blocks_by_judgment(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path, failures=[SCRIPTED])
    verdict = ("EVIDENCE_STATUS: COMPARABLE\nVERDICT: RECHECK\nFINDING U1 | high | blocking | menu @ mobile | the menu button "
               "is hidden | show it | owner: T1\nNEXT fix the menu")
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": verdict}],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    gate = _q(env, "SELECT * FROM gates WHERE kind='visual'")[0]
    assert gate["verdict"] == "RECHECK" and gate["route"] != "deterministic-capture"
    assert _scope(env)["round"] == 2, "a judged RECHECK spends the round"


def test_a_measured_failure_still_ends_the_gate_without_judgment_and_carries_the_scripted_one(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path, failures=[SCRIPTED, OVERFLOW])
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": VISUAL_OK}],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    gate = _q(env, "SELECT * FROM gates WHERE kind='visual'")[0]
    assert gate["verdict"] == "RECHECK" and gate["route"] == "deterministic-capture"
    assert "visual_reviewer" not in [c["role"] for c in env.calls()]
    assert len(_q(env, "SELECT 1 FROM findings WHERE gate_kind='visual'")) == 2


def test_with_no_reachable_state_a_scripted_failure_stays_deterministic(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path, failures=[SCRIPTED], shots=False)
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[{"reply": VISUAL_OK}],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    gate = _q(env, "SELECT * FROM gates WHERE kind='visual'")[0]
    assert gate["verdict"] == "RECHECK" and gate["route"] == "deterministic-capture", "nothing left to judge"
    assert "visual_reviewer" not in [c["role"] for c in env.calls()]


def test_scripted_failures_go_to_the_reviewer_only_for_tasks_with_something_to_judge():
    from office import convergence
    scripted = {"source": "interaction", "owners": ["T1"]}
    other = {"source": "interaction", "owners": ["T2"]}
    measured = {"owners": ["T1"]}
    shot = {"task": "T1", "screenshot": "a.png"}
    judge = convergence.judgeable_scripted_failures
    assert judge([scripted], [shot]) == [scripted]
    assert judge([scripted, measured], [shot]) == [], "a measured failure ends the gate"
    assert judge([scripted], [{"task": "T1"}]) == [], "nothing captured: nothing to judge"
    assert judge([scripted, other], [shot]) == [], "T2 has no captured state, however many T1 has"
    assert judge([], [shot]) == []


def test_visual_finding_codes_are_recoded_like_any_other_gate_kind(tmp_path):
    import uuid
    from types import SimpleNamespace

    from office import contract, convergence, db
    con = db.connect(tmp_path / "runs.db")
    con.execute("INSERT INTO findings(id, run_id, scope, contract, gate_kind, code, state, location, summary) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, "r", "L-T1", contract.CONVERGENCE, "visual", "U1", "resolved",
                                               "header @ mobile", "menu clipped"))
    parsed = SimpleNamespace(findings=[{"code": "U1", "summary": "footer overlaps", "location": "footer @ desktop"}],
                             resolved=[], retracted=[])
    assert convergence.distinct_codes(con, {"id": "r"}, "L-T1", "visual", parsed) == {"U1": "U2"}
    con.close()


def test_the_lane_visual_job_runs_on_a_visual_pin_set_before_its_first_review(env, monkeypatch, tmp_path):
    """The pin is the user's (T3's `dispatch L-T1:visual --review-as`); with scripted failures as evidence it is
    still the pinned route that judges."""
    _fake_capture(monkeypatch, tmp_path, failures=[SCRIPTED])
    scripts = {"claude:visual_reviewer": [{"reply": VISUAL_OK}], "codex:visual_reviewer": [{"reply": VISUAL_OK}],
               "visual_reviewer": [{"reply": VISUAL_OK}]}
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], probe=[{"reply": "auto"}], **scripts)
    env.office("dispatch", "T1", check=0)
    first = [c for c in env.calls() if c["role"] == "visual_reviewer"][0]["harness"]

    # A second run on a fresh scenario, pinned to the other route before anything is reviewed.
    other = "codex" if first == "claude" else "claude"
    route = CODEX if other == "codex" else CLAUDE
    env.office("start", "second goal", "--gear", "direct+review", "--planner", "inline", check=0)
    env.write_plan(PLAN_VISUAL)
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "L-T1:visual", "--review-as", route, check=0)
    before = len([c for c in env.calls() if c["role"] == "visual_reviewer"])
    env.office("dispatch", "T1", check=0)
    judged = [c for c in env.calls() if c["role"] == "visual_reviewer"][before:]
    assert judged and judged[0]["harness"] == other != first, (first, other, judged)
    assert [g["verdict"] for g in _q(env, "SELECT * FROM gates WHERE kind='visual' ORDER BY created_at")][-1] == "APPROVED"
