"""#790: a codex reviewer pane whose brief pointer sits typed in the composer at launch.

The delivery gave up before the agent UI took the pointer (a slow start), so no Enter was
sent and the launch reported "submit it". The launch now re-samples the pane: an idle agent
that holds the pointer gets Enter through the fenced send path, a working one gets none (#583)
and a mismatched pane gets nothing (#579). A pointer Enter cannot submit marks the dispatch
for one retry on the same route.
"""
from __future__ import annotations

import contextlib
import json

import pytest

from office import dispatch

from test_herdr_agent_launch import BUSY, EMPTY, _calls, _launch_events, launch_in_herdr
from test_herdr_landing import _claude_box, _sends


def _held(brief) -> str:
    return _claude_box(f"Read and carry out the review brief at {brief} exactly.")


@pytest.fixture
def late_ui(monkeypatch):
    """The delivery returns unlanded without sending anything: the pointer is typed after it gave up."""
    monkeypatch.setattr(dispatch, "_deliver_prompt", lambda *a, **kw: False)


def _launch(env, monkeypatch, *, reads, gets=(), role="reviewer", **kw):
    return launch_in_herdr(env, monkeypatch, reads=reads, gets=gets, adapter="codex", model="m-x", effort="high",
                           role=role, brief="You are a code reviewer\n", output=role != "worker", owned_cwd=True, **kw)


@pytest.mark.approved
def test_held_pointer_at_launch_is_submitted_with_enter(env, monkeypatch, late_ui):
    # sample read, pane view, _submit_held's check, then the screen after Enter.
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=lambda brief: [_held(brief)] * 3 + [BUSY])
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    prompts, enters, typed = _sends(_calls(state_file))
    assert prompts == [] and typed == [] and len(enters) == 1 and enters[0][:3] == ["pane", "send-keys", res["pane"]]
    assert _launch_events(env, run) == []
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["prompt_landed"] is True
    assert "fallback_reason" not in spec and "retry_same_route" not in spec and "identity_failure" not in spec


@pytest.mark.approved
def test_enter_that_cannot_submit_says_so_and_marks_one_same_route_retry(env, monkeypatch, late_ui):
    # Codex reviewer recovery is bounded to 1 Enter (#790) even when OFFICE_HERDR_ENTER_TRIES > 1
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=lambda brief: [_held(brief)],
                                            settings={"OFFICE_HERDR_ENTER_TRIES": "2"})
    assert res["launcher"] == "herdr" and res["prompt_landed"] is False
    prompts, enters, typed = _sends(_calls(state_file))
    assert prompts == [] and typed == [] and len(enters) == 1
    events = _launch_events(env, run)
    assert len(events) == 1 and "typed but unsubmitted" in events[0] and "after Office pressed Enter" in events[0]
    assert "retries it once on the same route" in events[0]
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["retry_same_route"] == "held-pointer" and "fallback_reason" not in spec


@pytest.mark.approved
def test_worker_that_cannot_submit_is_not_marked_for_a_reviewer_retry(env, monkeypatch, late_ui):
    state_file, run, d, ddir, res = _launch(
        env, monkeypatch, role="worker", reads=lambda brief: [f"> Read and carry out the brief at {brief} exactly."],
        settings={"OFFICE_HERDR_ENTER_TRIES": "1"})
    assert res["prompt_landed"] is False
    events = _launch_events(env, run)
    assert len(events) == 1 and "typed but unsubmitted" in events[0] and "retries" not in events[0]
    assert "retry_same_route" not in json.loads((ddir / "launch.json").read_text())


@pytest.mark.approved
def test_working_agent_with_the_pointer_in_scrollback_gets_no_enter(env, monkeypatch, late_ui):
    state_file, run, d, ddir, res = _launch(env, monkeypatch, gets=["working"], reads=lambda brief: [_held(brief)])
    assert res["prompt_landed"] is False
    assert _sends(_calls(state_file)) == ([], [], [])
    assert _launch_events(env, run) == []
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["landing_unconfirmed_working"] is True and "retry_same_route" not in spec


@pytest.mark.approved
def test_pane_reassigned_before_enter_sends_nothing(env, monkeypatch, late_ui):
    @contextlib.contextmanager
    def reassigned():
        raise dispatch.PaneMismatch("pane was reassigned to dispatch Dother")
        yield

    monkeypatch.setattr(dispatch, "_send_guard", lambda run, dispatch_, pane: reassigned)
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=lambda brief: [_held(brief)])
    assert res["launcher"] == "herdr" and res["prompt_landed"] is False
    assert _sends(_calls(state_file)) == ([], [], [])
    events = _launch_events(env, run)
    assert len(events) == 1 and "pane-mismatch" in events[0] and "brief NOT sent" in events[0]
    spec = json.loads((ddir / "launch.json").read_text())
    assert "reassigned" in spec["identity_failure"] and "retry_same_route" not in spec


# Unit tier (no env fixture): `_settle_unlanded_pointer` against a scripted pane and herdr.

POINTER = "Read and carry out the review brief at /runs/r/dispatches/D1/brief.md exactly. Write your complete review."


class _Pane:
    """Scripted herdr: `views` are successive pane reads, Enter is recorded, `submits` says which Enter lands."""

    def __init__(self, monkeypatch, views, *, status="idle", submits=True):
        self.views, self.enters, self.notices, self.status, self.submits = list(views), [], [], status, submits
        monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
        monkeypatch.setenv("OFFICE_HERDR_ENTER_TRIES", "2")
        monkeypatch.setattr(dispatch, "_pane_view", self.view)
        monkeypatch.setattr(dispatch, "_herdr_agent_sample", lambda name: {"status": self.status, "busy": None})
        monkeypatch.setattr(dispatch, "_prompt_landed", lambda *a, **kw: "landed" if self.entered and self.submits else "")
        monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *a: self.enters.append(a))
        monkeypatch.setattr(dispatch, "_launch_notice", lambda run, d, text: self.notices.append(text))

    entered = property(lambda self: bool(self.enters))

    def view(self, name):
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]

    def settle(self, *, worker=False, guard=None):
        spec: dict = {}
        got = dispatch._settle_unlanded_pointer({}, {"id": "D1", "task_id": "T1"}, spec, "a", "w1:p1", POINTER, None,
                                                guard, worker=worker)
        return got, spec


def test_idle_agent_holding_the_pointer_gets_enter_and_lands(monkeypatch):
    pane = _Pane(monkeypatch, [_claude_box(POINTER)])
    (landed, mismatch), spec = pane.settle()
    assert landed is True and mismatch is None and spec == {}
    assert pane.enters == [("pane", "send-keys", "w1:p1", "Enter")] and pane.notices == []


def test_enter_that_cannot_submit_marks_a_reviewer_and_not_a_worker(monkeypatch):
    pane = _Pane(monkeypatch, [_claude_box(POINTER)], submits=False)
    (landed, _), spec = pane.settle()
    assert landed is False and spec == {"retry_same_route": "held-pointer"} and len(pane.enters) == 2
    assert "after Office pressed Enter" in pane.notices[0] and "retries it once on the same route" in pane.notices[0]
    pane = _Pane(monkeypatch, [_claude_box(POINTER)], submits=False)
    (landed, _), spec = pane.settle(worker=True)
    assert landed is False and spec == {} and "retries" not in pane.notices[0]


def test_working_agent_with_the_pointer_in_scrollback_is_sent_nothing(monkeypatch):
    pane = _Pane(monkeypatch, [_claude_box(POINTER)], status="working")
    (landed, mismatch), spec = pane.settle()
    assert (landed, mismatch) == (False, None) and spec == {"landing_unconfirmed_working": True}
    assert pane.enters == [] and pane.notices == []


def test_composer_without_the_pointer_gets_no_enter(monkeypatch):
    pane = _Pane(monkeypatch, [EMPTY])
    (landed, _), spec = pane.settle()
    assert landed is False and spec == {} and pane.enters == []
    assert "did not land" in pane.notices[0]


def test_guard_refusal_sends_nothing_and_reports_the_mismatch(monkeypatch):
    @contextlib.contextmanager
    def refuse():
        raise dispatch.PaneMismatch("pane w1:p1 was reassigned")
        yield

    pane = _Pane(monkeypatch, [_claude_box(POINTER)])
    (landed, mismatch), spec = pane.settle(guard=refuse)
    assert landed is False and "reassigned" in mismatch and spec == {}
    assert pane.enters == [] and pane.notices == []


def test_agent_herdr_no_longer_reports_gets_no_enter(monkeypatch):
    pane = _Pane(monkeypatch, [_claude_box(POINTER)])
    monkeypatch.setattr(dispatch, "_herdr_agent_sample", lambda name: None)
    (landed, mismatch), spec = pane.settle()
    assert (landed, mismatch) == (False, None) and pane.enters == []


# A reviewer that ends without a reply file is not recorded as a plain success.

def _ended_summary(env, monkeypatch, *, reply: str | None, kind="reviewer") -> str:
    from test_herdr_agent_launch import _fake, _live_dispatch
    from office import paths
    _fake(env, monkeypatch)
    run, d = _live_dispatch(env, monkeypatch)
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    con = env.con()
    try:
        with con:
            con.execute("UPDATE dispatches SET kind=?, role=? WHERE id=?", (kind, kind, d["id"]))
    finally:
        con.close()
    out = ddir / "reply.txt"
    if reply is not None:
        out.write_text(reply)
    (ddir / "launch.json").write_text(json.dumps({"herdr_agent": "office-D", "kind": kind, "output": str(out)}))
    monkeypatch.setattr(dispatch, "watch_herdr_agent", lambda *a, **kw: (0, "success"))
    monkeypatch.setattr(dispatch, "_mark", lambda *a, **kw: None)
    dispatch.supervise(d["id"])
    con = env.con()
    try:
        return con.execute("SELECT summary FROM events WHERE kind='dispatch.ended' AND dispatch_id=?", (d["id"],)).fetchone()[0]
    finally:
        con.close()


@pytest.mark.approved
@pytest.mark.parametrize("reply", [None, ""])
def test_reviewer_ending_without_a_reply_file_says_so(env, monkeypatch, reply):
    summary = _ended_summary(env, monkeypatch, reply=reply)
    assert summary.endswith("reviewer ended: success with an empty reply: no reply file was written"), summary


@pytest.mark.approved
def test_reviewer_with_a_reply_file_ends_as_a_plain_success(env, monkeypatch):
    assert _ended_summary(env, monkeypatch, reply="VERDICT: PASS").endswith("reviewer ended: success")


@pytest.mark.approved
def test_worker_without_a_reply_file_ends_as_a_plain_success(env, monkeypatch):
    assert _ended_summary(env, monkeypatch, reply=None, kind="worker").endswith("worker ended: success")
