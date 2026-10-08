"""#399: Codex startup diagnostics and independent reply/last-message files."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from office import adapters, dispatch, doctor, gates, review_parse


@pytest.mark.parametrize("kind", ["reviewer", "vision"])
def test_codex_final_message_cannot_clobber_review(tmp_path, kind):
    # Reproduce Codex's exit write after the agent has written its own reply.
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\n" + '''import sys
from pathlib import Path
args = sys.argv[1:]
cwd = Path(args[args.index("--cd") + 1])
reply = cwd / "reply.txt"
reply.write_text("VERDICT: PASS\\n")
Path(args[args.index("-o") + 1]).write_text("[reply.txt](" + str(reply) + ")")
''')
    fake.chmod(0o755)
    adapter = adapters.load_all()["codex"]
    adapter["invocation"]["executable"] = str(fake)
    reply = tmp_path / "reply.txt"
    argv, _ = adapters.build_argv(adapter, kind, model="m", effort="high", cwd=tmp_path, output=reply)
    subprocess.run(argv, input="write your review", text=True, check=True)
    assert reply.read_text() == "VERDICT: PASS\n"
    assert (tmp_path / "last-message.txt").read_text().startswith("[reply.txt]")
    assert review_parse.parse(gates._reply_text({"launcher": "process-fallback"}, tmp_path, reply)).valid


def test_headless_reply_fallbacks_and_pane_isolation(tmp_path):
    reply = tmp_path / "reply.txt"
    last = tmp_path / "last-message.txt"
    last.write_text("VERDICT: PASS\n")
    (tmp_path / "output.log").write_text("unrelated terminal output")
    assert gates._reply_text({"launcher": "process"}, tmp_path, reply) == last.read_text()
    for launcher in ("herdr", "external"):
        assert gates._reply_text({"launcher": launcher}, tmp_path, reply) == ""
    reply.write_text("invalid review")
    assert gates._reply_text({"launcher": "process"}, tmp_path, reply) == "invalid review"
    reply.unlink()
    last.unlink()
    assert gates._reply_text({"launcher": "process"}, tmp_path, reply) == "unrelated terminal output"


@pytest.mark.parametrize("launcher", ["sync", "process", "process-fallback"])
def test_headless_invalid_diagnostic_never_promises_a_reprompt(tmp_path, monkeypatch, launcher):
    monkeypatch.setattr(gates, "_agent_alive", lambda name: pytest.fail("headless session probed"))
    _, _, reason = gates._reprompt_until_valid(None, {}, {"id": "D1", "launcher": launcher}, tmp_path,
                                              tmp_path / "reply.txt", review_parse.parse("bad"),
                                              plan_review=False, visual=False)
    assert "no re-prompt was possible" in reason and "headless" in reason
    assert "output.log" in reason and "last-message.txt" in reason
    assert "its pane is kept" not in reason and "office prompt" not in reason


@pytest.mark.parametrize("screen", ["Hooks need review", "Trust this folder?", "Update available"])
@pytest.mark.parametrize("timeout", [False, True])
def test_failed_codex_start_names_the_screen(tmp_path, monkeypatch, screen, timeout):
    calls, notices = [], []
    monkeypatch.setattr(dispatch, "write_agent_env", lambda *a, **kw: tmp_path / "agent.env")
    monkeypatch.setattr(dispatch, "_shell_run", lambda *a: True)
    monkeypatch.setattr(dispatch, "_launch_notice", lambda run, d, text: notices.append(text))
    monkeypatch.setattr(dispatch, "atomic_write_json", lambda *a, **kw: None)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["agent", "start"]:
            if timeout:
                raise subprocess.TimeoutExpired(argv, 120)
            return subprocess.CompletedProcess(argv, 1, "startup timeout", "")
        if argv[1:3] == ["pane", "close"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[1:3] == ["pane", "get"]:
            return subprocess.CompletedProcess(argv, 1, '{"error":{"code":"pane_not_found"}}', "")
        assert argv[1:3] == ["pane", "read"]
        return subprocess.CompletedProcess(argv, 0, screen, "")

    monkeypatch.setattr(dispatch.subprocess, "run", run)
    assert dispatch._herdr_agent_start({"id": "R1"}, {"id": "D1"}, {"kind": "reviewer"}, {},
                                       (["--sandbox", "workspace-write"], "codex"), "w1:p1",
                                       tmp_path, tmp_path) is None
    assert screen.rstrip("?") in notices[0] and "w1:p1" in notices[0]
    assert "headless" in notices[0]
    # No keys sent (hook trust and updates remain the user's decision; this cwd is not
    # Office's), and the abandoned pane is closed once its screen is saved.
    assert not any(c[1:3] == ["pane", "send-keys"] for c in calls), calls
    assert ["herdr", "pane", "close", "w1:p1"] in calls and "closed" in notices[0]


def test_unreadable_startup_screen_keeps_original_failure(monkeypatch):
    def fail(*a, **kw):
        raise OSError("unavailable")
    monkeypatch.setattr(dispatch.subprocess, "run", fail)
    notices = []
    monkeypatch.setattr(dispatch, "_launch_notice", lambda run, d, text: notices.append(text))
    monkeypatch.setattr(dispatch, "atomic_write_json", lambda *a, **kw: None)
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda run_id: Path("/nonexistent"))
    dispatch._herdr_fallback_notice({"id": "R1"}, {"id": "D1"}, {}, Path("/nonexistent"), "w1:p1",
                                    "herdr agent start failed", "startup timeout")
    assert "startup timeout" in notices[0] and "pane w1:p1 could not be read" in notices[0]


def test_hook_screen_never_gets_auto_accepted(monkeypatch):
    monkeypatch.setattr(dispatch, "_pane_view", lambda name: "Hooks need review\nWorking (1s • esc to interrupt)")
    assert not dispatch._landed_in(dispatch._pane_view("office-d1"), None)
    assert dispatch._await_agent_ui("office-d1", "w1:p1", 0, answer_trust=True,
                                    herdr=lambda *a: pytest.fail("hook screen auto-accepted")) == "trust"


def test_hook_screen_appearing_after_prompt_never_gets_enter(monkeypatch):
    monkeypatch.setattr(dispatch, "_await_agent_ui", lambda *a, **kw: "ready")
    monkeypatch.setattr(dispatch, "_pane_view", lambda name: "Hooks need review")
    monkeypatch.setattr(dispatch, "_prompt_landed", lambda *a, **kw: "trust")
    sent = []
    monkeypatch.setattr(dispatch, "_herdr_quiet", lambda *args: sent.append(args))
    assert not dispatch._deliver_prompt("office-d1", "w1:p1", "read brief", answer_trust=True)
    assert sent == [("agent", "prompt", "office-d1", "read brief")]


def test_doctor_warns_without_modifying_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": "echo user"}]}]}}))
    config = tmp_path / "config.toml"
    config.write_text('[hooks.state."old:session_start:1:0"]\ntrusted_hash = "old-hash"\n')
    before = config.read_bytes(), hooks.read_bytes()
    warnings = doctor.codex_hook_warnings()
    assert len(warnings) == 1 and "Hooks need review" in warnings[0]
    assert "UNVERIFIED" in warnings[0] and "reordered" in warnings[0]
    assert (config.read_bytes(), hooks.read_bytes()) == before
    config.write_text("[features]\nhooks = false\n")
    assert doctor.codex_hook_warnings() == []


def test_doctor_checks_project_and_enabled_plugin_hooks(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert doctor.codex_hook_warnings(tmp_path) == []
    project = tmp_path / ".codex" / "hooks.json"
    project.parent.mkdir()
    project.write_text('{"hooks": {"SessionStart": [{}]}}')
    plugin = home / "plugins/cache/market/example/1/hooks/hooks.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(project.read_text())
    cfg = home / "config.toml"
    cfg.write_text('[plugins."example@market"]\nenabled = false\n')
    assert str(plugin) not in " ".join(doctor.codex_hook_warnings(tmp_path))
    cfg.write_text('[plugins."example@market"]\nenabled = true\n')
    warning = " ".join(doctor.codex_hook_warnings(tmp_path))
    assert str(project) in warning and str(plugin) in warning
