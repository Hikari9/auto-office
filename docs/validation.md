# Validation

## Ordinary pull requests

Run `scripts/validate.sh --pr`. The gate runs the static ecosystem check, the tests in `tests/contracts/`, the named safety-smoke set, and tests selected for changed files. Selected tests run with `--all`, so their `integration`, `legacy`, and `slow` cases remain enabled. The selector uses the merge base with `origin/main`, prints its selected modules and reasons, and fails closed when the base or changed-file coverage is missing.

The contract directory must exist and contain at least one `test_*.py` module. A missing or empty directory blocks scoped validation. Safety-smoke tests are declared in `scripts/select_pr_tests.py`; they supplement contract and affected-domain coverage rather than replace either.

Scoped validation is a change-relevant check, not a full-suite pass. The pre-push hook runs the same gate and records scoped evidence separately from full-suite evidence. Landing attestation binds the committed validation script, selector, safety-smoke and integrity tests, and pre-push hook by digest. Changing one requires reviewing the attestation and updating the digest set together.

## Full validation

Run `scripts/validate.sh --full` for release validation, test infrastructure changes, deleted or renamed tests, or when a task explicitly requires the full regression suite. The full command runs all tests under `tests/` with `--all`; it records a full-suite result only when the checked tree remains unchanged.

`VALIDATE_BUILD=1 scripts/validate.sh --full` also builds the wheel and checks packaged resources. A scoped receipt never satisfies a full-suite requirement.

## Default pytest measurement

On 2026-10-29, `pytest -n 2` from the repository root collected 3,840 nodes on main (`986e9d8d`) in 265.18 seconds and 3,881 nodes on this branch (`cacb37c5`) in 305.19 seconds. Main reported 3,837 passed, 2 failed, and 1 skipped; this branch reported 3,876 passed, 3 failed, and 2 skipped. Both runs use pytest's default marker selection, so this is not the `--all` full-validation measurement tracked by issue #796. Main failed `tests/test_checks_stale.py::test_any_other_edit_while_checks_run_is_stale` and `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_xs_xl_runs`; the branch had those failures plus `tests/v31/test_argon_catalog_809.py::test_argon_exception_remains_l_executor_only_and_high_is_xl`.
