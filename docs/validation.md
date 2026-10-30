# Validation

## Ordinary pull requests

The run-level gate for an ordinary PR is `scripts/validate.sh --pr` plus an APPROVED convergence review. The full suite is not that gate (#892). Run `scripts/validate.sh --pr`. The gate runs the static ecosystem check, the tests in `tests/contracts/`, the named safety-smoke set, and tests selected for changed files. Selected tests run with `--all`, so their `integration`, `legacy`, and `slow` cases remain enabled. The selector uses the merge base with `origin/main`, prints its selected modules and reasons, and fails closed when the base or changed-file coverage is missing.

The contract directory must exist and contain at least one `test_*.py` module. A missing or empty directory blocks scoped validation. Safety-smoke tests are declared in `scripts/select_pr_tests.py`; they supplement contract and affected-domain coverage rather than replace either.

Scoped validation is a change-relevant check, not a full-suite pass. The pre-push hook runs the same gate on every push. The gate writes scoped and full-suite receipts separately; no landing check reads them. Landing attestation binds the committed validation script, selector, safety-smoke and integrity tests, and pre-push hook by digest. Changing one requires reviewing the attestation and updating the digest set together.

## Full validation

Run `scripts/validate.sh --full` for release validation, test infrastructure changes, deleted or renamed tests, or when a task explicitly requires the full regression suite. The full command runs all tests under `tests/` with `--all`; it records a full-suite result only when the checked tree remains unchanged.

Office runs the full suite under `verification.full_suite_timeout_seconds` (default 7200) or `check_timeout_seconds`, whichever is larger. The full suite is exactly `scripts/validate.sh` or `./scripts/validate.sh`, alone or with `--full`. Shell launchers (`bash`, `sh`), `env`, chains, and redirections are not recognized, so they keep `check_timeout_seconds` (default 1800), as does a `--pr` check. A timed-out check names the limit it hit and the setting to raise. Office does not re-run it automatically: a timeout under host load blocks the task until `office resume`. A full-suite check holds a host-wide check slot (`check_concurrency`) for up to `full_suite_timeout_seconds`, so a hung full suite can hold a slot for two hours by default.

`verification.job_timeout_seconds` is unused. No job-level timeout is enforced, so the full-suite check limit is not bounded by it.

`VALIDATE_BUILD=1 scripts/validate.sh --full` also builds the wheel and checks packaged resources. A scoped receipt never satisfies a full-suite requirement.

## Default pytest measurement

On 2026-10-29, `.venv/bin/python -m pytest -n 2` ran from the repository root with pytest's default marker selection, one sequential run per tree on the same machine. The baseline is `origin/main` (`986e9d8d`), run from a git worktree. The branch is `854ca15f`, this PR's HEAD. It sits on T1 R14 (`60a46a5c`).

| Tree | Collected | Passed | Failed | Skipped | Wall time |
| --- | --- | --- | --- | --- | --- |
| `origin/main` (`986e9d8d`) | 3,840 | 3,837 | 2 | 2 | 321.29 s |
| branch (`854ca15f`) | 3,885 | 3,878 | 6 | 2 | 297.24 s |

The branch carries T1 base commits that `origin/main` lacks, so the +45 collected nodes include T1 tests and this PR's new test. This run does not separate the two.

Both trees fail `tests/test_checks_stale.py::test_any_other_edit_while_checks_run_is_stale` and `tests/v31/test_issue809_run_gear.py::test_sonnet_claude_to_agy_self_fallback_applies_only_to_xs_xl_runs`. The branch also fails four tests in `tests/v31/test_notice_priority.py` and `tests/v31/test_quota_probe.py`. Those four fail at T1 base `60a46a5c` and do not fail on `origin/main`, so they come from T1 work, not this PR.

pytest's summary total is one higher than the collected count on both trees. The cause is not investigated.

Each tree was timed once. Wall time varies between runs: earlier branch runs took 342.87 s, 358.92 s and 400.32 s at earlier HEADs, so treat the wall-time difference as approximate.
