"""#211: a visual block the capture step could never satisfy is refused at
plan submit, not after an executor has done the work."""
from __future__ import annotations

import socket

from conftest import PLAN_ONE


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _plan(visual: str) -> str:
    return PLAN_ONE.replace("visual: none\n", visual)


def test_unreachable_url_without_start_is_refused_at_submit(env):
    port = _closed_port()
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan(f"visual:\n  url: http://localhost:{port}/\n"))
    code, out = env.office("submit")
    assert code != 0, out
    assert "no `start:`" in out and "not reachable" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_remote_url_is_refused_and_start_makes_a_local_url_acceptable(env):
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan("visual:\n  url: https://example.com/\n"))
    code, out = env.office("submit")
    assert code != 0 and "not a local/test origin" in out, out
    port = _closed_port()
    env.write_plan(_plan(f"visual:\n  url: http://127.0.0.1:{port}/\n  start: python3 -m http.server {port}\n"))
    code, out = env.office("submit")
    assert code == 0, out


def test_missing_capture_backend_warns(monkeypatch):
    from office import visual
    monkeypatch.setattr(visual, "capture_backend_missing", lambda: "browser capture unavailable: install it")
    tasks = [{"id": "T1", "visual": {"url": "http://127.0.0.1:1/", "start": "serve"}},
             {"id": "T2", "visual": {"none": True}}]
    errors, warnings = visual.preflight(tasks)
    assert errors == []
    assert len(warnings) == 1 and "CAPTURE_BLOCKED" in warnings[0] and "install it" in warnings[0]
    assert visual.preflight([{"id": "T2", "visual": {"none": True}}]) == ([], [])
