"""#656: `office model verify <route> --evidence <run>/<task>` promotes a model added with `office model add`
from `unverified:` to `local-evidence:` on cited acceptances it produced under independent review. Explicit
only (Rico, 2026-11-02): nothing promotes a model on its own."""
from __future__ import annotations

import json

import pytest

from test_self_review_ledger import _dispatch, write_ledger

pytestmark = [pytest.mark.integration, pytest.mark.approved]

ROUTE = "pi/my-model"


def _overlay_rows(env):
    from office import user_catalog
    return [r for r in user_catalog.load()["models"] if r.get("model_id") == "my-model"]


def _accepted_on_route(env, effort="high"):
    """A real dispatch and submission, then recorded as accepted on the user-added route."""
    wenv, wt, d = _dispatch(env)
    write_ledger(wt)
    assert env.office("submit", cwd=wt, env=wenv)[0] == 0
    con = env.con()
    rev = con.execute("SELECT id FROM revisions WHERE task_id='T1' ORDER BY seq DESC").fetchone()[0]
    con.execute("UPDATE tasks SET status='accepted', accepted_revision_id=? WHERE id='T1'", (rev,))
    con.execute("UPDATE dispatches SET harness='pi', adapter_id='pi', model='prov/my-model', effort=?, "
                "triple=? WHERE id=?", (effort, f"pi@1/my-model@{effort}", d["id"]))
    con.commit()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    return f"{run_id[:8]}/T1"


def _add(env):
    code, out = env.office("model", "add", "pi/prov/my-model", "--effort", "high,low")
    assert code == 0, out


def test_verify_promotes_only_the_cited_effort_to_local_evidence(env):
    _add(env)
    ref = _accepted_on_route(env)
    code, out = env.office("model", "verify", ROUTE, "--evidence", ref)
    assert code == 0 and "verified pi/my-model@high (local-evidence:)" in out, out
    assert "still unverified: pi/my-model@low" in out, out
    rows = {r["effort"]: r["invocation_source"] for r in _overlay_rows(env)}
    assert rows["high"].startswith("local-evidence: office model verify on ") and ref in rows["high"], rows
    assert rows["low"].startswith("unverified:"), rows
    code, out = env.office("model", "verify", f"{ROUTE}@high", "--evidence", ref)
    assert code == 0 and "already has a local-evidence: invocation source" in out, out


def test_verify_refuses_evidence_that_does_not_prove_the_route(env):
    _add(env)
    ref = _accepted_on_route(env, effort="medium")  # a different effort: no row of this route ran it
    code, out = env.office("model", "verify", ROUTE, "--evidence", ref)
    assert code != 0 and "evidence-rejected" in out, out
    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    code, out = env.office("model", "verify", ROUTE, "--evidence", ref)
    assert code != 0 and "not accepted" in out, out
    code, out = env.office("model", "verify", ROUTE)
    assert code != 0 and "--evidence" in out, out
    assert all(r["invocation_source"].startswith("unverified:") for r in _overlay_rows(env))


def test_verify_refuses_a_run_without_independent_review(env):
    _add(env)
    ref = _accepted_on_route(env)
    con = env.con()
    gates = json.loads(con.execute("SELECT gates_json FROM runs").fetchone()[0])
    con.execute("UPDATE runs SET gates_json=?", (json.dumps({**gates, "code_review": False}),))
    con.commit()
    code, out = env.office("model", "verify", ROUTE, "--evidence", ref)
    assert code != 0 and "without independent code review" in out, out


def test_verify_refuses_a_shipped_row(env):
    code, out = env.office("model", "verify", "claude/claude-opus-5-5", "--evidence", "x/T1")
    assert code != 0 and "not a row added with office model add" in out, out
