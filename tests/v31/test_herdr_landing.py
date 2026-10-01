"""Brief-pointer landing in herdr panes (rock-mcp run 2fc0f696, 2026-09-30).

C1: codex opens a new dispatch directory on a "Trust this folder?" dialog that
eats the pointer. C3: a busy footer is not the only landed signal, and
`agent_status` is none at all. C4: nothing is typed into a pane whose agent is
not up, because that text goes to the shell.
"""
from __future__ import annotations

import json

import pytest

from test_herdr_agent_launch import BUSY, EMPTY, _calls, _fake, _launch_events, _live_dispatch, launch_in_herdr

TRUST = ("Folder access\n  /runs/x/dispatches/D1\n  Trust this folder? Codex can read, edit, and run files here\n"
         "> 1. Trust and continue\n  2. Quit\n  enter continue · esc quit")


def _launch(env, monkeypatch, *, reads, adapter="codex", owned=True):
    return launch_in_herdr(env, monkeypatch, reads=reads, adapter=adapter, model="m-x", effort="high", role="reviewer",
                           brief="You are a code reviewer\n", output=True, owned_cwd=owned)


def test_codex_interactive_launch_pre_trusts_its_cwd(tmp_path):
    from office import adapters
    codex = adapters.load_all()["codex"]
    for kind in ("worker", "reviewer", "vision"):
        args, _ = adapters.interactive_argv(codex, kind, model="m", effort="high", cwd=tmp_path)
        key = json.dumps(str(tmp_path.resolve()))
        assert f'projects={{{key}={{trust_level="trusted"}}}}' in args, (kind, args)
        # Headless `codex exec` shows no dialog and keeps its argv.
        argv, _ = adapters.build_argv(codex, kind, model="m", effort="high", cwd=tmp_path)
        assert not any(a.startswith("projects=") for a in argv)


@pytest.mark.approved
def test_trust_dialog_on_an_office_dir_is_answered_before_the_prompt(env, monkeypatch):
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=[TRUST, BUSY])
    assert res["prompt_landed"] is True
    calls = _calls(state_file)
    enter = next(i for i, c in enumerate(calls) if c[:2] == ["pane", "send-keys"] and c[-1] == "Enter")
    prompt = next(i for i, c in enumerate(calls) if c[:2] == ["agent", "prompt"])
    assert enter < prompt
    assert _launch_events(env, run) == []


@pytest.mark.approved
def test_trust_dialog_outside_office_dirs_is_reported_not_answered(env, monkeypatch):
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=[TRUST], owned=False)
    assert res["prompt_landed"] is False
    calls = _calls(state_file)
    assert not any(c[:2] == ["pane", "send-keys"] for c in calls)
    assert not any(c[:2] in (["agent", "prompt"], ["pane", "send-text"]) for c in calls)
    events = _launch_events(env, run)
    assert len(events) == 1 and "folder-trust dialog" in events[0] and "re-prompt it" in events[0]


@pytest.mark.approved
def test_nothing_is_typed_into_a_pane_without_an_agent(env, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_NO_AGENT", "1")
    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=["rico@mac ~ %"])
    assert res["prompt_landed"] is False
    calls = _calls(state_file)
    assert not any(c[:2] == ["pane", "send-text"] for c in calls)
    assert not any(c[:2] == ["pane", "run"] and "brief.md" in " ".join(c) for c in calls)


def test_rising_claude_ctx_counts_as_landed_without_a_busy_footer(env, monkeypatch):
    narrow = "> \n  Opus 5.5 · ctx: {}k · $0"
    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude",
                                            reads=[narrow.format(0), narrow.format(0), narrow.format(14)])
    assert res["prompt_landed"] is True
    assert not any(c[:2] == ["pane", "send-text"] for c in _calls(state_file))


@pytest.mark.approved
def test_transcript_logging_the_prompt_counts_as_landed(env, monkeypatch):
    from office import transcripts
    monkeypatch.setattr(transcripts, "prompt_seen", lambda *a, **k: True)
    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude", reads=[EMPTY])
    assert res["prompt_landed"] is True
    assert not any(c[:2] == ["pane", "send-text"] for c in _calls(state_file))


def test_prompt_seen_reads_the_claude_transcript(env, monkeypatch):
    from office import transcripts
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env.home / ".claude"))
    cwd, brief = env.tmp / "review", env.tmp / "dispatches" / "D1" / "brief.md"
    assert not transcripts.prompt_seen("claude", marker=str(brief), cwd=cwd)
    d = env.home / ".claude" / "projects" / transcripts.claude_slug(cwd)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "user", "cwd": str(cwd), "message": {
        "role": "user", "content": f"Read and carry out the review brief at {brief} exactly."}}) + "\n")
    assert transcripts.prompt_seen("claude", marker=str(brief), cwd=cwd)


def test_pointer_left_in_the_composer_is_submitted_not_retyped(env, monkeypatch):
    def reads(brief):
        typed = f"› Read and carry out the review brief at {brief} exactly."
        return [EMPTY, EMPTY, typed, typed, BUSY]

    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=reads)
    assert res["prompt_landed"] is True
    calls = _calls(state_file)
    assert not any(c[:2] == ["pane", "send-text"] for c in calls)
    assert any(c[:2] == ["pane", "send-keys"] and c[-1] == "Enter" for c in calls)


def test_external_read_only_reviewer_reply_is_taken_from_its_transcript(env, monkeypatch):
    """C2: the printed external argv is read-only, so reply.txt never appears;
    the watcher takes the review from the session the brief pointer started."""
    from test_herdr_review_capture import FINAL_CR, _codex_transcript, _watch_reviewer
    from office import dispatch
    d, cwd, brief, out, spec = _watch_reviewer(env, monkeypatch, "codex", "")
    elsewhere = env.tmp / "dispatch-dir"  # the person started it in the dispatch dir, not spec cwd
    _codex_transcript(env.home, elsewhere, brief, FINAL_CR)
    assert dispatch.watch_external_output(d["id"], spec, poll=0) == (0, "success")
    assert out.read_text() == FINAL_CR


def test_external_transcript_without_a_verdict_keeps_waiting(env, monkeypatch):
    from test_herdr_review_capture import _codex_transcript, _watch_reviewer
    from office import dispatch
    d, cwd, brief, out, spec = _watch_reviewer(env, monkeypatch, "codex", "")
    _codex_transcript(env.home, cwd, brief, "Checking the diff first.")
    assert dispatch._external_transcript_reply(dispatch.state_dispatch(d["id"]), spec) is None


@pytest.mark.approved
def test_external_reviewer_instructions_allow_a_reply_and_warn_about_the_ui(env, monkeypatch):
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch
    d = {**d, "adapter_id": "codex", "model": "m-x", "effort": "high", "worktree": None}
    lines = dispatch.launch_instructions(run, d, output="/x/reply.txt")
    text = "\n".join(lines)
    assert "end your reply with the complete review" in text
    assert "Trust this folder?" in text and "never `pane run`" in text
    assert 'trust_level="trusted"' in text  # the printed codex argv pre-trusts its cwd


# Unsubmitted prompts: `agent prompt` types the text, but a TUI that is not
# ready to submit (a resumed Claude session, a slow start) can keep it in the
# composer. Office presses Enter for it and never sends the text again.

def _claude_box(text: str) -> str:
    """Claude's composer: bordered, a prompt glyph, the pointer wrapped over rows."""
    rows = [text[i:i + 30] for i in range(0, len(text), 30)]
    body = "\n".join(f"│ {'>' if i == 0 else ' '} {r:<30} │" for i, r in enumerate(rows))
    return f"╭{'─' * 34}╮\n{body}\n╰{'─' * 34}╯\n  Opus 5.5 · ctx: 0k · $0"


def _sends(calls):
    prompts = [c for c in calls if c[:2] == ["agent", "prompt"]]
    enters = [c for c in calls if c[1:2] == ["send-keys"] and c[-1] == "Enter"]
    typed = [c for c in calls if c[:2] == ["pane", "send-text"]]
    return prompts, enters, typed


def test_wrapped_pointer_left_unsubmitted_gets_one_enter_and_no_duplicate(env, monkeypatch):
    def reads(brief):
        held = _claude_box(f"Read and carry out the review brief at {brief} exactly.")
        return [EMPTY, EMPTY, held, held, BUSY]

    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude", reads=reads)
    assert res["prompt_landed"] is True
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 1 and typed == []
    assert _launch_events(env, run) == []


def test_collapsed_paste_placeholder_counts_as_unsubmitted(env, monkeypatch):
    held = "╭───╮\n│ > [Pasted text #1 +4 lines] │\n╰───╯"
    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude",
                                            reads=[EMPTY, EMPTY, held, held, BUSY])
    assert res["prompt_landed"] is True
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 1 and typed == []


def test_landed_prompt_gets_no_enter(env, monkeypatch):
    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude", reads=[EMPTY, EMPTY, BUSY])
    assert res["prompt_landed"] is True
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and enters == [] and typed == []


def test_enter_that_never_submits_is_bounded_then_reported(env, monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_ENTER_TRIES", "3")

    def reads(brief):
        return [EMPTY, EMPTY, _claude_box(f"Read and carry out the review brief at {brief} exactly.")]

    state_file, run, d, ddir, res = _launch(env, monkeypatch, adapter="claude", reads=reads)
    assert res["prompt_landed"] is False
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 3 and typed == []
    events = _launch_events(env, run)
    assert len(events) == 1 and "typed but unsubmitted" in events[0]
    assert f"herdr pane send-keys {res['pane']} Enter" in events[0] and "agent prompt" not in events[0]


def test_enter_that_empties_the_composer_counts_as_submitted(env, monkeypatch):
    # A narrow pane can show no landed signal; an Enter that cleared the
    # composer was the submit, so the pointer is not typed again.
    def reads(brief):
        held = f"› Read and carry out the review brief at {brief} exactly."
        return [EMPTY, EMPTY, held, held, EMPTY]

    state_file, run, d, ddir, res = _launch(env, monkeypatch, reads=reads)
    assert res["prompt_landed"] is True
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 1 and typed == []


def _reviewer_reprompt(env, monkeypatch, reads):
    state_file = _fake(env, monkeypatch, gets=["idle"], reads=reads)
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("OFFICE_HERDR_LAND_TIMEOUT", "0")
    monkeypatch.setenv("OFFICE_HERDR_KEY_DELAY", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_WAIT", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_POLL", "0")
    from office import gates, review_parse
    d = {**d, "launcher": "herdr", "pane_id": "w1:p7", "role": "code_reviewer"}
    run = {**run, "gates": {"review_reprompt_max": 1}}
    con = env.con()
    try:
        got = gates._reprompt_until_valid(con, run, d, env.tmp, env.tmp / "reply.txt", review_parse.parse(""),
                                          plan_review=False, visual=False)
    finally:
        con.close()
    return state_file, got


def test_reviewer_reprompt_left_unsubmitted_gets_enter_not_a_second_prompt(env, monkeypatch):
    held = "› Office could not read your review (empty reply). Write your complete review again"
    state_file, (text, parsed, reason) = _reviewer_reprompt(env, monkeypatch, [EMPTY, held, held, BUSY])
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 1 and typed == []
    assert enters[0][:3] == ["pane", "send-keys", "w1:p7"]
    assert "unsubmitted" not in (reason or "")


def test_reviewer_reprompt_that_never_submits_stops_with_an_enter_hint(env, monkeypatch):
    monkeypatch.setenv("OFFICE_HERDR_ENTER_TRIES", "2")
    held = "› Office could not read your review (empty reply). Write your complete review again"
    state_file, (text, parsed, reason) = _reviewer_reprompt(env, monkeypatch, [EMPTY, held])
    prompts, enters, typed = _sends(_calls(state_file))
    assert len(prompts) == 1 and len(enters) == 2 and typed == []
    assert "typed but unsubmitted" in reason and "send-keys" in reason
