"""Pane-hosted reviewer replies, the pane ledger owner, unedited contract
amendments, and the integration missing-dependency hint.

All transcripts and pane captures here are synthetic.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, ROOT, start_inline
from test_herdr_agent_launch import BUSY, _fake, _live_dispatch

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

CLAUDE_PANE = """\
╭──────────────────────────────────────╮
│ ✻ Welcome                            │
╰──────────────────────────────────────╯
> Read and carry out the review brief at /tmp/example/brief.md exactly.
⏺ Read(brief.md)
  ⎿  Read 40 lines
⏺ VERDICT: CHANGES_REQUIRED
  FINDING F1 | material | calc.py:2 | subtracts instead of adds | return a + b
──────────────────────────────────────────
>
  ? for shortcuts
"""

CODEX_PANE = """\
• Explored
  └ Read brief.md

• VERDICT: PASS

› Improve documentation in @filename

  example-model high · 100% left · ~/review
"""


# ------------------------------------------------------------------ parser tolerance


def _parse(text, **kw):
    from office import gates, review_parse
    return review_parse.parse(gates._last_block(text), **kw)


def test_bulleted_verdict_from_a_pane_parses():
    p = _parse(CODEX_PANE)
    assert p.valid and p.verdict == "PASS", p.errors


def test_claude_pane_capture_keeps_the_finding_and_ignores_chrome():
    p = _parse(CLAUDE_PANE)
    assert p.valid and p.verdict == "CHANGES_REQUIRED", p.errors
    assert [f["code"] for f in p.findings] == ["F1"]
    assert p.findings[0]["location"] == "calc.py:2"


def test_box_gutters_and_bulleted_findings_before_the_verdict():
    text = ("│ FINDING F2 | minor | a.py:1 | a nit | rename it          │\n"
            "│ • VERDICT: PASS                                           │\n")
    p = _parse(text)
    assert p.valid and p.verdict == "PASS", p.errors
    assert [f["code"] for f in p.findings] == ["F2"]


def test_plain_replies_parse_as_before():
    p = _parse("- **VERDICT: CHANGES_REQUIRED**\nFINDING F1 | material | x.py:3 | wrong | fix it")
    assert p.valid and p.verdict == "CHANGES_REQUIRED"
    assert _parse("no verdict here").errors == ["no VERDICT line"]


# ------------------------------------------------------------------ transcripts


def _claude_transcript(home: Path, cwd: Path, brief: Path, final: str, *, name="s1", tool_only=False):
    from office import transcripts
    d = home / ".claude" / "projects" / transcripts.claude_slug(cwd)
    d.mkdir(parents=True, exist_ok=True)
    prompt = f"Read and carry out the review brief at {brief} exactly. Write your complete review to reply.txt."
    first = ({"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": f"brief: {brief}"}]}} if tool_only else
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": prompt}})
    rows = [
        first,
        {"type": "assistant", "cwd": str(cwd), "message": {"id": "m1", "role": "assistant",
                                                          "content": [{"type": "text", "text": "Reading the brief."}]}},
        {"type": "assistant", "cwd": str(cwd), "message": {"id": "m1", "role": "assistant",
                                                          "content": [{"type": "tool_use", "id": "t1", "name": "Read"}]}},
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "def add(a, b): return a - b"}]}},
        {"type": "assistant", "cwd": str(cwd), "message": {"id": "m2", "role": "assistant",
                                                          "content": [{"type": "text", "text": final}]}},
    ]
    f = d / f"{name}.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def _codex_transcript(home: Path, cwd: Path, brief: Path, final: str):
    day = datetime.now(timezone.utc)
    d = home / ".codex" / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    d.mkdir(parents=True, exist_ok=True)
    msg = lambda role, kind, text: {"type": "response_item", "payload": {  # noqa: E731
        "type": "message", "role": role, "content": [{"type": kind, "text": text}]}}
    rows = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": str(cwd)}},
        msg("user", "input_text", "<environment_context>cwd</environment_context>"),
        msg("user", "input_text", f"Read and carry out the review brief at {brief} exactly."),
        msg("assistant", "output_text", "Checking the diff."),
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "ok"}},
        msg("assistant", "output_text", final),
    ]
    f = d / "rollout-2000-01-01T00-00-00-synthetic.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


FINAL_CR = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | subtracts instead of adds | return a + b"


def test_claude_transcript_final_reply(env):
    from office import transcripts
    cwd, brief = env.tmp / "review", env.tmp / "dispatches" / "D1" / "brief.md"
    cwd.mkdir()
    _claude_transcript(env.home, cwd, brief, FINAL_CR)
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, since=0) == FINAL_CR


def test_codex_transcript_final_reply(env):
    from office import transcripts
    cwd, brief = env.tmp / "review", env.tmp / "dispatches" / "D2" / "brief.md"
    cwd.mkdir()
    _codex_transcript(env.home, cwd, brief, "VERDICT: PASS")
    assert transcripts.final_reply("codex", marker=str(brief), cwd=cwd, since=0) == "VERDICT: PASS"


def test_orchestrator_transcript_that_only_printed_the_path_is_not_the_reply(env):
    """A session that saw the brief path in a tool result, or ran in another
    cwd, is not the reviewer's session."""
    from office import transcripts
    cwd, brief = env.tmp / "review", env.tmp / "dispatches" / "D3" / "brief.md"
    cwd.mkdir()
    _claude_transcript(env.home, env.repo, brief, "VERDICT: PASS", name="orch", tool_only=True)
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, since=0) is None
    other = env.tmp / "elsewhere"
    other.mkdir()
    _claude_transcript(env.home, other, brief, "VERDICT: PASS", name="other")
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, since=0) is None


def test_transcripts_older_than_the_dispatch_are_ignored(env):
    from office import transcripts
    cwd, brief = env.tmp / "review", env.tmp / "dispatches" / "D4" / "brief.md"
    cwd.mkdir()
    f = _claude_transcript(env.home, cwd, brief, FINAL_CR)
    os.utime(f, (1_000_000, 1_000_000))
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, since="2020-01-01T00:00:00+00:00") is None


def _watch_reviewer(env, monkeypatch, harness, pane_text):
    state_file = _fake(env, monkeypatch, gets=["done", "done", "done"])
    data = json.loads(state_file.read_text())
    data["content"] = pane_text
    state_file.write_text(json.dumps(data))
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    try:
        with con:
            con.execute("UPDATE dispatches SET harness=? WHERE id=?", (harness, d["id"]))
    finally:
        con.close()
    cwd = env.tmp / "review"
    cwd.mkdir(exist_ok=True)
    brief = env.tmp / "dispatches" / d["id"] / "brief.md"
    out = env.tmp / "reply.txt"
    spec = {"herdr_agent": "office-d", "output": str(out), "prompt_file": str(brief), "cwd": str(cwd)}
    return d, cwd, brief, out, spec


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_read_only_reviewer_reply_comes_from_its_transcript_not_the_pane(env, monkeypatch, harness):
    """The pane shows only chrome; the transcript has the full review."""
    from office import dispatch
    d, cwd, brief, out, spec = _watch_reviewer(env, monkeypatch, harness, "? for shortcuts\n")
    (_claude_transcript if harness == "claude" else _codex_transcript)(env.home, cwd, brief, FINAL_CR)
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")
    assert out.read_text() == FINAL_CR
    assert _parse(out.read_text()).verdict == "CHANGES_REQUIRED"


def test_pane_text_is_still_the_last_resort(env, monkeypatch):
    from office import dispatch
    d, cwd, brief, out, spec = _watch_reviewer(env, monkeypatch, "claude", "⏺ VERDICT: PASS")
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")
    assert _parse(out.read_text()).verdict == "PASS"


# ------------------------------------------------------------------ pane ledger


def _herdr_worker(env, monkeypatch):
    state_file = _fake(env, monkeypatch, reads=[BUSY])
    run, d = _live_dispatch(env, monkeypatch)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    from office import dispatch, paths
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    d = {**d, "adapter_id": "claude", "model": "fake-model", "effort": "high"}
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("ROLE executor\n")
    res = dispatch.launch(run, d, "worker", ddir, cwd=env.repo)
    rows = [json.loads(line) for line in (paths.run_dir(run["id"]) / "panes.jsonl").read_text().splitlines()]
    return run, d, res, rows


def test_ledger_row_records_its_owner_agent_and_kind(env, monkeypatch):
    run, d, res, rows = _herdr_worker(env, monkeypatch)
    row = rows[-1]
    assert row["pane_id"] == res["pane"]
    assert row["orchestrator_pane_id"] == "w1:pQ"
    assert row["agent"] == res["agent"] and row["kind"] == "claude"
    assert row["dispatch_id"] == d["id"] and row["run_id"] == run["id"]
    assert row["status"] == "working" and row["closed"] is False
    assert row["spawned_at"] and row["worktree"] == str(env.repo)


def test_ledger_owner_falls_back_to_the_split_anchor(env, monkeypatch):
    run, d = _live_dispatch(env, monkeypatch)
    from office import dispatch, paths
    (paths.run_dir(run["id"]) / "herdr-tab.json").write_text(json.dumps({"mode": "split", "anchor": "w1:pA", "panes": []}))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    dispatch._pane_ledger(run, d, "w1:p9", agent="office-x", kind="codex")
    row = json.loads((paths.run_dir(run["id"]) / "panes.jsonl").read_text().splitlines()[-1])
    assert row["orchestrator_pane_id"] == "w1:pA" and row["kind"] == "codex"


def _node() -> str | None:
    found = shutil.which("node", path=os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]))
    if found:
        return found
    nvm = sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "node")))
    return nvm[-1] if nvm else None


def test_herdr_ledger_sweep_sees_office_rows(env, monkeypatch, tmp_path):
    """The sweep only handles rows owned by the caller's pane; an Office row
    must be one of them."""
    node = _node()
    if not node:
        pytest.skip("node is not installed")
    run, d, res, rows = _herdr_worker(env, monkeypatch)
    fake = env.bin / "herdr"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\na = sys.argv[1:]\n"
                    "print(json.dumps({'result': {'agents': []} if a[:2] == ['agent', 'list'] else "
                    "{'panes': []} if a[:2] == ['pane', 'list'] else {}}))\n")
    fake.chmod(0o755)
    from office import paths
    e = dict(os.environ, HERDR_LEDGER=str(paths.run_dir(run["id"]) / "panes.jsonl"), HERDR_PANE_ID="w1:pQ",
             HERDR_NODE_BIN=node)
    proc = subprocess.run([str(ROOT / "skills" / "herdr" / "scripts" / "herdr-ledger.mjs"), "sweep", "--dry-run"],
                          capture_output=True, text=True, env=e, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert [c["pane"] for c in out["closed"]] == [res["pane"]], out


# ------------------------------------------------------------------ contract amendments


def _inline(env, plan=PLAN_TWO):
    env.trust()
    start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)


def _versions(env):
    con = env.con()
    try:
        plan = con.execute("SELECT plan_version FROM runs").fetchone()[0]
        tasks = {r["id"]: r["contract_version"] for r in con.execute("SELECT id, contract_version FROM tasks")}
        amendments = con.execute("SELECT COUNT(*) FROM amendments").fetchone()[0]
    finally:
        con.close()
    return plan, tasks, amendments


def test_contract_amendment_without_a_plan_edit_is_refused(env):
    _inline(env)
    before = _versions(env)
    code, out = env.office("amend", "T1", "--contract", "--", "T1 also owns the docs")
    assert code == 4 and "plan-not-edited" in out and "edit .office/PLAN.md" in out, out
    assert _versions(env) == before


def test_contract_amendment_that_edits_another_task_names_it(env):
    _inline(env)
    before = _versions(env)
    env.write_plan(PLAN_TWO.replace("scope: mul.py", "scope: mul.py, mul_test.py"))
    code, out = env.office("amend", "T1", "--contract", "--", "T1 also owns the docs")
    assert code == 4 and "contract-not-edited" in out and "T2" in out, out
    assert _versions(env) == before


def test_contract_amendment_with_the_task_edited_bumps_its_contract(env):
    _inline(env)
    plan, tasks, _ = _versions(env)
    env.write_plan(PLAN_TWO.replace("scope: calc.py", "scope: calc.py, calc_docs.md"))
    code, out = env.office("amend", "T1", "--contract", "--", "T1 also owns the docs")
    assert code == 0, out
    new_plan, new_tasks, _ = _versions(env)
    assert new_plan == plan + 1 and new_tasks["T1"] == new_plan and new_tasks["T2"] == tasks["T2"]


# ------------------------------------------------------------------ integration hint


def test_missing_command_on_the_composed_tree_says_to_install_dependencies(env):
    plan = PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: office-test-nonexistent-cmd\n")
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    finally:
        con.close()
    detail = landing["integration"]["detail"]
    assert "UNAVAILABLE" in detail and "no installed dependencies" in detail and "--frozen-lockfile" in detail, detail
    code, out = env.office("status")
    assert "no installed dependencies" in out, out


def test_submit_help_warns_checks_are_non_mutating_and_install_their_deps(env):
    code, out = env.office("submit", "--help")
    assert code == 0
    assert "non-mutating" in out and "pnpm install --frozen-lockfile" in out and "--contract" in out, out
