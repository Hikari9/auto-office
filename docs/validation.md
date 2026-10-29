# Validation

## Ordinary pull requests

Run `scripts/validate.sh --pr`. The gate runs the static ecosystem check, the tests in `tests/contracts/`, the named safety-smoke set, and tests selected for changed files. Selected tests run with `--all`, so their `integration`, `legacy`, and `slow` cases remain enabled. The selector uses the merge base with `origin/main`, prints its selected modules and reasons, and fails closed when the base or changed-file coverage is missing.

The contract directory must exist and contain at least one `test_*.py` module. A missing or empty directory blocks scoped validation. Safety-smoke tests are declared in `scripts/select_pr_tests.py`; they supplement contract and affected-domain coverage rather than replace either.

Scoped validation is a change-relevant check, not a full-suite pass. The pre-push hook runs the same gate and records scoped evidence separately from full-suite evidence. Landing attestation binds the committed validation script, selector, safety-smoke and integrity tests, and pre-push hook by digest. Changing one requires reviewing the attestation and updating the digest set together.

## Full validation

Run `scripts/validate.sh --full` for release validation, test infrastructure changes, deleted or renamed tests, or when a task explicitly requires the full regression suite. The full command runs all tests under `tests/` with `--all`; it records a full-suite result only when the checked tree remains unchanged.

`VALIDATE_BUILD=1 scripts/validate.sh --full` also builds the wheel and checks packaged resources. A scoped receipt never satisfies a full-suite requirement.
