"""User model overrides (#185): dispatch --as/--cli/--external and --review-as."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, start_inline

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _go(env, plan=PLAN_ONE, gear="direct+review", **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear=gear)
    env.office("approve", "plan", "--quote", "approved", check=0)


def _dispatch_row(env, tid="T1"):
    con = env.con()
    did = con.execute("SELECT current_dispatch_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


def test_as_dispatches_an_unrouted_model_and_records_the_override(env):
    _go(env)
    # Not in the registry at all: routing alone would refuse it.
    code, out = env.office("dispatch", "T1", "--as", "claude/some-future-model@low", env=EXTERNAL)
    assert code == 0 and "(user override)" in out, out
    d = _dispatch_row(env)
    assert d["triple"].endswith("/some-future-model@low") and d["harness"] == "claude"
    assert json.loads(d["override_json"]) == {"by": "user", "declared": True, "triple": d["triple"]}
    code, out = env.office("inspect", "task", "T1")
    assert "user override" in out, out


def test_as_resolves_the_catalog_invocation_slug(env):
    # agy takes the combined native-Gemini slug; the user types the model and effort.
    _go(env)
    env.office("dispatch", "T1", "--as", "agy/gemini-3.8-flash@medium", env=EXTERNAL, check=0)
    d = _dispatch_row(env)
    assert d["model"] == "gemini-3.8-flash-medium" and d["effort"] == "medium"


def test_dispatch_prints_paths_and_a_valid_herdr_command(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", env=EXTERNAL)
    d = _dispatch_row(env)
    for key in ("brief:", "env:", "worktree:", "herdr agent start", "herdr agent prompt"):
        assert key in out, (key, out)
    name = re.search(r"herdr agent start (\S+)", out).group(1)
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) and d["id"].lower() in name
    assert "--model claude-sonnet-5-5" in out and d["worktree"] in out


def test_external_launches_nothing_and_says_how(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--external")
    assert code == 0, out
    d = _dispatch_row(env)
    assert d["launcher"] == "external" and not d["pid"]
    assert (Path(d["worktree"]).exists())
    con = env.con()
    notes = [r[0] for r in con.execute("SELECT summary FROM events WHERE kind='launch'")]
    assert any("external executor" in n and "herdr agent start" in n for n in notes), notes
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    from office import paths
    env_text = (paths.run_dir(run_id) / "dispatches" / d["id"] / "agent.env").read_text()
    assert f"OFFICE_DISPATCH_ID={d['id']}" in env_text


def test_flag_combinations_are_validated(env):
    _go(env)
    for args, why in ((["--cli", "claude --model x"], "--cli needs --as"),
                      (["--as", "claude/x", "--cli", "claude", "--external"], "mutually exclusive"),
                      (["--review-external"], "need --review-as"),
                      (["--as", "claude/x", "--route", "claude/y"], "--as or --route"),
                      (["--as", "gemini-3.8-flash"], "expected <harness>/<model>")):
        code, out = env.office("dispatch", "T1", *args)
        assert code != 0 and why in out, (args, out)


def test_cli_starts_that_exact_argv_in_herdr(env, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_herdr_agent_launch import BUSY, _calls, _fake
    state_file = _fake(env, monkeypatch, reads=[BUSY])
    _go(env)
    env.office("dispatch", "T1", "--as", "agy/gemini-3.8-flash@medium", "--external", check=0)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:pQ")
    monkeypatch.setenv("OFFICE_LAUNCHER", "herdr")
    monkeypatch.setenv("OFFICE_HERDR_LAND_TIMEOUT", "0")
    from office import dispatch, paths, state
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    d = state.get_dispatch(con, _dispatch_row(env)["id"])
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    (ddir / "brief.md").write_text("ROLE executor\n")
    res = dispatch.launch(run, d, "worker", ddir, cwd=env.repo, cli="agy --model gemini-3.8-flash-medium --yolo")
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    start = next(c for c in _calls(state_file) if c[:2] == ["agent", "start"])
    assert start[start.index("--kind") + 1] == "agy"
    assert start[start.index("--") + 1:] == ["--model", "gemini-3.8-flash-medium", "--yolo"]


def test_cli_without_herdr_is_left_external_not_run_headless(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--cli", "claude --model x")
    assert code == 0, out
    assert _dispatch_row(env)["launcher"] == "external"
    con = env.con()
    assert any("--cli needs a herdr session" in r[0] for r in con.execute("SELECT summary FROM events WHERE kind='launch'"))
    assert not [c for c in env.calls() if c.get("role") == "executor"]


def test_stacked_task_keeps_the_override_and_launch_form(env):
    _go(env, plan=PLAN_TWO)
    code, out = env.office("dispatch", "T1", "T2", "--as", "claude/claude-sonnet-5-5@high", "--external")
    assert "T2 stacked after T1" in out, out
    con = env.con()
    stash = json.loads(con.execute("SELECT route_json FROM tasks WHERE id='T2'").fetchone()[0])
    assert stash["override"] is True and stash["launch"] == {"external": True}
    assert stash["candidate"]["invocation_model_id"] == "claude-sonnet-5-5"


def test_relaunch_keeps_the_override(env):
    _go(env)
    env.office("dispatch", "T1", "--as", "claude/some-future-model@low", env=EXTERNAL, check=0)
    first = _dispatch_row(env)
    from office import db, dispatch, state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    with db.transaction(con):
        con.execute("UPDATE dispatches SET status='exited' WHERE id=?", (first["id"],))
        con.execute("UPDATE leases SET released_at='x' WHERE id=?", (first["lease_id"],))
        did = dispatch.request_launch(con, run, "T1", role="executor")
    again = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())
    assert again["triple"] == first["triple"] and json.loads(again["override_json"])["declared"] is True


def test_review_as_same_family_as_the_executor_is_refused(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high",
                           "--review-as", "claude/claude-opus-5-5@low", env=EXTERNAL)
    assert code != 0 and "review-not-independent" in out and "claude" in out, out
    assert _dispatch_row_or_none(env) is None


def _dispatch_row_or_none(env):
    con = env.con()
    return con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0]


def test_review_as_is_pinned_on_the_task(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "agy/gemini-3.8-flash@medium",
                           "--review-as", "codex/gpt-6-luna@xhigh", "--review-external", env=EXTERNAL)
    assert code == 0, out
    con = env.con()
    pinned = json.loads(con.execute("SELECT review_override_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert pinned == {"as": "codex/gpt-6-luna@xhigh", "cli": None, "external": True, "by": "user"}
    code, out = env.office("inspect", "task", "T1")
    assert "review pinned by user: codex/gpt-6-luna@xhigh (external)" in out, out


def test_pinned_reviewer_rechecks_independence_against_the_real_producer(env):
    # The executor was relaunched on a different route after --review-as: the
    # gate compares against whoever actually produced the revision.
    _go(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--review-as", "codex/gpt-6-luna@xhigh",
               check=0)
    con = env.con()
    reviewed = [c for c in env.calls() if c.get("role") == "code_reviewer"]
    assert reviewed and reviewed[0]["harness"] == "codex", env.calls()
    # Now pin a same-family reviewer after the fact and re-run the gate.
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": "claude/claude-opus-5-5@low", "by": "user"}),))
    con.commit()
    from office import gates, state
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    gate = dict(con.execute("SELECT * FROM gates WHERE task_id='T1' AND kind='code_review' ORDER BY created_at DESC")
                .fetchone())
    out = gates.run_reviewer(con, run, gate, "code_reviewer", "brief", cwd=env.repo)
    assert out["verdict"] == "UNAVAILABLE" and "same model family (claude)" in out["summary"], out


def test_routed_review_excludes_a_declared_producers_family(env):
    env.trust()
    from office import candidates, config, state
    con = env.con()
    cfg = config.load_yaml(config.default_config_path())
    run = {"id": "r", "gear": "", "playbook": "Change", "risk": {}}
    got = candidates.route_role(con, cfg, run, "code_reviewer", exclude={"family:gpt"}, probe=False)
    chosen = got.get("candidate") or {}
    assert candidates.model_family(chosen.get("model_id")) != "gpt"
    assert all(candidates.model_family(c["model_id"]) != "gpt" for c in got["request"]["candidates"])


def test_external_reviewer_ends_when_its_review_file_is_written(env, tmp_path):
    from office import db, dispatch, state
    _go(env)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    d = _dispatch_row(env)
    out = tmp_path / "reply.txt"
    out.write_text("VERDICT: PASS\n")
    assert dispatch.watch_external_output(d["id"], {"output": str(out)}, poll=0) == (0, "success")
    missing = tmp_path / "none.txt"
    con = env.con()
    with db.transaction(con):
        con.execute("UPDATE dispatches SET status='cancelled' WHERE id=?", (d["id"],))
    assert dispatch.watch_external_output(d["id"], {"output": str(missing)}, poll=0) == (None, "cancelled")


def test_model_family():
    from office.candidates import model_family
    assert model_family("claude-sonnet-5-5") == model_family("opus") == "claude"
    assert model_family("gpt-6-luna") == model_family("luna") == "gpt"
    assert model_family("gemini-3.8-flash") == "gemini"
    assert model_family(None) is None


def test_external_dispatch_says_nothing_was_launched(env):
    _go(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--external")
    assert code == 0 and "nothing launched" in out and " launching" not in out, out
