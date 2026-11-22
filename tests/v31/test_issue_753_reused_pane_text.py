"""#753: a dispatch that takes over a reused pane reads only its own output: the revoked
executor's usage-limit text above its setup line must not stall it."""
from __future__ import annotations

from office import dispatch, paths, rerun

OLD = ("codex working...\n"
       "■ You've hit your usage limit. Upgrade to Pro or try again at 9:30 PM.\n"
       "Usage limit reached ... Ask Codex to do anything\n")


def _pane(d, width=60):
    marker = str(paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "shell-ready")
    setup = f"$ cd /wt && export OFFICE_DISPATCH_ID={d['id']} && touch {marker}"
    wrapped = "\n".join(setup[i:i + width] for i in range(0, len(setup), width))  # the pane wraps it
    return OLD + wrapped + "\npi  claude-opus-5-5 high  working on calc.py\n"


def test_text_above_the_dispatchs_own_setup_line_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path))
    d = {"id": "D2", "run_id": "r1", "launcher": "herdr"}
    text = _pane(d)
    assert dispatch._usage_limit(text), "the raw pane shows the revoked executor's limit"
    own = rerun.own_pane_text(text, d)
    assert "usage limit" not in own.lower() and "working on calc.py" in own
    assert dispatch._usage_limit(own) is None


def test_without_the_setup_line_on_screen_the_text_stands(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path))
    d = {"id": "D2", "run_id": "r1"}
    assert rerun.own_pane_text(OLD, d) == OLD
    assert rerun.own_pane_text(_pane({"id": "D1", "run_id": "r1"}), d).startswith("codex working")
