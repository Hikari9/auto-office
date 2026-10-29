# Validation

## Ordinary pull requests

Run `scripts/validate.sh --pr`. The gate runs the static ecosystem check, the tests in `tests/contracts/`, the named safety-smoke set, and tests selected for changed files. Selected tests run with `--all`, so their `integration`, `legacy`, and `slow` cases remain enabled. The selector uses the merge base with `origin/main`, prints its selected modules and reasons, and fails closed when the base or changed-file coverage is missing.

The contract directory must exist and contain at least one `test_*.py` module. A missing or empty directory blocks scoped validation. Safety-smoke tests are declared in `scripts/select_pr_tests.py`; they supplement contract and affected-domain coverage rather than replace either.

Scoped validation is a change-relevant check, not a full-suite pass. The pre-push hook runs the same gate and records scoped evidence separately from full-suite evidence. Landing attestation binds the committed validation script, selector, safety-smoke and integrity tests, and pre-push hook by digest. Changing one requires reviewing the attestation and updating the digest set together.

## Full validation

Run `scripts/validate.sh --full` for release validation, test infrastructure changes, deleted or renamed tests, or when a task explicitly requires the full regression suite. The full command runs all tests under `tests/` with `--all`; it records a full-suite result only when the checked tree remains unchanged.

`VALIDATE_BUILD=1 scripts/validate.sh --full` also builds the wheel and checks packaged resources. A scoped receipt never satisfies a full-suite requirement.

## Default pytest measurement

On 2026-10-29, `.venv/bin/python -m pytest -n 2` ran from the repository root with pytest's default marker selection. Each tree ran once, sequentially, on the same machine. The baseline is `origin/main` (`986e9d8d`). The branch is `bbbfb8b9`.

| Tree | Collected | Passed | Failed | Skipped | Wall time |
| --- | --- | --- | --- | --- | --- |
| `origin/main` | 3,840 | 3,837 | 2 | 2 | 321.29 s |
| branch | 3,880 | 3,876 | 3 | 2 | 342.87 s |

The branch includes five T1 commits that `origin/main` lacks (`ab090d55` through `515206b4`). The +40 collected nodes and the wall-time difference include them. This run does not separate the T1 contribution from this PR's.

Both trees fail `tests/test_checks_stale.py::test_any_other_edit_while_checks_run_is_stale` and `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_xs_xl_runs`. The branch also fails `tests/v31/test_argon_catalog_809.py::test_argon_exception_remains_l_executor_only_and_high_is_xl`. That test already fails at `515206b4`, before this PR.

pytest's summary total is one higher than the collected count on both trees. The cause is not investigated.

Each tree was timed once. A first baseline run measured 358.66 s on an archive without `.git`, so its git-dependent tests failed; it was discarded. Treat the 21.6 s difference as approximate.
