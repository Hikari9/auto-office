"""#1027 (Rico, 2026-11-02): executors and reviewers launch the same way. A reviewer or visual reviewer profile
gets a Herdr pane exactly when its harness's worker profile does; the headless CLI form is the fallback, and a
reviewer that took it is announced where office status reads launch notices."""
from __future__ import annotations

import pytest

from office import adapters, gates


def _seeds():
    return {h: a for h, a in adapters.load_all().items() if adapters.profile(a, "worker")}


@pytest.mark.parametrize("harness", sorted(_seeds()))
def test_every_review_profile_launches_like_its_worker(harness):
    adapter = _seeds()[harness]
    worker = adapters.pane_capable(adapter, "worker")
    for kind in ("reviewer", "vision"):
        if adapters.profile(adapter, kind):
            assert adapters.pane_capable(adapter, kind) == worker, (harness, kind)


def test_pi_reviewer_keeps_a_headless_cli_form_for_the_fallback():
    prof = adapters.profile(adapters.load_all()["pi"], "reviewer")
    assert "--print" in prof["argv"] and prof["output"] == "stdout", "the CLI fallback replies on stdout"
    tools = prof["argv"][prof["argv"].index("--tools") + 1].split(",")
    assert "write" not in tools, "the headless fallback stays read-only"


@pytest.mark.parametrize("launcher, announced", [("process", True), ("process-fallback", True), ("herdr", False)])
def test_a_pane_capable_reviewer_that_ran_headless_is_announced(monkeypatch, launcher, announced):
    emitted = []
    monkeypatch.setattr(gates.state, "emit", lambda con, run, kind, summary, **kw: emitted.append((kind, summary)))
    monkeypatch.setattr(gates.db, "transaction", lambda con: __import__("contextlib").nullcontext())
    d = {"id": "D1", "role": "code_reviewer", "task_id": "T1", "harness": "pi", "adapter_id": "pi",
         "triple": "pi@1/gpt-6-luna@high", "launcher": launcher}
    gates._note_headless_reviewer(None, {"id": "r1"}, d, "reviewer")
    assert bool(emitted) is announced, emitted
    if announced:
        assert emitted[0][0] == "launch" and "CLI fallback" in emitted[0][1]


def test_a_headless_only_harness_is_not_announced(monkeypatch):
    emitted = []
    monkeypatch.setattr(gates.state, "emit", lambda *a, **k: emitted.append(a))
    adapter = {"office_profiles": {"reviewer": {"argv": ["x"], "prompt": "stdin", "output": "stdout"}}}
    monkeypatch.setattr(adapters, "load_all", lambda: {"headless": adapter})
    gates._note_headless_reviewer(None, {"id": "r1"}, {"id": "D1", "role": "code_reviewer", "harness": "headless",
                                                       "launcher": "process"}, "reviewer")
    assert not emitted


def test_the_pi_reviewer_pane_form_renders_with_the_reply_write_grant(tmp_path):
    """Herdr by default: the rendered pane argv pins the model and effort and may write only its reply (Q22)."""
    args, kind = adapters.interactive_argv(adapters.load_all()["pi"], "reviewer", model="openai/gpt-6-luna",
                                           effort="high", cwd=tmp_path, output=tmp_path / "reply.txt")
    assert kind == "pi" and args[args.index("--tools") + 1] == "read,grep,find,ls,write"
    assert args[args.index("--model") + 1] == "openai/gpt-6-luna" and "--print" not in args
