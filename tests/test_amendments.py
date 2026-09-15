"""Tests for scripts/office_family.py's apply_amendment -- Task T2 (amendment v2
finding F8; docs/v3-runtime-contracts.md §2.3; protocol/families-and-amendments.md).

Covers the T2 dispatch-brief receipts about amendments specifically:
  1. A routing-only delta leaves requirements/plan approval intact; asserts resulting
     version numbers and the recorded event.
  2. A stale concurrent delta fails with NO partial write.
  3. A contract change invalidates only affected work.
  8. F8 amendment transition matrix, written so that a naive implementation which bumps
     every version on every delta fails it.
"""
import importlib.util
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


fam = _load("office_family", "scripts/office_family.py")
pk = _load("office_packets", "scripts/office_packets.py")

EVIDENCE_HASH = "sha256:" + ("a" * 64)


class AmendmentTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        fam.register_family(self.state_dir, "sess-1", "fam-a", "acme/repo", 1)

    def tearDown(self):
        self._tmp.cleanup()

    def _versions(self, family_id="fam-a"):
        full = fam.get_family(self.state_dir, family_id)
        return {k: full[k] for k in fam.VERSION_FIELDS}

    def _amend(self, family_id="fam-a", **overrides):
        kwargs = dict(
            state_dir=self.state_dir, family_id=family_id, kind="routing",
            affected_scopes=["T2"], reason="test reason", evidence="test evidence",
            evidence_hash=EVIDENCE_HASH, version_bumps={"routing_version": 2},
        )
        kwargs.update(overrides)
        return fam.apply_amendment(**kwargs)


class RoutingOnlyPreservesApprovalTests(AmendmentTestBase):
    """Receipt 1."""

    def test_routing_only_delta_leaves_requirements_and_plan_version_untouched(self):
        fam.approve_family_plan(self.state_dir, "fam-a", approved_by="user", quote="ship it")
        before = self._versions()
        result = self._amend()
        self.assertEqual(result["status"], "amended")
        self.assertEqual(result["resulting_versions"], {
            "requirements_version": before["requirements_version"],
            "plan_version": before["plan_version"],
            "routing_version": before["routing_version"] + 1,
        })
        after = self._versions()
        self.assertEqual(after["requirements_version"], before["requirements_version"])
        self.assertEqual(after["plan_version"], before["plan_version"])
        self.assertEqual(after["routing_version"], before["routing_version"] + 1)

    def test_routing_only_delta_leaves_approval_intact(self):
        approval = fam.approve_family_plan(self.state_dir, "fam-a", approved_by="user", quote="ship it")
        self.assertEqual(approval["status"], "approved")
        result = self._amend()
        self.assertTrue(result["approval_intact"])
        full = fam.get_family(self.state_dir, "fam-a")
        self.assertIsNotNone(full["approval"])
        self.assertEqual(full["approval"]["plan_version"], full["plan_version"])

    def test_routing_only_delta_records_the_event(self):
        result = self._amend()
        amendment_path = (self.state_dir / "families" / "fam-a" / "amendments"
                           / f"{result['amendment_id']}.json")
        self.assertTrue(amendment_path.exists())
        record = json.loads(amendment_path.read_text())
        self.assertEqual(record["kind"], "routing")
        self.assertEqual(record["resulting_versions"]["routing_version"], 2)
        errors = fam.rt.validate_with_schema(record, "amendment.schema.json")
        self.assertEqual(errors, [])

    def test_routing_only_delta_does_not_wake_the_planner(self):
        result = self._amend()
        self.assertFalse(result["wakes_planner"])
        self.assertEqual(result["paused_scopes"], [])


class StaleConcurrentDeltaTests(AmendmentTestBase):
    """Receipt 2: NO partial write on a stale/conflicting concurrent delta."""

    def test_stale_expected_prior_versions_is_rejected_without_any_write(self):
        # First writer succeeds and advances routing_version to 2.
        first = self._amend(expected_prior_versions={"requirements_version": 1, "plan_version": 1,
                                                       "routing_version": 1})
        self.assertEqual(first["status"], "amended")

        family_json = self.state_dir / "families" / "fam-a" / "family.json"
        registry_json = self.state_dir / "family_registry.json"
        family_before = family_json.read_bytes()
        registry_before = registry_json.read_bytes()

        # Second writer read routing_version=1 before the first writer committed (stale),
        # and now submits based on that stale read.
        stale = self._amend(
            version_bumps={"routing_version": 2},
            expected_prior_versions={"requirements_version": 1, "plan_version": 1, "routing_version": 1},
        )
        self.assertEqual(stale["status"], "conflict")

        self.assertEqual(family_json.read_bytes(), family_before)
        self.assertEqual(registry_json.read_bytes(), registry_before)

    def test_family_not_found_is_rejected_without_any_write(self):
        registry_before = (self.state_dir / "family_registry.json").read_bytes()
        result = self._amend(family_id="fam-does-not-exist")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "family_not_found")
        self.assertFalse((self.state_dir / "families" / "fam-does-not-exist").exists())
        self.assertEqual((self.state_dir / "family_registry.json").read_bytes(), registry_before)


class ContractChangeInvalidatesOnlyAffectedWorkTests(AmendmentTestBase):
    """Receipt 3."""

    def test_plan_contract_amendment_pauses_only_its_affected_scopes(self):
        result = fam.apply_amendment(
            state_dir=self.state_dir, family_id="fam-a", kind="plan_contract",
            affected_scopes=["T4"], reason="interface changed", evidence="diff excerpt",
            evidence_hash=EVIDENCE_HASH, version_bumps={"plan_version": 2},
        )
        self.assertEqual(result["status"], "amended")
        self.assertTrue(result["wakes_planner"])
        self.assertEqual(result["paused_scopes"], ["T4"])
        full = fam.get_family(self.state_dir, "fam-a")
        self.assertEqual(full["paused_scopes"], ["T4"])

    def test_invalidate_packets_only_touches_packets_in_affected_scope(self):
        base_packet = dict(
            run_id="r1", session_id="s1", family_id="fam-a", versions=(1, 1, 1),
            packet_version=1, observable_outcome="tests pass", blast_radius="low",
            allowed_mutations=[], protected_paths=[], validation_commands=["pytest"],
            selection_disclosure={"role": "executor", "triple": "a@b/c@d",
                                   "invocation_model_id": "m", "model_id": "m", "effort": "high",
                                   "harness": "h", "harness_version": "1", "reason": "x"},
            effective_config_hash="deadbeefcafebabe", base_sha="abcd1234",
        )
        affected = pk.create_execution_packet(task_id="T2", task_scope="T2", **base_packet)
        unaffected = pk.create_execution_packet(task_id="T3", task_scope="T3", **base_packet)
        pk.write_execution_packet(self.state_dir, affected)
        pk.write_execution_packet(self.state_dir, unaffected)

        count = pk.invalidate_packets(self.state_dir, plan_version=2, affected_scopes=["T2"])
        self.assertEqual(count, 1)

        affected_stored = pk.read_execution_packet(self.state_dir, affected["packet_id"])
        unaffected_stored = pk.read_execution_packet(self.state_dir, unaffected["packet_id"])
        self.assertTrue(affected_stored["meta"]["invalidated"])
        self.assertFalse(unaffected_stored["meta"]["invalidated"])


class AmendmentTransitionMatrixTests(AmendmentTestBase):
    """Receipt 8 (amendment v2 finding F8): each cell asserts resulting version numbers
    and the recorded event, not a non-zero exit. Written so that an implementation which
    bumps every version on every delta FAILS this matrix.
    """

    def test_matrix_routing_only_delta_does_not_wake_planner_and_touches_only_routing_version(self):
        before = self._versions()
        result = self._amend(kind="routing", version_bumps={"routing_version": before["routing_version"] + 1})
        self.assertEqual(result["status"], "amended")
        self.assertFalse(result["wakes_planner"])
        after = self._versions()
        # A naive "bump every version" implementation would fail these two assertions.
        self.assertEqual(after["requirements_version"], before["requirements_version"])
        self.assertEqual(after["plan_version"], before["plan_version"])
        self.assertEqual(after["routing_version"], before["routing_version"] + 1)

    def test_matrix_requirements_delta_still_fitting_plan_does_not_wake_planner(self):
        before = self._versions()
        result = self._amend(kind="requirements", affected_scopes=["T5"],
                              version_bumps={"requirements_version": before["requirements_version"] + 1})
        self.assertEqual(result["status"], "amended")
        self.assertFalse(result["wakes_planner"])
        after = self._versions()
        self.assertEqual(after["requirements_version"], before["requirements_version"] + 1)
        # A naive "bump every version" implementation would fail these two assertions.
        self.assertEqual(after["plan_version"], before["plan_version"])
        self.assertEqual(after["routing_version"], before["routing_version"])

    def test_matrix_plan_contract_delta_wakes_planner_and_pauses_only_affected_scopes(self):
        before = self._versions()
        result = self._amend(kind="plan_contract", affected_scopes=["T4"],
                              version_bumps={"plan_version": before["plan_version"] + 1})
        self.assertEqual(result["status"], "amended")
        self.assertTrue(result["wakes_planner"])
        self.assertEqual(result["paused_scopes"], ["T4"])
        after = self._versions()
        self.assertEqual(after["plan_version"], before["plan_version"] + 1)
        # A naive "bump every version" implementation would fail these two assertions.
        self.assertEqual(after["requirements_version"], before["requirements_version"])
        self.assertEqual(after["routing_version"], before["routing_version"])

    def test_matrix_running_dispatch_completes_its_atomic_unit_unless_explicitly_replaced(self):
        # Simulate a running dispatch by writing it directly into active_dispatches,
        # bypassing the CLI (no dispatch-spawn command is in T2's scope).
        full = fam.get_family(self.state_dir, "fam-a")
        full["active_dispatches"] = ["disp-running-1"]
        fam._write_json_atomic(fam._family_json_path(self.state_dir, "fam-a"), full)

        result = self._amend(kind="plan_contract", affected_scopes=["T2"],
                              version_bumps={"plan_version": 2})
        self.assertEqual(result["status"], "amended")
        # A plan-contract amendment pauses the scope but does not itself terminate the
        # dispatch already running against it -- it must complete its current atomic
        # unit unless a caller explicitly replaces it (§3.1), which this call did not do.
        self.assertEqual(result["active_dispatches"], ["disp-running-1"])
        full_after = fam.get_family(self.state_dir, "fam-a")
        self.assertEqual(full_after["active_dispatches"], ["disp-running-1"])

    def test_matrix_bumping_every_version_on_every_delta_is_rejected(self):
        """The out-of-scope-bump guard: this is the case that makes the matrix able to
        FAIL a naive "bump everything" implementation, rather than merely observing that
        it happens not to for these inputs."""
        before = self._versions()
        result = self._amend(
            kind="routing",
            version_bumps={
                "routing_version": before["routing_version"] + 1,
                "plan_version": before["plan_version"] + 1,
                "requirements_version": before["requirements_version"] + 1,
            },
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "amendment kind may only change its own version field")
        after = self._versions()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
