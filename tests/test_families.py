"""Tests for scripts/office_family.py -- Task T2 (docs/plans/v3-final-merge.md,
amendment v2 findings F5/F7; docs/v3-runtime-contracts.md §2.2, §4.2).

Covers the T2 dispatch-brief receipts that are about family registry/focus mechanics
rather than amendments (which live in tests/test_amendments.py) or the CLI
command-level contract (tests/test_landings.py):
  4. An ambiguous focus command mutates nothing and says why.
  5. Two sessions do not collide; restart reconstructs active families.
  6. Legacy state preserves unknown fields and evidence.
  7. F7 sticky-focus matrix + advisory projected-collision warning.
Plus the family/session config-tier precedence deliverable.
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


class TempStateDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class FocusAmbiguityTests(TempStateDirMixin, unittest.TestCase):
    """Receipt 4: an ambiguous focus command mutates nothing and states why."""

    def test_unqualified_command_with_no_registered_family_is_ambiguous_and_mutates_nothing(self):
        result = fam.resolve_focus_target(self.state_dir)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("reason", result)
        self.assertTrue(result["reason"])
        self.assertFalse(result["mutated"])
        self.assertFalse((self.state_dir / "family_registry.json").exists())

    def test_named_command_against_unknown_family_is_ambiguous_and_mutates_nothing(self):
        fam.register_family(self.state_dir, "sess-1", "fam-a", "acme/repo", 1)
        before = json.loads((self.state_dir / "family_registry.json").read_text())
        result = fam.resolve_focus_target(self.state_dir, requested_family_id="fam-ghost")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("fam-ghost", result["reason"])
        self.assertFalse(result["mutated"])
        after = json.loads((self.state_dir / "family_registry.json").read_text())
        self.assertEqual(before, after)


class StickyFocusMatrixTests(TempStateDirMixin, unittest.TestCase):
    """Receipt 7 (amendment v2 finding F7): two concurrent families A and B."""

    def setUp(self):
        super().setUp()
        fam.register_family(self.state_dir, "sess-1", "fam-a", "acme/repo", 1)
        fam.register_family(self.state_dir, "sess-1", "fam-b", "acme/repo", 2)
        # fam-a registered first, so it holds initial focus (register_family only
        # auto-focuses the first family registered).
        self.assertEqual(
            fam.load_family_registry(self.state_dir)["current_focus_family_id"], "fam-a"
        )

    def test_unqualified_command_resolves_only_current_focus_family(self):
        result = fam.resolve_focus_target(self.state_dir)
        self.assertEqual(result, {"status": "ok", "scope": "unqualified",
                                   "family_id": "fam-a", "mutated": False})

    def test_named_command_resolves_named_family_and_moves_focus(self):
        result = fam.resolve_focus_target(self.state_dir, requested_family_id="fam-b")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["family_id"], "fam-b")
        self.assertTrue(result["mutated"])
        self.assertEqual(
            fam.load_family_registry(self.state_dir)["current_focus_family_id"], "fam-b"
        )
        # Focus is now sticky on fam-b: a subsequent unqualified command resolves to it.
        self.assertEqual(fam.resolve_focus_target(self.state_dir)["family_id"], "fam-b")

    def test_ambiguous_named_command_mutates_neither_family_nor_focus(self):
        result = fam.resolve_focus_target(self.state_dir, requested_family_id="fam-c-unknown")
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result["mutated"])
        self.assertEqual(
            fam.load_family_registry(self.state_dir)["current_focus_family_id"], "fam-a"
        )

    def test_explicit_global_command_applies_to_both_families_without_moving_focus(self):
        result = fam.resolve_focus_target(self.state_dir, explicit_global=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope"], "global")
        self.assertEqual(sorted(result["family_ids"]), ["fam-a", "fam-b"])
        self.assertFalse(result["mutated"])
        self.assertEqual(
            fam.load_family_registry(self.state_dir)["current_focus_family_id"], "fam-a"
        )

    def test_projected_collision_warns_for_one_family_while_the_other_stays_runnable(self):
        quota_snapshots = {
            "fam-a": {"tightest_remaining_percent": 15, "projected_burn_percent": 10},  # 5 < reserve 20 -> warning
            "fam-b": {"tightest_remaining_percent": 90, "projected_burn_percent": 5},   # 85 >= reserve -> ok
        }
        result = fam.project_family_collisions(quota_snapshots, reserve_percent=20.0)
        self.assertEqual(result["collisions"], ["fam-a"])
        self.assertEqual(result["runnable"], ["fam-b"])
        # Advisory only: fam-b is unaffected and remains runnable despite fam-a's warning.
        self.assertEqual(result["per_family"]["fam-b"]["status"], "ok")
        self.assertEqual(result["per_family"]["fam-a"]["status"], "warning")

    def test_unknown_quota_is_neither_a_collision_nor_silently_safe(self):
        result = fam.project_resource_demand("fam-a", {"tightest_remaining_percent": None})
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["projected_remaining"])


class RestartReconstructionTests(TempStateDirMixin, unittest.TestCase):
    """Receipt 5: two sessions do not collide; restart reconstructs active families."""

    def test_two_registrations_against_the_same_state_dir_do_not_drop_either_family(self):
        fam.register_family(self.state_dir, "sess-A", "fam-a", "acme/repo", 1)
        fam.register_family(self.state_dir, "sess-B", "fam-b", "other/repo", 9)
        registry = fam.load_family_registry(self.state_dir)
        self.assertEqual(set(registry["families"]), {"fam-a", "fam-b"})

    def test_restart_reconstructs_registry_from_durable_family_records_when_registry_is_lost(self):
        fam.register_family(self.state_dir, "sess-1", "fam-a", "acme/repo", 1)
        fam.register_family(self.state_dir, "sess-1", "fam-b", "acme/repo", 2)
        fam.update_family_focus(self.state_dir, "fam-b")
        before = fam.load_family_registry(self.state_dir)

        (self.state_dir / "family_registry.json").unlink()
        self.assertFalse((self.state_dir / "family_registry.json").exists())

        after = fam.load_family_registry(self.state_dir)
        self.assertEqual(set(after["families"]), {"fam-a", "fam-b"})
        self.assertEqual(after["current_focus_family_id"], before["current_focus_family_id"])
        self.assertTrue((self.state_dir / "family_registry.json").exists())

    def test_restart_reconstructs_registry_when_registry_file_is_corrupted(self):
        fam.register_family(self.state_dir, "sess-1", "fam-a", "acme/repo", 1)
        (self.state_dir / "family_registry.json").write_text("{not json", encoding="utf-8")
        registry = fam.load_family_registry(self.state_dir)
        self.assertEqual(set(registry["families"]), {"fam-a"})


class LegacyMigrationTests(TempStateDirMixin, unittest.TestCase):
    """Receipt 6: legacy state preserves unknown fields and evidence."""

    def test_migration_preserves_unknown_fields_and_backs_up_original_bytes(self):
        legacy = {
            "family_id": "fam-legacy",
            "repo": "acme/legacy-repo",
            "issue": 12,
            "phase": "executing",
            # No requirements_version/routing_version -- pre-v3 shape.
            "plan_version": 3,
            "some_v1_only_field": "kept-verbatim",
            "evidence_bundle": {"kind": "legacy-evidence", "sha": "deadbeef"},
        }
        result = fam.migrate_legacy_family(self.state_dir, legacy, session_id="sess-1")
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(set(result["unknown_fields"]), {"some_v1_only_field", "evidence_bundle"})

        full = fam.get_family(self.state_dir, "fam-legacy")
        self.assertEqual(full["requirements_version"], 1)
        self.assertEqual(full["routing_version"], 1)
        self.assertEqual(full["plan_version"], 3)
        self.assertEqual(full["_legacy_unknown_fields"]["some_v1_only_field"], "kept-verbatim")
        self.assertEqual(full["_legacy_unknown_fields"]["evidence_bundle"], legacy["evidence_bundle"])

        backup = json.loads(Path(result["backup_path"]).read_text())
        self.assertEqual(backup, legacy)

    def test_migration_never_grants_ownership_or_certifies_a_stale_packet(self):
        legacy = {
            "family_id": "fam-legacy2",
            "active_dispatches": ["disp-should-not-carry-over"],
            "latest_landing": {"landing_id": "stale-land", "task_id": "T9", "head_sha": "aaaa",
                                "validation_evidence": "unverified", "evidence_hash": "sha256:" + "0" * 64},
            "ownership": {"holder_id": "claimed-holder", "role": "executor", "triple": "x@y/z@w"},
        }
        fam.migrate_legacy_family(self.state_dir, legacy, session_id="sess-1", family_id="fam-legacy2")
        full = fam.get_family(self.state_dir, "fam-legacy2")
        self.assertEqual(full["active_dispatches"], [])
        self.assertIsNone(full["latest_landing"])
        self.assertEqual(full["ownership"]["holder_id"], "unknown")

    def test_read_only_migration_persists_nothing(self):
        legacy = {"family_id": "fam-ro", "phase": "planned"}
        result = fam.migrate_legacy_family(self.state_dir, legacy, session_id="sess-1", persist=False)
        self.assertEqual(result["status"], "migrated_read_only")
        self.assertFalse((self.state_dir / "families" / "fam-ro").exists())
        self.assertFalse((self.state_dir / "family_registry.json").exists())
        # The backup is still written -- migration always preserves evidence, even read-only.
        self.assertTrue(Path(result["backup_path"]).exists())


class FamilyConfigTierPrecedenceTests(TempStateDirMixin, unittest.TestCase):
    """dispatch > family/project > repo > session > user > default
    (protocol/families-and-amendments.md)."""

    def _write(self, path: Path, obj: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")

    def test_each_tier_outranks_the_one_below_it(self):
        repo_root = Path(tempfile.mkdtemp())
        family_id = "fam-cfg"
        self.assertEqual(
            fam.resolve_family_config_tiers(repo_root, self.state_dir, family_id)[0]
            ["quota"]["reserve_percent"],
            20,
        )

        user_path = self.state_dir / "user.yaml"
        self._write(user_path, {"quota": {"reserve_percent": 30}})
        eff, _, _ = fam.resolve_family_config_tiers(repo_root, self.state_dir, family_id,
                                                      user_path=str(user_path))
        self.assertEqual(eff["quota"]["reserve_percent"], 30)

        eff, _, _ = fam.resolve_family_config_tiers(
            repo_root, self.state_dir, family_id, user_path=str(user_path),
            session_overrides={"quota": {"reserve_percent": 40}})
        self.assertEqual(eff["quota"]["reserve_percent"], 40)

        self._write(repo_root / ".auto-office" / "config.yaml", {"quota": {"reserve_percent": 50}})
        eff, _, _ = fam.resolve_family_config_tiers(
            repo_root, self.state_dir, family_id, user_path=str(user_path),
            session_overrides={"quota": {"reserve_percent": 40}})
        self.assertEqual(eff["quota"]["reserve_percent"], 50)

        self._write(self.state_dir / "families" / family_id / "config.yaml",
                    {"quota": {"reserve_percent": 60}})
        eff, _, _ = fam.resolve_family_config_tiers(
            repo_root, self.state_dir, family_id, user_path=str(user_path),
            session_overrides={"quota": {"reserve_percent": 40}})
        self.assertEqual(eff["quota"]["reserve_percent"], 60)

        eff, _, _ = fam.resolve_family_config_tiers(
            repo_root, self.state_dir, family_id, user_path=str(user_path),
            session_overrides={"quota": {"reserve_percent": 40}},
            dispatch_overrides={"quota": {"reserve_percent": 70}})
        self.assertEqual(eff["quota"]["reserve_percent"], 70)


if __name__ == "__main__":
    unittest.main()
