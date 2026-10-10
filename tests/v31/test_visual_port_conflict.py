"""#507: avoid a second server on an occupied port and name its owner."""
from pathlib import Path
from types import SimpleNamespace

from office import visual


def test_port_conflict_names_pid_command_and_possible_finished_dispatch(monkeypatch, tmp_path):
    class Conn:
        def execute(self, sql, params):
            assert "ended_at IS NOT NULL" in sql
            assert params == ("R1", str(tmp_path))
            return self
        def fetchone(self):
            return (1,)

    class OpenSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(visual.socket, "create_connection", lambda *args, **kwargs: OpenSocket())
    def fake_run(args, **kwargs):
        if args[0] == "ps":
            return SimpleNamespace(returncode=0, stdout="/usr/local/bin/node\n")
        if "-iTCP:8002" in args:
            return SimpleNamespace(returncode=0, stdout="p4567\n")
        return SimpleNamespace(returncode=0, stdout=f"p4567\nn{tmp_path}\n")
    monkeypatch.setattr(visual.subprocess, "run", fake_run)
    result = visual._capture_port_conflict("http://127.0.0.1:8002/", Conn(), {"id": "R1"}, tmp_path)
    assert "port 8002" in result and "PID 4567" in result and "(node)" in result
    assert "ended Office executor" in result and "kill 4567" in result


def test_visual_capture_blocks_before_spawning_second_server(monkeypatch, tmp_path):
    monkeypatch.setattr(visual.paths, "run_dir", lambda *_: tmp_path)
    monkeypatch.setattr(visual, "_capture_port_conflict", lambda *args: "port 8002 occupied by PID 123")
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: None)
    monkeypatch.setattr(visual, "LOCAL_ORIGIN", visual.LOCAL_ORIGIN)
    monkeypatch.setattr(visual.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not start second server")))
    from office import submit
    monkeypatch.setattr(submit, "matches_revision", lambda *args: True)
    run = {"id": "R1", "repo_root": str(tmp_path)}
    task = {"id": "T1", "visual": {"url": "http://127.0.0.1:8002/", "start": "vite --port 8002 --strictPort"}}
    result = visual.capture_all(None, run, task, {"id": "V1", "commit_sha": "abc"}, {"id": "G1", "recaptures": 0}, tmp_path)
    assert result["evidence_status"] == "CAPTURE_BLOCKED"
    assert "PID 123" in result["cause"]
