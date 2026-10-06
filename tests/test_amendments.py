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
import json
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


import office_family as fam
import office_packets as pk

EVIDENCE_HASH = "sha256:" + ("a" * 64)


@pytest.mark.legacy  # tests the 3.0 scripts/ surface
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


# ------------------------------------------------------------------ `office amend --no-review` (the orchestrator's veto)
#
# These drive the real CLI against the 3.1 runtime through the isolated Env of tests/v31. They are
# fast enough to run in the default tier, so the file's own check exercises them.

import importlib.util  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

_V31 = ROOT / "tests" / "v31"


def _v31_conftest():
    if "v31_env" not in sys.modules:
        sys.path.insert(0, str(_V31))
        spec = importlib.util.spec_from_file_location("v31_env", _V31 / "conftest.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["v31_env"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["v31_env"]


@pytest.fixture
def veto_env(request, tmp_path, monkeypatch):
    """An isolated Env; `@pytest.mark.parametrize("veto_env", ["v3.1"], indirect=True)` pins the review contract."""
    v = _v31_conftest()
    e = v.Env(tmp_path, monkeypatch, contract=getattr(request, "param", None))
    e.trust_snapshots = {}
    return v._activate(e, monkeypatch)


APPROVED = "VERDICT: APPROVED\nNEXT proceed"
MANUAL = {"OFFICE_JOBS": "manual"}
V31_PASS = "VERDICT: PASS"


def _start(env, *, approved: bool, gear: str = "express"):
    """A started inline run: its plan review is APPROVED, or still queued (jobs manual)."""
    v = _v31_conftest()
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    job_env = None if approved else MANUAL
    code, out = env.office("start", "fixture goal", "--gear", gear, "--planner", "inline", env=job_env)
    assert code == 0, out
    env.write_plan(v.PLAN_ONE)
    code, out = env.office("submit", env=job_env)
    assert code == 0, out


def _rows(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _plan_gates(env):
    return _rows(env, "SELECT plan_version, status FROM gates WHERE kind='plan_review' ORDER BY created_at")


def _plan_version(env):
    return _rows(env, "SELECT plan_version FROM runs")[0]["plan_version"]


def _veto(env, *extra, delta="reword the docstring", reason="doc-only wording", scope="plan", check=None, jobs=MANUAL):
    args = ["amend", scope, "--no-review"]
    if reason is not None:
        args += ["--reason", reason]
    return env.office(*args, *extra, "--", delta, check=check, env=jobs)


def test_veto_after_approved_review_makes_plan_p2_with_no_new_gate(veto_env):
    _start(veto_env, approved=True)
    before = _plan_gates(veto_env)
    assert [g["status"] for g in before] == ["done"] and _plan_version(veto_env) == 1
    code, out = _veto(veto_env)
    assert code == 0, out
    assert _plan_version(veto_env) == 2
    assert _plan_gates(veto_env) == before, "a vetoed amendment queues no plan-review gate for p2"
    assert "rereview" not in out and "plan-review" not in out, out
    skipped = _rows(veto_env, "SELECT * FROM events WHERE kind='plan.review_skipped'")
    assert len(skipped) == 1 and "doc-only wording" in json.dumps(skipped[0]) and "p2" in json.dumps(skipped[0])


def test_veto_while_review_is_pending_leaves_the_queued_gate_untouched(veto_env):
    _start(veto_env, approved=False)
    before = _plan_gates(veto_env)
    assert [(g["plan_version"], g["status"]) for g in before] == [(1, "queued")], before
    code, out = _veto(veto_env)
    assert code == 0, out
    assert _plan_version(veto_env) == 2
    assert _plan_gates(veto_env) == before, "the earlier version's queued gate keeps its status; none is added for p2"
    assert len(_rows(veto_env, "SELECT 1 FROM events WHERE kind='plan.review_skipped'")) == 1


def test_veto_leaves_a_running_gate_running(veto_env):
    _start(veto_env, approved=False)
    con = veto_env.con()
    with con:
        con.execute("UPDATE gates SET status='running' WHERE kind='plan_review'")
    con.close()
    code, out = _veto(veto_env)
    assert code == 0, out
    assert _plan_gates(veto_env) == [{"plan_version": 1, "status": "running"}]


def test_without_the_veto_the_same_amendment_still_queues_review_while_pending(veto_env):
    """The contrast: no --no-review, a pending review, and p2 does get its own gate."""
    _start(veto_env, approved=False)
    code, out = veto_env.office("amend", "plan", "--", "reword the docstring", env=MANUAL)
    assert code == 0, out
    assert _plan_version(veto_env) == 2
    assert [g["plan_version"] for g in _plan_gates(veto_env)] == [1, 2]
    assert not _rows(veto_env, "SELECT 1 FROM events WHERE kind='plan.review_skipped'")


def test_no_review_without_a_reason_is_a_usage_error(veto_env):
    _start(veto_env, approved=True)
    for reason in (None, "   "):
        code, out = _veto(veto_env, reason=reason)
        assert code == 2 and "--reason" in out and "next:" in out, (code, out)
    assert _plan_version(veto_env) == 1


def test_a_reason_without_no_review_is_a_usage_error(veto_env):
    _start(veto_env, approved=True)
    code, out = veto_env.office("amend", "plan", "--reason", "why", "--", "reword")
    assert code == 2 and "--no-review" in out, (code, out)
    assert _plan_version(veto_env) == 1


@pytest.mark.parametrize("extra,scope", [(("--contract",), "plan"), (("--requirements", "--quote", "u"), "plan"),
                                          ((), "requirements")])
def test_no_review_is_refused_for_contract_and_requirements_amendments(veto_env, extra, scope):
    _start(veto_env, approved=True)
    code, out = _veto(veto_env, *extra, scope=scope)
    assert code != 0 and "always get review" in out and "next:" in out, (code, out)
    assert _plan_version(veto_env) == 1
    assert len(_plan_gates(veto_env)) == 1
    assert not _rows(veto_env, "SELECT 1 FROM events WHERE kind IN ('plan.review_skipped','requirements.changed')")


@pytest.mark.parametrize("veto_env", ["v3.1"], indirect=True)
def test_the_v31_path_honours_the_veto(veto_env):
    env = veto_env
    _start(env, approved=False)
    before = _plan_gates(env)
    assert [(g["plan_version"], g["status"]) for g in before] == [(1, "queued")], before
    code, out = _veto(env)
    assert code == 0, out
    assert _plan_version(env) == 2
    assert _plan_gates(env) == before
    assert len(_rows(env, "SELECT 1 FROM events WHERE kind='plan.review_skipped'")) == 1
    code, out = env.office("amend", "plan", "--", "second, reviewed tweak", env=MANUAL)
    assert code == 0 and _plan_version(env) == 3
    assert [g["plan_version"] for g in _plan_gates(env)] == [1, 3], "without the veto p3 queues review as before"


if __name__ == "__main__":
    unittest.main()
