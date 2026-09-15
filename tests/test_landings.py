"""Command-level contract tests for the landing/checkpoint/completion/family CLI
surface T0 pinned for T4 (docs/v3-runtime-contracts.md §5; amendment v2 finding F6).

T2 is the sole owner of scripts/office_runtime.py and scripts/office_packets.py, so T2
implements and tests every one of these commands here; T4 only ever calls them. This
file exercises the CLI command functions (`cmd_*`) directly via an in-process Namespace,
the same convention tests/test_runtime.py uses -- the underlying family/amendment
mechanics are covered by tests/test_families.py and tests/test_amendments.py, so this
file stays at the command-level contract (argument handling, exit codes, stdout shape).
"""
import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load("office_runtime", "scripts/office_runtime.py")
fam = _load("office_family", "scripts/office_family.py")
monitor = _load("office_monitor", "scripts/office_monitor.py")

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(schema_dir, name="accept_complete.json"):
    return json.loads((FIXTURES / schema_dir / name).read_text())


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _invoke(func, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(Args(**kwargs))
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else None)


class LandingCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        fam.register_family(self.state_dir, "sess-001", "fam-core", "acme-corp/office-skills-example", 35,
                             requirements_version=1, plan_version=2, routing_version=2)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_tmp(self, obj) -> str:
        path = self.state_dir / f"_input_{id(obj)}.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return str(path)

    # ---- family-show / family-focus / family-list / family-update ----

    def test_family_show_defaults_to_focus_family(self):
        code, out = _invoke(rt.cmd_family_show, family_id=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["family_id"], "fam-core")

    def test_family_show_unknown_family_is_exit_2(self):
        code, out = _invoke(rt.cmd_family_show, family_id="fam-ghost", state_dir=str(self.state_dir))
        self.assertEqual(code, 2)

    def test_family_focus_moves_focus(self):
        fam.register_family(self.state_dir, "sess-001", "fam-other", "acme/repo", 9, set_focus=False)
        code, out = _invoke(rt.cmd_family_focus, family_id="fam-other", state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["current_focus"], "fam-other")

    def test_family_focus_unknown_family_is_exit_2(self):
        code, out = _invoke(rt.cmd_family_focus, family_id="fam-ghost", state_dir=str(self.state_dir))
        self.assertEqual(code, 2)

    def test_family_list_reports_versions_using_schema_field_names(self):
        code, out = _invoke(rt.cmd_family_list, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        entry = next(f for f in out["families"] if f["family_id"] == "fam-core")
        self.assertEqual(entry["versions"], {"requirements_version": 1, "plan_version": 2, "routing_version": 2})

    def test_family_update_advances_phase(self):
        code, out = _invoke(rt.cmd_family_update, family_id="fam-core", phase="planned",
                             latest_landing=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "updated")
        self.assertEqual(fam.get_family(self.state_dir, "fam-core")["phase"], "planned")

    def test_family_update_rejects_non_adjacent_phase_jump(self):
        code, out = _invoke(rt.cmd_family_update, family_id="fam-core", phase="closed",
                             latest_landing=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 2)

    # ---- amend ----

    def test_amend_applies_a_routing_delta_from_a_file(self):
        delta = _fixture("amendment")
        path = self._write_tmp(delta)
        code, out = _invoke(rt.cmd_amend, kind="routing", delta_file=path, family_id=None,
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "amended")
        self.assertEqual(out["resulting_versions"]["routing_version"], 3)

    def test_amend_conflict_is_exit_3(self):
        delta = _fixture("amendment")
        delta["expected_prior_versions"]["routing_version"] = 999  # stale
        path = self._write_tmp(delta)
        code, out = _invoke(rt.cmd_amend, kind="routing", delta_file=path, family_id=None,
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 3)

    def test_amend_schema_invalid_delta_is_exit_2(self):
        bad = _fixture("amendment", "reject_missing_identity_amendment_id.json")
        path = self._write_tmp(bad)
        code, out = _invoke(rt.cmd_amend, kind="routing", delta_file=path, family_id=None,
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 2)

    # ---- checkpoint ----

    def test_save_load_validate_checkpoint_round_trip(self):
        checkpoint = _fixture("checkpoint")
        path = self._write_tmp(checkpoint)
        code, out = _invoke(rt.cmd_save_checkpoint, file=path, family_id=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["checkpoint_id"], checkpoint["checkpoint_id"])

        code, out = _invoke(rt.cmd_load_checkpoint, file=None, checkpoint_id=checkpoint["checkpoint_id"],
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["checkpoint_id"], checkpoint["checkpoint_id"])

        code, out = _invoke(rt.cmd_validate_checkpoint, file=path)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"valid": True})

    def test_validate_checkpoint_rejects_missing_field(self):
        bad = _fixture("checkpoint", "reject_missing_identity_checkpoint_id.json")
        path = self._write_tmp(bad)
        code, out = _invoke(rt.cmd_validate_checkpoint, file=path)
        self.assertEqual(code, 2)
        self.assertFalse(out["valid"])

    def test_load_checkpoint_not_found_is_exit_1(self):
        code, out = _invoke(rt.cmd_load_checkpoint, file=None, checkpoint_id="nope", state_dir=str(self.state_dir))
        self.assertEqual(code, 1)

    # ---- landing ----

    def test_record_landing_then_validate_and_verify(self):
        landing = _fixture("landing")
        path = self._write_tmp(landing)
        code, out = _invoke(rt.cmd_record_landing, file=path, family_id=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "recorded")
        self.assertEqual(fam.get_family(self.state_dir, "fam-core")["latest_landing"]["landing_id"],
                          landing["landing_id"])

        code, out = _invoke(rt.cmd_validate_landing, file=path)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"valid": True})

        landing_verifiable = copy.deepcopy(landing)
        landing_verifiable["head_sha"] = rt._start_base_sha(ROOT)
        vpath = self._write_tmp(landing_verifiable)
        code, out = _invoke(rt.cmd_verify_landing, file=vpath, strict=False)
        self.assertEqual(code, 0)
        self.assertTrue(out["verified"])

    def test_record_landing_missing_validation_evidence_is_exit_4(self):
        landing = _fixture("landing")
        landing["validation_evidence"]["passed"] = False
        path = self._write_tmp(landing)
        code, out = _invoke(rt.cmd_record_landing, file=path, family_id=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 4)

    def test_verify_landing_rejects_mismatched_head_sha(self):
        landing = _fixture("landing")
        landing["head_sha"] = "0000000"
        path = self._write_tmp(landing)
        code, out = _invoke(rt.cmd_verify_landing, file=path, strict=False)
        self.assertEqual(code, 4)
        self.assertFalse(out["verified"])

    # ---- events (delegated to office_monitor) ----

    def test_record_list_ack_completion_status_round_trip(self):
        event = _fixture("completion-event")
        event["event_id"] = monitor.derive_event_id(event["dispatch_id"], event["sequence"])
        epath = self._write_tmp(event)

        code, out = _invoke(rt.cmd_record_event, file=epath, event_id=None, session_id=None,
                             family_id=None, dispatch_id=None, sequence=None, observed_status=None,
                             terminal_classification=None, source=None, evidence_timestamp=None,
                             evidence_hash=None, evidence_payload=None, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "recorded")

        code, out = _invoke(rt.cmd_list_events, dispatch_id=event["dispatch_id"], since_seq=None,
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(len(out["events"]), 1)

        code, out = _invoke(rt.cmd_ack_event, session_id=event["session_id"], family_id=event["family_id"],
                             dispatch_id=event["dispatch_id"], sequence=event["sequence"],
                             event_id=event["event_id"], state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "acknowledged")

        code, out = _invoke(rt.cmd_completion_status, dispatch_id=event["dispatch_id"],
                             state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertTrue(out["found"])
        self.assertEqual(out["observed_status"], "finish")

    def test_ack_event_sequence_gap_is_exit_3(self):
        event = _fixture("completion-event")
        event["sequence"] = 2
        event["event_id"] = monitor.derive_event_id(event["dispatch_id"], event["sequence"])
        epath = self._write_tmp(event)
        _invoke(rt.cmd_record_event, file=epath, event_id=None, session_id=None, family_id=None,
                dispatch_id=None, sequence=None, observed_status=None, terminal_classification=None,
                source=None, evidence_timestamp=None, evidence_hash=None, evidence_payload=None,
                state_dir=str(self.state_dir))
        code, out = _invoke(rt.cmd_ack_event, session_id=event["session_id"], family_id=event["family_id"],
                             dispatch_id=event["dispatch_id"], sequence=event["sequence"],
                             event_id=event["event_id"], state_dir=str(self.state_dir))
        self.assertEqual(code, 3)

    # ---- start receipt / review ----

    def test_record_start_receipt(self):
        receipt = _fixture("start-receipt")
        path = self._write_tmp(receipt)
        code, out = _invoke(rt.cmd_record_start_receipt, file=path, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "recorded")
        stored = json.loads((self.state_dir / "dispatches" / receipt["dispatch_id"]
                              / "start_receipt.json").read_text())
        self.assertEqual(stored, receipt)

    def test_record_review_and_validate_review(self):
        review = _fixture("review-result")
        path = self._write_tmp(review)
        code, out = _invoke(rt.cmd_record_review, file=path, state_dir=str(self.state_dir))
        self.assertEqual(code, 0)
        self.assertEqual(out["overall_status"], "PASS")

        code, out = _invoke(rt.cmd_validate_review, file=path)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"valid": True})

    def test_record_review_self_approval_is_rejected(self):
        review = _fixture("review-result")
        review["reviewer_id"] = review["producer_id"]
        path = self._write_tmp(review)
        code, out = _invoke(rt.cmd_record_review, file=path, state_dir=str(self.state_dir))
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
