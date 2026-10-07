"""F4: outcome labels written at closeout from recorded evidence."""
from __future__ import annotations

import re

import pytest

from conftest import GOOD_ADD, approved_run

from office import scoring

HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _accepted_run(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    return con


def _labels(con):
    return [dict(r) for r in con.execute("SELECT * FROM outcome_labels").fetchall()]


@pytest.mark.approved
def test_close_labels_accepted_work_once(env):
    con = _accepted_run(env)
    run_id = con.execute("SELECT id FROM runs WHERE phase IS NOT NULL").fetchone()[0]
    # A dispatch with no recorded evidence must stay unlabeled.
    con.execute("INSERT INTO dispatches(id, run_id, role, status) VALUES('Dbare', ?, 'executor', 'failed')", (run_id,))
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code == 0, out
    labels = _labels(env.con())
    accepted = env.con().execute(
        "SELECT r.dispatch_id FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id").fetchone()[0]
    assert [l["dispatch_id"] for l in labels] == [accepted]
    label = labels[0]
    assert label["label"] == "verified_no_observed_failure"
    assert label["label"] in {k[0] for k in scoring._NARRATIVE_BASE}
    assert HASH.match(label["evidence_hash"])
    assert label["primary_attribution"] is None
    env.office("close", "--handoff", "https://example.test/pr/1")
    assert scoring.label_run_outcomes(env.con(), run_id, "closed") == []
    assert len(_labels(env.con())) == 1


@pytest.mark.approved
def test_abandon_labels_evidenced_dispatches_abandoned(env):
    con = _accepted_run(env)
    run_id = con.execute("SELECT id FROM runs WHERE phase IS NOT NULL").fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, role, status) VALUES('Dbare', ?, 'executor', 'failed')", (run_id,))
    code, out = env.office("close", "--abandon", "no longer needed")
    assert code == 0, out
    labels = _labels(env.con())
    assert labels and all(l["label"] == "abandoned" for l in labels)
    assert all(HASH.match(l["evidence_hash"]) and l["primary_attribution"] is None for l in labels)
    assert "Dbare" not in {l["dispatch_id"] for l in labels}
    assert len({l["dispatch_id"] for l in labels}) == len(labels)
