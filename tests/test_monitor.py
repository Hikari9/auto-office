#!/usr/bin/env python3
"""Tests for scripts/office_monitor.py and the close_finished_panes.mjs gate it feeds.

Deterministic harness fakes only — a fake `herdr` binary this test suite
controls, never the real Herdr server. No test in this file touches a real
Herdr pane.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("office_monitor", ROOT / "scripts/office_monitor.py")
mon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mon)


def _resolve_node() -> str | None:
    """Real node binary. `node` is an nvm lazy-load shell function in this
    environment, not a binary on PATH in a non-interactive subprocess."""
    nvm = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"))
    if nvm:
        return str(nvm[-1])
    return shutil.which("node")


NODE_BIN = _resolve_node()

FAKE_HERDR_SOURCE = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    import json, os, sys

    STATE_PATH = os.environ["FAKE_HERDR_STATE"]
    STATE = json.loads(open(STATE_PATH, encoding="utf-8").read())
    args = sys.argv[1:]

    def emit(obj, code=0):
        print(json.dumps(obj))
        sys.exit(code)

    def next_index(key):
        idx_path = STATE_PATH + f".{key}.idx"
        idx = 0
        if os.path.exists(idx_path):
            idx = int(open(idx_path, encoding="utf-8").read().strip() or "0")
        open(idx_path, "w", encoding="utf-8").write(str(idx + 1))
        return idx

    if args[:2] == ["agent", "list"]:
        emit({"result": {"agents": STATE.get("agent_list", []), "type": "agent_list"}})
    if args[:2] == ["pane", "list"]:
        emit({"result": {"panes": STATE.get("pane_list", []), "type": "pane_list"}})
    if args[:2] == ["agent", "get"] and len(args) >= 3:
        target = args[2]
        responses = STATE.get("agent_get", {}).get(target)
        if not responses:
            emit({"error": {"code": "agent_not_found", "message": "not found"}}, code=1)
        idx = next_index(f"get.{target}")
        emit({"result": {"agent": responses[min(idx, len(responses) - 1)]}})
    if args[:2] == ["agent", "read"] and len(args) >= 3:
        target = args[2]
        frames = STATE.get("agent_read", {}).get(target)
        if frames is None:
            sys.exit(0)
        idx = next_index(f"read.{target}")
        sys.stdout.write(frames[min(idx, len(frames) - 1)])
        sys.exit(0)
    if args[:2] == ["pane", "close"] and len(args) >= 3:
        pane = args[2]
        if pane in STATE.get("pane_close_fail", []):
            emit({"error": {"code": "close_failed", "message": "boom"}}, code=1)
        emit({"result": {"pane_id": pane, "closed": True}})
    emit({"error": {"code": "unsupported", "message": f"fake herdr: unsupported args {args}"}}, code=1)
    """
).lstrip()


class FakeHerdr:
    """A fake `herdr` executable on its own PATH-prepended directory.

    `write_state` rewrites the canned-response document the fake reads on
    every invocation; per-target call indices persist on disk so repeated
    `agent get`/`agent read` calls can return successive frames (used to
    simulate advancing pane content, i.e. still-working output).
    """

    def __init__(self, tmp: Path):
        self.bin_dir = tmp / "fakebin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.herdr_path = self.bin_dir / "herdr"
        self.herdr_path.write_text(FAKE_HERDR_SOURCE, encoding="utf-8")
        self.herdr_path.chmod(self.herdr_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.state_path = tmp / "fake_herdr_state.json"
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def env(self, base: dict | None = None) -> dict:
        base = dict(base or os.environ)
        base["PATH"] = f"{self.bin_dir}{os.pathsep}{base.get('PATH', '')}"
        base["FAKE_HERDR_STATE"] = str(self.state_path)
        return base


class MonitorFunctionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-monitor-test-"))
        self.state_dir = self.tmp / ".office"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, dispatch_id, sequence, session_id="sess-1", family_id="fam-1", **overrides):
        payload = dict(
            state_dir=self.state_dir,
            event_id=mon.derive_event_id(dispatch_id, sequence),
            session_id=session_id,
            family_id=family_id,
            dispatch_id=dispatch_id,
            sequence=sequence,
            observed_status="finish",
            terminal_classification="success",
            source="process_exit",
            evidence_timestamp="2026-09-15T00:00:00Z",
            evidence_hash=mon._sha256_obj({"n": sequence}),
        )
        payload.update(overrides)
        return mon.record_completion_event(**payload)

    # ---- event identity, dedup, replay ----

    def test_event_id_must_match_derivation(self):
        with self.assertRaises(mon.MonitorArgumentError):
            mon.record_completion_event(
                state_dir=self.state_dir,
                event_id="evt-not-the-real-one",
                session_id="s", family_id="f", dispatch_id="d", sequence=1,
                observed_status="finish", terminal_classification="success",
                source="process_exit", evidence_timestamp="2026-09-15T00:00:00Z",
                evidence_hash=mon._sha256_obj({}),
            )

    def test_duplicate_sequence_is_a_noop_not_a_second_row(self):
        first = self._record("disp-1", 1)
        second = self._record("disp-1", 1)  # exact same (dispatch_id, sequence)
        self.assertEqual(first, second)
        rows = mon._read_jsonl(mon._events_path(self.state_dir))
        self.assertEqual(len(rows), 1, "duplicate suppression: only one row for (dispatch_id, sequence)")

    def test_reconnect_restart_replay_does_not_refire_or_replay_old_events(self):
        self._record("disp-1", 1, observed_status="running", terminal_classification="non_terminal")
        self._record("disp-1", 2, observed_status="finish", terminal_classification="success")
        mon.acknowledge_events(
            self.state_dir, "sess-1", "fam-1", "disp-1", 1,
            mon.derive_event_id("disp-1", 1),
        )
        mon.acknowledge_events(
            self.state_dir, "sess-1", "fam-1", "disp-1", 2,
            mon.derive_event_id("disp-1", 2),
        )

        # "restart": nothing but the module-level (pure) functions and the
        # files on disk — no in-memory state survives.
        cursor_after_restart = mon.get_event_cursor(self.state_dir, "sess-1", "fam-1", "disp-1")
        self.assertEqual(cursor_after_restart, 2)

        # A consumer resuming from the persisted cursor never replays
        # already-acked events...
        unseen = mon.list_events(self.state_dir, dispatch_id="disp-1", since_seq=cursor_after_restart)
        self.assertEqual(unseen, [])

        # ...and re-observing the same already-terminal dispatch after
        # "restart" does not re-fire a new terminal event: it short-circuits
        # to the existing trusted terminal event without even needing to
        # re-read pid/exit_code.
        again = mon.emit_process_exit_event(self.state_dir, "sess-1", "fam-1", "disp-1")
        existing = mon.latest_trusted_terminal_event(self.state_dir, "disp-1")
        self.assertEqual(again["event_id"], existing["event_id"])
        self.assertEqual(existing["sequence"], 2)
        all_events = mon.list_events(self.state_dir, dispatch_id="disp-1")
        self.assertEqual(len(all_events), 2, "restart replay must not append a duplicate terminal event")

    # ---- ack-event contract (§5.5) ----

    def test_ack_event_in_order_then_idempotent_duplicate(self):
        self._record("disp-1", 1, observed_status="running", terminal_classification="non_terminal")
        event_id = mon.derive_event_id("disp-1", 1)
        r1 = mon.acknowledge_events(self.state_dir, "sess-1", "fam-1", "disp-1", 1, event_id)
        self.assertEqual(r1["status"], "acknowledged")
        r2 = mon.acknowledge_events(self.state_dir, "sess-1", "fam-1", "disp-1", 1, event_id)
        self.assertEqual(r2["status"], "already_acknowledged")
        self.assertEqual(r1["acknowledgement_hash"], r2["acknowledgement_hash"])

    def test_ack_event_sequence_gap_rejected(self):
        self._record("disp-1", 1, observed_status="running", terminal_classification="non_terminal")
        self._record("disp-1", 2, observed_status="finish", terminal_classification="success")
        with self.assertRaises(mon.MonitorSequenceError):
            mon.acknowledge_events(self.state_dir, "sess-1", "fam-1", "disp-1", 2, mon.derive_event_id("disp-1", 2))

    def test_ack_event_id_mismatch_rejected(self):
        self._record("disp-1", 1, observed_status="running", terminal_classification="non_terminal")
        with self.assertRaises(mon.MonitorArgumentError):
            mon.acknowledge_events(self.state_dir, "sess-1", "fam-1", "disp-1", 1, "evt-wrong")

    # ---- concurrent family isolation ----

    def test_concurrent_family_isolation(self):
        self._record("disp-A", 1, session_id="sess-1", family_id="fam-A")
        self._record("disp-B", 1, session_id="sess-1", family_id="fam-B")
        mon.acknowledge_events(self.state_dir, "sess-1", "fam-A", "disp-A", 1, mon.derive_event_id("disp-A", 1))

        self.assertEqual(mon.get_event_cursor(self.state_dir, "sess-1", "fam-A", "disp-A"), 1)
        self.assertEqual(
            mon.get_event_cursor(self.state_dir, "sess-1", "fam-B", "disp-B"), 0,
            "acking family A's dispatch must not advance family B's cursor",
        )
        self.assertEqual(len(mon.list_events(self.state_dir, dispatch_id="disp-A")), 1)
        self.assertEqual(len(mon.list_events(self.state_dir, dispatch_id="disp-B")), 1)

    # ---- monitor health: loss must be visible ----

    def test_monitor_loss_is_visible_not_silent(self):
        mon.record_monitor_health(
            self.state_dir, monitor_id="mon-1", session_id="sess-1", sequence=1, status="healthy",
            active_panes=["w1:p1"], event_lag_ms=10,
            health_evidence={"checks_passed": True, "evidence_hash": mon._sha256_obj({"n": 1})},
        )
        self.assertFalse(mon.monitor_is_stale(self.state_dir, "mon-1", max_age_seconds=3600))

        stale_ts = "2020-01-01T00:00:00Z"
        mon.record_monitor_health(
            self.state_dir, monitor_id="mon-1", session_id="sess-1", sequence=2, status="healthy",
            active_panes=["w1:p1"], event_lag_ms=10,
            health_evidence={"checks_passed": True, "evidence_hash": mon._sha256_obj({"n": 2})},
            timestamp=stale_ts,
        )
        self.assertTrue(
            mon.monitor_is_stale(self.state_dir, "mon-1", max_age_seconds=60),
            "a heartbeat that stopped advancing must read as stale, not healthy-by-default",
        )
        self.assertTrue(
            mon.monitor_is_stale(self.state_dir, "mon-unknown", max_age_seconds=3600),
            "no heartbeat at all must read as stale (monitor never started / already gone)",
        )

    # ---- process_exit source: native, reliable, no corroboration window ----

    def test_process_exit_event_requires_both_pid_death_and_exit_code(self):
        dispatch_dir = self.state_dir / "dispatches" / "disp-1"
        dispatch_dir.mkdir(parents=True)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        (dispatch_dir / "pid").write_text(str(proc.pid))
        try:
            self.assertIsNone(
                mon.emit_process_exit_event(self.state_dir, "sess-1", "fam-1", "disp-1"),
                "process still alive: must not emit a terminal event",
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        (dispatch_dir / "exit_code").write_text("0")
        event = mon.emit_process_exit_event(self.state_dir, "sess-1", "fam-1", "disp-1")
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "process_exit")
        self.assertEqual(event["terminal_classification"], "success")

        again = mon.emit_process_exit_event(self.state_dir, "sess-1", "fam-1", "disp-1")
        self.assertEqual(again["event_id"], event["event_id"], "idempotent: restart must not re-fire")

    def test_process_exit_nonzero_code_is_failure_not_success(self):
        dispatch_dir = self.state_dir / "dispatches" / "disp-2"
        dispatch_dir.mkdir(parents=True)
        (dispatch_dir / "pid").write_text("999999")  # not alive
        (dispatch_dir / "exit_code").write_text("1")
        event = mon.emit_process_exit_event(self.state_dir, "sess-1", "fam-1", "disp-2")
        self.assertEqual(event["terminal_classification"], "failure")

    # ---- the Herdr bridge: corroboration, not a single sample ----

    def test_raw_single_sample_never_corroborates(self):
        bridge = mon.HerdrBridge("disp-1", stable_samples=3)
        result = bridge.observe({"status": "done", "content": "frame", "content_hash": "h1"})
        self.assertFalse(result["corroborated"])
        self.assertEqual(result["terminal_classification"], "non_terminal")

    def test_idle_and_blocked_never_corroborate_even_if_stable(self):
        for status in ("idle", "blocked"):
            bridge = mon.HerdrBridge(f"disp-{status}", stable_samples=2)
            r1 = bridge.observe({"status": status, "content": "same", "content_hash": "h"})
            r2 = bridge.observe({"status": status, "content": "same", "content_hash": "h"})
            self.assertFalse(r2["corroborated"], f"{status} must never be corroborated into terminal")

    def test_agy_false_done_fixture_stays_non_terminal_while_output_advances(self):
        """Carries agy's actual observed status sequence from the recorded live
        evidence: `herdr agent prompt --wait` settled (a "done"-equivalent
        signal) while the pane still showed an active spinner ("Reading
        file...") and advancing output, and the worktree had no deliverables.
        Source: /tmp/aoffice-0b3e/evidence/agy-false-done-observation.md.
        """
        bridge = mon.HerdrBridge("t0exec-disp", stable_samples=3)
        spinner_frames = [
            "Reading file... (esc to cancel)",
            "Reading file..o (esc to cancel)",
            "Reading file...o. (esc to cancel)",
            "Reading file...oo. (esc to cancel)",
            "Reading file...ooo. (esc to cancel)",
        ]
        last = None
        for frame in spinner_frames:
            sample = {"status": "done", "content": frame, "content_hash": mon._sha256_hex(frame.encode())}
            last = bridge.observe(sample)
        self.assertFalse(
            last["corroborated"],
            "agy reported a settled/done signal while the pane content kept advancing "
            "(spinner); this must never be corroborated into a terminal event",
        )
        self.assertEqual(last["terminal_classification"], "non_terminal")

    def test_bridge_corroborates_once_content_and_status_are_genuinely_stable(self):
        bridge = mon.HerdrBridge("disp-real-done", stable_samples=3, completion_marker="## LANDING")
        report = "## LANDING\nfiles changed: 3\n"
        sample = {"status": "done", "content": report, "content_hash": mon._sha256_hex(report.encode())}
        r1 = bridge.observe(sample)
        r2 = bridge.observe(sample)
        r3 = bridge.observe(sample)
        self.assertFalse(r1["corroborated"])
        self.assertFalse(r2["corroborated"])
        self.assertTrue(r3["corroborated"])
        self.assertEqual(r3["observed_status"], "finish")
        self.assertEqual(r3["terminal_classification"], "success")

    def test_bridge_without_completion_marker_match_does_not_corroborate(self):
        bridge = mon.HerdrBridge("disp-no-marker", stable_samples=2, completion_marker="## LANDING")
        sample = {"status": "done", "content": "no report written yet", "content_hash": "h"}
        bridge.observe(sample)
        r2 = bridge.observe(sample)
        self.assertFalse(r2["corroborated"], "stable but missing the required independent completion marker")

    def test_disappeared_pane_classified_failure_not_assumed_success(self):
        bridge = mon.HerdrBridge("disp-gone", stable_samples=1)
        result = bridge.observe(None)
        self.assertTrue(result["corroborated"])
        self.assertEqual(result["observed_status"], "disappeared")
        self.assertEqual(result["terminal_classification"], "failure")

    def test_emit_herdr_bridge_event_end_to_end_and_idempotent_on_restart(self):
        bridge = mon.HerdrBridge("disp-e2e", stable_samples=2)
        sample = {"status": "done", "content": "stable", "content_hash": "hh"}
        first = mon.emit_herdr_bridge_event(self.state_dir, "sess-1", "fam-1", "disp-e2e", bridge, sample)
        self.assertIsNone(first, "1st sample: not yet corroborated")
        second = mon.emit_herdr_bridge_event(self.state_dir, "sess-1", "fam-1", "disp-e2e", bridge, sample)
        self.assertIsNotNone(second)
        self.assertEqual(second["source"], "monitor_bridge")

        # Simulate a monitor restart: a fresh bridge instance, same disk state.
        fresh_bridge = mon.HerdrBridge("disp-e2e", stable_samples=2)
        replay = mon.emit_herdr_bridge_event(self.state_dir, "sess-1", "fam-1", "disp-e2e", fresh_bridge, sample)
        self.assertEqual(replay["event_id"], second["event_id"], "restart must not re-fire a new terminal event")
        self.assertEqual(len(mon.list_events(self.state_dir, dispatch_id="disp-e2e")), 1)


@unittest.skipUnless(NODE_BIN, "no node binary resolvable in this environment")
class HerdrSnapshotParsingTestCase(unittest.TestCase):
    """Exercises herdr_agent_snapshot() against a fake herdr binary shaped
    exactly like the real CLI's live output (captured via `herdr agent get`/
    `herdr agent read` against this session's own panes, read-only)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-monitor-herdr-"))
        self.fake = FakeHerdr(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_unwraps_nested_agent_and_reads_plain_text(self):
        self.fake.write_state({
            "agent_get": {"t0exec": [{"agent_status": "working", "pane_id": "w1:p1"}]},
            "agent_read": {"t0exec": ["Reading file... (esc to cancel)\n"]},
        })
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import importlib.util as u\n"
            "spec = u.spec_from_file_location('office_monitor', %r)\n"
            "m = u.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "snap = m.herdr_agent_snapshot('t0exec')\n"
            "print(__import__('json').dumps(snap))\n"
        ) % (str(ROOT / "scripts"), str(ROOT / "scripts" / "office_monitor.py"))
        env = self.fake.env()
        env["OFFICE_HERDR_BIN"] = str(self.fake.herdr_path)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snap = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(snap["status"], "working")
        self.assertIn("Reading file", snap["content"])

    def test_snapshot_returns_none_on_agent_not_found(self):
        self.fake.write_state({"agent_get": {}})
        script = (
            "import importlib.util as u\n"
            "spec = u.spec_from_file_location('office_monitor', %r)\n"
            "m = u.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "print(__import__('json').dumps(m.herdr_agent_snapshot('missing')))\n"
        ) % (str(ROOT / "scripts" / "office_monitor.py"),)
        env = self.fake.env()
        env["OFFICE_HERDR_BIN"] = str(self.fake.herdr_path)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "null")


@unittest.skipUnless(NODE_BIN, "no node binary resolvable in this environment")
class ClosePanesHookTestCase(unittest.TestCase):
    """Subprocess-level tests of scripts/hooks/close_finished_panes.mjs
    against a fake herdr binary and a temporary ledger/state dir. Never
    touches a real Herdr pane."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-monitor-hook-"))
        self.fake = FakeHerdr(self.tmp)
        self.ledger = self.tmp / "panes.jsonl"
        self.state_dir = self.tmp / ".office"
        (self.state_dir / "events").mkdir(parents=True)
        self.hook = ROOT / "scripts" / "hooks" / "close_finished_panes.mjs"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ledger(self, entries):
        self.ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def _write_events(self, events):
        path = self.state_dir / "events" / "completions.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + ("\n" if events else ""))

    def _run_hook(self):
        env = self.fake.env()
        env["OFFICE_NODE_BIN"] = NODE_BIN
        env["OFFICE_PANE_LEDGER"] = str(self.ledger)
        env["OFFICE_STATE_DIR"] = str(self.state_dir)
        env.pop("OFFICE_RUN_ID", None)
        env.pop("OFFICE_SESSION_ID", None)
        return subprocess.run([str(self.hook)], env=env, capture_output=True, text=True, timeout=30)

    def _trusted_event(self, dispatch_id, source="process_exit", sequence=1, terminal="success", status="finish"):
        return {
            "event_id": mon.derive_event_id(dispatch_id, sequence),
            "session_id": "sess-1",
            "family_id": "fam-1",
            "dispatch_id": dispatch_id,
            "sequence": sequence,
            "observed_status": status,
            "terminal_classification": terminal,
            "source": source,
            "evidence_timestamp": "2026-09-15T00:00:00Z",
            "evidence_hash": mon._sha256_obj({"n": sequence}),
        }

    def test_no_closure_without_any_durable_event(self):
        self._write_ledger([{"pane_id": "w1:p1", "agent": "t0exec", "dispatch_id": "disp-1", "run_id": "run-1"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
            "pane_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
        })
        self._write_events([])  # no monitor evidence at all
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("closed", result.stdout)
        self.assertEqual(json.loads(self.ledger.read_text().splitlines()[0])["pane_id"], "w1:p1")

    def test_no_closure_on_raw_uncorroborated_herdr_source_alone(self):
        self._write_ledger([{"pane_id": "w1:p1", "agent": "t0exec", "dispatch_id": "disp-1", "run_id": "run-1"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
            "pane_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
        })
        # A raw, uncorroborated single-sample event exists but must not count.
        self._write_events([self._trusted_event("disp-1", source="herdr")])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("closed", result.stdout)

    def test_agy_false_done_fixture_hook_preserves_the_pane(self):
        """End-to-end version of the F9 fixture: the ledger/herdr side reports
        `done` exactly as agy did in the recorded live evidence, and no
        durable event exists yet (the bridge has not corroborated it,
        because the pane is still advancing output) — the hook must close
        nothing."""
        self._write_ledger([{"pane_id": "w3N:p6", "agent": "t0exec", "dispatch_id": "t0exec-disp", "run_id": "run-0b3e"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w3N:p6", "agent_status": "done"}],
            "pane_list": [{"pane_id": "w3N:p6", "agent_status": "done"}],
        })
        self._write_events([])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("closed", result.stdout)
        remaining = json.loads(self.ledger.read_text().splitlines()[0])
        self.assertEqual(remaining["dispatch_id"], "t0exec-disp")

    def test_closes_with_trusted_process_exit_event(self):
        self._write_ledger([{"pane_id": "w1:p1", "agent": "t0exec", "dispatch_id": "disp-1", "run_id": "run-1"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
            "pane_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
        })
        self._write_events([self._trusted_event("disp-1", source="process_exit")])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("closed 1 finished Herdr pane(s)", result.stdout)
        self.assertFalse(self.ledger.exists(), "ledger emptied after closing its only entry")

    def test_closes_with_trusted_monitor_bridge_event(self):
        self._write_ledger([{"pane_id": "w1:p1", "agent": "t0exec", "dispatch_id": "disp-1", "run_id": "run-1"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
            "pane_list": [{"pane_id": "w1:p1", "agent_status": "done"}],
        })
        self._write_events([self._trusted_event("disp-1", source="monitor_bridge")])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("closed 1 finished Herdr pane(s)", result.stdout)
        self.assertFalse(self.ledger.exists(), "ledger emptied after closing its only entry")

    def test_blocked_and_unknown_never_closed_even_with_a_durable_event(self):
        self._write_ledger([{"pane_id": "w1:p1", "agent": "t0exec", "dispatch_id": "disp-1", "run_id": "run-1"}])
        self.fake.write_state({
            "agent_list": [{"pane_id": "w1:p1", "agent_status": "blocked"}],
            "pane_list": [{"pane_id": "w1:p1", "agent_status": "blocked"}],
        })
        # Even a durable, trusted terminal event must not override a live
        # "blocked" read: blocked/unknown panes are unresolved, not complete.
        self._write_events([self._trusted_event("disp-1", source="process_exit")])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("closed", result.stdout)

    def test_concurrent_family_isolation_only_finished_dispatch_closes(self):
        self._write_ledger([
            {"pane_id": "w1:p1", "agent": "exec-a", "dispatch_id": "disp-A", "run_id": "run-1"},
            {"pane_id": "w1:p2", "agent": "exec-b", "dispatch_id": "disp-B", "run_id": "run-1"},
        ])
        self.fake.write_state({
            "agent_list": [
                {"pane_id": "w1:p1", "agent_status": "done"},
                {"pane_id": "w1:p2", "agent_status": "working"},
            ],
            "pane_list": [
                {"pane_id": "w1:p1", "agent_status": "done"},
                {"pane_id": "w1:p2", "agent_status": "working"},
            ],
        })
        self._write_events([self._trusted_event("disp-A", source="process_exit")])
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("w1:p1", result.stdout)
        self.assertNotIn("w1:p2", result.stdout)
        remaining = [json.loads(l) for l in self.ledger.read_text().splitlines() if l.strip()]
        self.assertEqual([r["pane_id"] for r in remaining], ["w1:p2"])


class OfficeLivenessTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-liveness-test-"))
        self.state_dir = self.tmp / "state"
        self.dispatch_dir = self.state_dir / "dispatches" / "disp-1"
        self.dispatch_dir.mkdir(parents=True)
        self.script = ROOT / "scripts" / "office_liveness.sh"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_completed_dispatch(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        (self.dispatch_dir / "pid").write_text(str(proc.pid))
        (self.dispatch_dir / "exit_code").write_text("0")
        (self.dispatch_dir / "meta.json").write_text(json.dumps({"started_at": int(time.time()) - 20}))
        (self.dispatch_dir / "output.log").write_text("some output\n")

    def test_without_identity_flags_output_is_unchanged(self):
        self._write_completed_dispatch()
        result = subprocess.run(
            [str(self.script), "--dispatch-id", "disp-1", "--state-dir", str(self.state_dir)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertNotIn("completion_event", payload)

    def test_with_identity_flags_emits_durable_process_exit_event(self):
        self._write_completed_dispatch()
        result = subprocess.run(
            [
                str(self.script), "--dispatch-id", "disp-1", "--state-dir", str(self.state_dir),
                "--session-id", "sess-1", "--family-id", "fam-1",
            ],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["completion_event"]["status"], "recorded")
        self.assertEqual(payload["completion_event"]["terminal_classification"], "success")

        status = mon.completion_status(self.state_dir, "disp-1")
        self.assertTrue(status["terminal"])
        self.assertEqual(status["source"], "process_exit")


if __name__ == "__main__":
    unittest.main()
