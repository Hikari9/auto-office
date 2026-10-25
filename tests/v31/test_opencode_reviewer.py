"""The opencode reviewer: a pane-hosted (`mini`) and a headless (`run`) form that read the
checkout and write exactly their reply file.

opencode has no permission flag. Office writes a per-dispatch `office-reviewer` agent file
into the dispatch dir and points OPENCODE_CONFIG_DIR at it. A scripted fake `opencode`
applies the permission rules the way opencode 2.0.29 was probed to (last matching rule
wins; a path inside $PWD is checked as a cwd-relative resource; a path outside it needs
`external_directory` on `<dir>/*` first and then the action on the absolute path; `*`
crosses `/`). No test calls the real binary or a real model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import approved_run  # noqa: F401  (puts src on sys.path via the env fixture)
from test_herdr_agent_launch import _calls, _fake, _live_dispatch, launch_in_herdr

MODEL = "opencode/muse-spark-1.3-contributor-free"

FAKE_OPENCODE = f"""#!{sys.executable}
import json, os, re, sys
import yaml

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("opencode v2.0.29"); sys.exit(0)
if args[:1] == ["models"]:
    print("{MODEL}"); sys.exit(0)

def flag(name):
    return args[args.index(name) + 1] if name in args else None

def match(pattern, text):
    return re.fullmatch(re.escape(pattern).replace(r"\\*", ".*"), text) is not None

def allowed(rules, action, resource):
    last = None
    for r in rules:
        if match(r["action"], action) and match(r["resource"], resource):
            last = r
    return last is not None and last["effect"] == "allow"

def attempt(rules, directory, tool, path):
    action = {{"read": "read", "write": "edit"}}[tool]
    target = os.path.abspath(path)
    if target == directory or target.startswith(directory.rstrip("/") + "/"):
        resource = os.path.relpath(target, directory)
    else:
        if not allowed(rules, "external_directory", os.path.dirname(target) + "/*"):
            return "Permission denied: external_directory"
        resource = target
    if not allowed(rules, action, resource):
        return "Permission denied: " + action
    if tool == "write":
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "w").write("written by the fake opencode\\n")
    else:
        open(target).read()
    return "ok"

log = {{"argv": args, "pwd": os.environ.get("PWD"), "cwd": os.getcwd(),
        "config_dir": os.environ.get("OPENCODE_CONFIG_DIR"), "results": []}}
if args[:1] in (["run"], ["mini"]):
    config_dir, name = os.environ.get("OPENCODE_CONFIG_DIR"), flag("--agent")
    agent = os.path.join(config_dir or "", "agents", (name or "") + ".md")
    if not os.path.isfile(agent):
        print('Agent not found: "%s"' % name, file=sys.stderr); sys.exit(1)
    rules = yaml.safe_load(open(agent).read().split("---")[1])["permissions"]
    for tool, path in json.loads(os.environ.get("FAKE_OPENCODE_OPS", "[]")):
        log["results"].append([tool, path, attempt(rules, os.environ["PWD"], tool, path)])
if os.environ.get("FAKE_OPENCODE_LOG"):
    open(os.environ["FAKE_OPENCODE_LOG"], "w").write(json.dumps(log))
print("READY")
"""


@pytest.fixture
def opencode(env, monkeypatch):
    """A fake `opencode` first on PATH; its log lands in FAKE_OPENCODE_LOG."""
    exe = env.bin / "opencode"
    exe.write_text(FAKE_OPENCODE)
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(env.tmp / "opencode-log.json"))
    monkeypatch.setenv("OFFICE_PREFLIGHT", "off")
    from office import adapters
    adapters._RESOLVED_EXE.clear()
    return env


def _adapter():
    from office import adapters
    return adapters.load_all()["opencode"]


def _paths(env):
    """A checkout with one file, an include dir with one file, and a dispatch dir."""
    checkout = env.tmp / "checkout"
    include = env.tmp / "include"
    ddir = env.tmp / "runs" / "R1" / "dispatches" / "D1"
    for d in (checkout, include, ddir, ddir.parent / "D2"):
        d.mkdir(parents=True, exist_ok=True)
    (checkout / "src.py").write_text("print(1)\n")
    (include / "plan.md").write_text("plan\n")
    (checkout / ".env").write_text("SECRET=1\n")
    return checkout, include, ddir, ddir / "reply.txt"


def _stale_pwd(env) -> Path:
    """A directory holding none of the paths under test: a supervisor's PWD, not the checkout."""
    stale = env.tmp / "stale-pwd"
    stale.mkdir(exist_ok=True)
    return stale


def _run_fake(env, argv_tail, *, cwd, config_dir, ops, pwd=None):
    """Run the fake opencode the way a launch would: PWD is the process's directory unless a
    test sets it to something else (a supervisor's stale PWD)."""
    child = {**os.environ, "OPENCODE_CONFIG_DIR": str(config_dir), "PWD": str(pwd or cwd),
             "FAKE_OPENCODE_OPS": json.dumps([[t, str(p)] for t, p in ops])}
    proc = subprocess.run([str(env.bin / "opencode"), *argv_tail], cwd=str(cwd), env=child, capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr
    logged = json.loads(Path(os.environ["FAKE_OPENCODE_LOG"]).read_text())["results"]
    return {(t, Path(p)): r for t, p, r in logged}


def test_the_reviewer_profile_declares_the_pane_form_and_keeps_headless_run(opencode, tmp_path):
    from office import adapters
    adapter = _adapter()
    prof = adapters.profile(adapter, "reviewer")
    assert prof["herdr_kind"] == "opencode"
    inter = adapters.interactive_argv(adapter, "reviewer", model=MODEL, effort="high", cwd=tmp_path,
                                      output=tmp_path / "D1" / "reply.txt")
    assert inter == (["mini", "--standalone", "--agent", "office-reviewer", "--model", f"{MODEL}#high"], "opencode")
    headless, _ = adapters.build_argv(adapter, "reviewer", model=MODEL, effort="high", cwd=tmp_path,
                                      output=tmp_path / "D1" / "reply.txt")
    assert headless[1:] == ["run", "--standalone", "--agent", "office-reviewer", "--model", f"{MODEL}#high"]
    assert prof["prompt"] == "argv"
    for argv in (inter[0], headless):
        assert "--auto" not in argv
    # The worker form is untouched.
    assert adapters.profile(adapter, "worker")["interactive"]["argv"] == ["mini", "--model", "{model}#{effort}"]


def test_validate_adapter_passes_on_the_seed(opencode):
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, str(root / "scripts" / "office_runtime.py"), "validate-adapter",
                           str(root / "adapters" / "seed" / "opencode.yaml")], capture_output=True, text=True,
                          cwd=str(root))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_agent_file_names_the_reply_file_and_nothing_else_to_write(opencode):
    from office import read_scope
    checkout, include, ddir, out = _paths(opencode)
    rules = read_scope.opencode_permissions(checkout, out, [include])
    assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}
    edits = [r for r in rules if r["action"] == "edit"]
    assert edits and all(r["effect"] == "allow" for r in edits)
    assert {r["resource"] for r in edits} <= {str(out), str(out.resolve())}  # the exact file, no wildcard
    allowed = {r["action"] for r in rules if r["effect"] == "allow"}
    assert allowed == {"read", "glob", "grep", "external_directory", "edit"}
    assert not any(r["effect"] == "allow" and r["resource"] == "*" and r["action"] not in ("read", "glob", "grep")
                   for r in rules)
    external = {r["resource"] for r in rules if r["action"] == "external_directory"}
    for d in (checkout, include, ddir):
        assert f"{d}/*" in external
    text = read_scope.opencode_agent_text(checkout, out, [include])
    assert yaml.safe_load(text.split("---")[1])["permissions"] == rules


def test_reviewer_env_writes_the_agent_into_the_dispatch_dir(opencode):
    from office import adapters
    checkout, include, ddir, out = _paths(opencode)
    got = adapters.reviewer_env(_adapter(), "reviewer", cwd=checkout, output=out, include_dirs=[include])
    assert got == {"OPENCODE_CONFIG_DIR": str(ddir / "opencode")}
    assert (ddir / "opencode" / "agents" / "office-reviewer.md").is_file()
    # A worker, or a harness with no agent_file, gets nothing.
    assert adapters.reviewer_env(_adapter(), "worker", cwd=checkout, output=out) == {}
    assert adapters.reviewer_env(adapters.load_all()["claude"], "reviewer", cwd=checkout, output=out) == {}


def test_the_launch_refuses_with_a_reason_when_the_agent_file_cannot_be_written(opencode):
    from office import adapters
    checkout, _, ddir, out = _paths(opencode)
    (ddir / "opencode").write_text("a file where the config dir must go")
    with pytest.raises(adapters.AdapterError, match="agent file could not be written"):
        adapters.reviewer_env(_adapter(), "reviewer", cwd=checkout, output=out)


@pytest.mark.parametrize("pwd_is_checkout", [True, False], ids=["pwd-checkout", "pwd-elsewhere"])
def test_the_reviewer_reads_the_checkout_and_writes_only_its_reply(opencode, pwd_is_checkout):
    from office import adapters
    checkout, include, ddir, out = _paths(opencode)
    config = adapters.reviewer_env(_adapter(), "reviewer", cwd=checkout, output=out, include_dirs=[include])
    sibling, other_run = ddir / "notes.txt", ddir.parent / "D2" / "reply.txt"
    ops = [("read", checkout / "src.py"), ("read", include / "plan.md"), ("read", checkout / ".env"),
           ("write", out), ("write", sibling), ("write", other_run), ("write", checkout / "src.py"),
           ("write", include / "plan.md")]
    pwd = None if pwd_is_checkout else _stale_pwd(opencode)
    got = _run_fake(opencode, ["run", "--standalone", "--agent", "office-reviewer", "--model", "m"], cwd=checkout,
                    config_dir=config["OPENCODE_CONFIG_DIR"], ops=ops, pwd=pwd)
    results = got
    assert results[("read", checkout / "src.py")] == "ok"
    assert results[("read", include / "plan.md")] == "ok"
    assert results[("read", checkout / ".env")] == "Permission denied: read"
    assert results[("write", out)] == "ok" and out.is_file()
    for denied in (sibling, other_run, checkout / "src.py", include / "plan.md"):
        assert results[("write", denied)].startswith("Permission denied"), denied
    assert not sibling.exists() and not other_run.exists()


def test_without_the_external_directory_grant_the_checkout_is_denied(opencode):
    """The control: the fake enforces `external_directory`, so the grant is what lets the read through."""
    from office import read_scope
    checkout, _, ddir, out = _paths(opencode)
    config = ddir / "opencode"
    agent = config / "agents"
    agent.mkdir(parents=True)
    kept = [r for r in read_scope.opencode_permissions(checkout, out) if r["action"] != "external_directory"]
    body = "\n".join(f"  - {{action: {json.dumps(r['action'])}, resource: {json.dumps(r['resource'])}, "
                     f"effect: {r['effect']}}}" for r in kept)
    (agent / "office-reviewer.md").write_text(f"---\npermissions:\n{body}\n---\nx\n")
    got = _run_fake(opencode, ["run", "--agent", "office-reviewer"], cwd=checkout, config_dir=config,
                    ops=[("read", checkout / "src.py")], pwd=_stale_pwd(opencode))
    assert list(got.values()) == ["Permission denied: external_directory"]


def test_a_headless_process_runs_in_its_checkout_by_pwd(opencode):
    """opencode takes its working directory from $PWD; Popen's cwd alone leaves the supervisor's."""
    from office import dispatch
    checkout, _, _, _ = _paths(opencode)
    log = opencode.tmp / "pwd.txt"
    child = dispatch._spawn_agent(
        [sys.executable, "-c", f"import os; open({str(log)!r}, 'w').write(os.environ['PWD'])"], str(checkout),
        {**os.environ, "PWD": "/somewhere/stale"}, subprocess.DEVNULL)
    child.communicate()
    assert log.read_text() == str(checkout)


@pytest.mark.approved
def test_a_pane_hosted_opencode_reviewer_is_launched_labelled_and_fenced(opencode, monkeypatch):
    from office import dispatch
    include = opencode.tmp / "include"
    include.mkdir()
    (include / "plan.md").write_text("plan\n")
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", role="reviewer", output=True,
                                                    brief="You are a code reviewer\n", include_dirs=[include])
    assert res["launcher"] == "herdr", res
    calls = _calls(state_file)
    start = [c for c in calls if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--kind") + 1] == "opencode"
    assert start[start.index("--") + 1:] == ["mini", "--standalone", "--agent", "office-reviewer", "--model",
                                              f"{MODEL}#high"]
    # Named like the other reviewer panes: by pane_label for this dispatch.
    label = dispatch.pane_label(run, {**d, "role": d["role"]}, "reviewer")
    assert label and any(label in c for c in calls), (label, calls)
    # The pane's shell sources agent.env, which points opencode at this dispatch's agent file.
    env_text = (ddir / "agent.env").read_text()
    assert f"export OPENCODE_CONFIG_DIR={ddir / 'opencode'}" in env_text
    assert "OFFICE_DISPATCH_ID" not in env_text and "OFFICE_ROLE=reviewer" in env_text
    # Starting the agent as the pane would: the checkout and include dir read, only the reply file written.
    out = ddir / "reply.txt"
    ops = [("read", opencode.repo / "README.md"), ("read", include / "plan.md"), ("write", out),
           ("write", ddir / "brief.md"), ("write", opencode.repo / "README.md")]
    (opencode.repo / "README.md").write_text("readme\n")
    got = _run_fake(opencode, start[start.index("--") + 1:], cwd=opencode.repo,
                    config_dir=ddir / "opencode", ops=ops, pwd=_stale_pwd(opencode))
    results = got
    assert results[("read", opencode.repo / "README.md")] == "ok"
    assert results[("read", include / "plan.md")] == "ok"
    assert results[("write", out)] == "ok"
    assert results[("write", ddir / "brief.md")].startswith("Permission denied")
    assert results[("write", opencode.repo / "README.md")].startswith("Permission denied")
    prompt = [c for c in calls if c[:2] == ["agent", "prompt"]][-1]
    assert str(out) in prompt[3] and "office submit" not in prompt[3]


@pytest.mark.approved
def test_an_opencode_pane_reviewer_result_is_read_only_from_the_reply_file(opencode, monkeypatch):
    # The pane shows a verdict but the reply file was never written: nothing is scraped from the pane.
    state_file = _fake(opencode, monkeypatch, gets=["done", "done", "done"])
    data = json.loads(state_file.read_text())
    data["content"] = "VERDICT: APPROVED"
    state_file.write_text(json.dumps(data))
    run, d = _live_dispatch(opencode, monkeypatch)
    from office import dispatch
    out = opencode.tmp / "reply.txt"
    spec = {"herdr_agent": "office-D", "output": str(out), "kind": "reviewer"}
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")
    assert not out.exists()
    assert dispatch.transcript_reply({**d, "harness": "opencode"}, spec) is None


@pytest.mark.approved
def test_the_headless_reviewer_form_runs_when_herdr_is_unavailable(opencode, monkeypatch):
    """No Herdr: the headless `run` form is the dispatch of record, with the same per-dispatch agent."""
    from office import db, dispatch, paths
    run, d = _live_dispatch(opencode, monkeypatch)
    for key in ("HERDR_ENV", "HERDR_PANE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OFFICE_LAUNCHER", "sync")
    con = opencode.con()
    try:
        with db.transaction(con):
            con.execute("UPDATE dispatches SET adapter_id='opencode', model=?, effort='high' WHERE id=?",
                        (MODEL, d["id"]))
    finally:
        con.close()
    d = {**d, "adapter_id": "opencode", "model": MODEL, "effort": "high"}
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("You are a code reviewer\n")
    out = ddir / "reply.txt"
    (opencode.repo / "README.md").write_text("readme\n")
    monkeypatch.setenv("FAKE_OPENCODE_OPS", json.dumps([["read", str(opencode.repo / "README.md")],
                                                         ["write", str(out)],
                                                         ["write", str(opencode.repo / "README.md")]]))
    dispatch.launch(run, d, "reviewer", ddir, cwd=opencode.repo, output=out, include_dirs=[opencode.tmp],
                    wait=True)
    log = json.loads(Path(os.environ["FAKE_OPENCODE_LOG"]).read_text())
    assert log["argv"][:5] == ["run", "--standalone", "--agent", "office-reviewer", "--model"], log["argv"]
    assert log["argv"][5] == f"{MODEL}#high" and "--auto" not in log["argv"]
    assert log["config_dir"] == str(ddir / "opencode") and log["pwd"] == str(opencode.repo)
    results = {(t, Path(p)): r for t, p, r in log["results"]}
    assert results[("read", opencode.repo / "README.md")] == "ok"
    assert results[("write", out)] == "ok" and out.is_file()
    assert results[("write", opencode.repo / "README.md")].startswith("Permission denied")


@pytest.mark.approved
def test_a_pane_reviewer_never_starts_when_its_agent_file_cannot_be_written(opencode, monkeypatch):
    """No agent file means no write fence, and an unknown `--agent` may fall back to a default
    agent that can write anywhere: the pane launch refuses and never starts opencode."""
    from office import adapters
    from test_herdr_agent_launch import _launch_events
    real = adapters.reviewer_env

    def blocked(adapter, kind, *, cwd, output, include_dirs=()):
        (Path(output).parent / "opencode").write_text("a file where the config dir must go")
        return real(adapter, kind, cwd=cwd, output=output, include_dirs=include_dirs)

    monkeypatch.setattr(adapters, "reviewer_env", blocked)
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", role="reviewer", output=True,
                                                    brief="You are a code reviewer\n")
    calls = _calls(state_file)
    assert not any(c[:2] == ["agent", "start"] for c in calls), calls
    assert res["launcher"] == "process-fallback", res
    assert any("per-dispatch permissions could not be written" in e and "agent file could not be written" in e
               for e in _launch_events(opencode, run))
    assert "OPENCODE_CONFIG_DIR" not in (ddir / "agent.env").read_text()
