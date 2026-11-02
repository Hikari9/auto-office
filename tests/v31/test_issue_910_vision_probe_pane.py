"""#910: a vision conformance probe pane says it is a probe, not a visual review, and is closed once its
reply is read."""
from __future__ import annotations

import uuid

from office import db, pane_labels, version

RUN = {"id": "r1", "repo_root": "/work/auto-office"}
PROBE = {"id": "D1", "role": "visual_reviewer", "kind": "conformance", "harness": "agy",
         "model": "gemini-3.8-flash", "effort": "low"}


def test_a_probe_pane_is_labelled_vision_probe():
    label = pane_labels.format_label(RUN, PROBE, "plan", None, None)
    assert "vision probe" in label and "visual review" not in label
    review = pane_labels.format_label(RUN, {**PROBE, "kind": "reviewer"}, "L-T1", None, None)
    assert "visual review" in review


def test_the_probe_pane_is_closed_after_its_reply_is_read(tmp_path, monkeypatch):
    from office import conformance, dispatch, state
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    con = db.connect(tmp_path / "runs.db")
    run_id = "R" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO runs(id, status, requirements_version, plan_version, office_version) "
                "VALUES(?, 'executing', 1, 1, ?)", (run_id, version.current()))
    con.commit()
    run = state.get_run(con, run_id)
    answered, closed = {}, []

    def launch(run_, d, kind, ddir, *, output, **kw):
        brief = (ddir / "brief.md").read_text()
        assert "visual conformance probe" in brief
        answered["d"] = d["id"]
        # read the digits and colour back from the probe's own call: the test answers it correctly
        output.write_text(f"PROBE {answered['digits']} {answered['color']}\nEVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS\n")
        con.execute("UPDATE dispatches SET terminal_classification='success', exit_code=0 WHERE id=?", (d["id"],))
        con.commit()

    real_probe_image = conformance.probe_image

    def probe_image(path, digits, color):
        answered.update(digits=digits, color=color)
        return real_probe_image(path, digits, color)

    monkeypatch.setattr(conformance, "probe_image", probe_image)
    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(dispatch, "reclaim_pane", lambda run_, did, **kw: closed.append(did) or "closed")
    cand = {"harness": "agy", "invocation_model_id": "gemini-3.8-flash", "effort": "low", "adapter_id": "agy",
            "model_id": "gemini-3.8-flash", "harness_version": "1"}
    out = conformance.probe_vision(con, run, cand, {"id": "agy"})
    assert out["result"] == "pass", out
    assert closed == [answered["d"]]
