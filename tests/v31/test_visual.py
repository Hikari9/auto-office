"""Visual gate with real headless Chrome (skipped when Playwright is absent)."""
from __future__ import annotations

import socket

import pytest

pytest.importorskip("playwright")


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
    # First preference (latest Gemini Flash via agy at low effort), qualified by its own probe.
    assert (judge["harness"], judge["model"]) == ("agy", "gemini-3.8-flash-low"), dict(judge)
    receipt = con.execute("SELECT path FROM evidence WHERE kind='capture_receipt'").fetchone()[0]
    import json
    data = json.loads(open(receipt).read())
    assert data["measurement_method"] == "dom" and data["reference"]["sha256"].startswith("sha256:")
    assert any(m.get("property") == "width" for f in data["frames"] for m in f["measurements"])


def test_broken_interaction_is_a_product_failure_not_invalid(env):
    con, out, wt, wenv = _setup(env, BROKEN, port=_port())
    g = _gate(con)
    assert g["verdict"] == "CHANGES_REQUIRED" and g["evidence_status"] == "COMPARABLE", g
    f = con.execute("SELECT summary FROM findings WHERE gate_kind='visual'").fetchone()[0]
    assert "did not produce nav.open" in f
    assert not [c for c in env.calls() if c["role"] == "visual_reviewer"]  # no judgment spent


def test_clipped_element_is_measured_material_drift(env):
    con, out, wt, wenv = _setup(env, CLIPPED, port=_port())
    g = _gate(con)
    assert g["verdict"] == "CHANGES_REQUIRED", g
    rows = [r[0] for r in con.execute("SELECT measurement_json FROM findings WHERE gate_kind='visual'")]
    assert any("viewport clearance" in (r or "") or "scrollWidth" in (r or "") for r in rows), rows


def test_wrong_state_is_invalid_comparison_then_blocks_after_one_recapture(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), extra="  auth: [data-test=signed-in]\n")
    g = _gate(con)
    assert g["evidence_status"] == "INVALID_COMPARISON" and g["verdict"] == "UNAVAILABLE", g
    assert g["recaptures"] == 1
    assert _task(con)["status"] == "blocked"


def test_no_reference_passes_with_fidelity_unmeasured(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), reference=None)
    g = _gate(con)
    assert g["verdict"] == "PASS" and "fidelity unmeasured" in (g["summary"] or ""), g


def test_route_without_image_capability_never_passes(env):
    con, out, wt, wenv = _setup(env, PAGE, port=_port(), probe="PROBE NO_IMAGE")
    g = _gate(con)
    assert g["verdict"] == "UNAVAILABLE", g
    assert _task(con)["status"] == "blocked"
    assert con.execute("SELECT COUNT(*) FROM capability_proofs WHERE result='pass'").fetchone()[0] == 0


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
