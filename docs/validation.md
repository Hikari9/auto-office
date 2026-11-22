# Validation

## Ordinary pull requests

The run-level gate for an ordinary PR is `scripts/validate.sh --pr` plus an APPROVED convergence review. The full suite is not that gate (#892). Run `scripts/validate.sh --pr`. The gate runs the static ecosystem check, the tests in `tests/contracts/`, the named safety-smoke set, and tests selected for changed files. Selected tests run with `--all`, so their `integration`, `legacy`, and `slow` cases remain enabled. The selector uses the merge base with `origin/main`, prints its selected modules and reasons, and fails closed when the base or changed-file coverage is missing.

The contract directory must exist and contain at least one `test_*.py` module. A missing or empty directory blocks scoped validation. Safety-smoke tests are declared in `scripts/select_pr_tests.py`; they supplement contract and affected-domain coverage rather than replace either.

Scoped validation is a change-relevant check, not a full-suite pass. The pre-push hook runs the same gate on every push. The gate writes scoped and full-suite receipts separately; no landing check reads them. Landing attestation binds the committed validation script, selector, safety-smoke and integrity tests, and pre-push hook by digest. Changing one requires reviewing the attestation and updating the digest set together.

## Full validation

Run `scripts/validate.sh --full` for release validation, test infrastructure changes, deleted or renamed tests, or when a task explicitly requires the full regression suite. The full command runs all tests under `tests/` with `--all`; it records a full-suite result only when the checked tree remains unchanged.

Office runs the full suite under `verification.full_suite_timeout_seconds` (default 7200) or `check_timeout_seconds`, whichever is larger. The full suite is exactly `scripts/validate.sh` or `./scripts/validate.sh`, alone or with `--full`. Shell launchers (`bash`, `sh`), `env`, chains, and redirections are not recognized, so they keep `check_timeout_seconds` (default 1800), as does a `--pr` check. A timed-out check names the limit it hit and the setting to raise. Office does not re-run it automatically: a timeout under host load blocks the task until `office resume`. A full-suite check holds a host-wide check slot (`check_concurrency`) for up to `full_suite_timeout_seconds`, so a hung full suite can hold a slot for two hours by default.

`verification.job_timeout_seconds` is unused. No job-level timeout is enforced, so the full-suite check limit is not bounded by it.

Workers: `scripts/validate.sh` runs pytest with `-n "${VALIDATE_WORKERS:-2}"`. The default stays 2 and accepts 1 to 4. `VALIDATE_WORKERS=4 scripts/validate.sh --full` is opt-in for an otherwise idle host only: with other runs, reviewers or a second suite on the machine, 4 workers contend for CPU and the lifecycle tests' subprocess timing, which costs more in flakes than it saves. Set it in the shell that runs the script. As an Office check command an `env`-style prefix is not recognized as the full suite (see above), so export it in the environment instead.

`VALIDATE_BUILD=1 scripts/validate.sh --full` also builds the wheel and checks packaged resources. A scoped receipt never satisfies a full-suite requirement.

## Visual review diagnostics (#942)

`office doctor` always tries to launch headless Chromium. Visual review is enabled when some gear preset in the effective config sets `funded_browser_verification`. When it is enabled and the launch fails, doctor counts one problem and exits 1. An unreadable or malformed config counts as enabled, so the failure is not hidden, and the line says the config is unreadable. The message names the fix: the `<python> -m playwright install chromium-headless-shell` command (with `--with-deps` on Linux), or, when `OFFICE_CHROME` or the default Google Chrome path is the binary being launched, the path to check. When visual review is not enabled, the same failure is reported and is not a failure. A missing optional `playwright` package is reported and never counts.

At closeout, each task whose visual review is required (planner-declared `visual:` block, or acceptance that reads as user-visible) is checked against its latest visual gate. A task is disclosed by ID when the review never ran (no covering gate) or finished without comparable evidence (`evidence_status` other than `COMPARABLE` or `NOT_APPLICABLE`, or a gate that is not `done`). A convergence lane gate covers the tasks frozen in its membership. A lane gate that predates recorded membership covers no task. While one exists, a required task is reported as unverifiable unless its latest covering gate is newer than that gate. DOM-only tests do not count as visual review. Disclosures appear as lines in the close report, in the final `office close done` line, and in the structured result as `visual_review_disclosures`.

## Full-suite measurement

On 2026-11-22 (#1302), `scripts/validate.sh --full` ran once per tree on the same machine with `-n 2` and no other full suite running. Collected counts come from `pytest --collect-only -q --all`.

| Tree | Collected | Passed | Failed | Skipped | Wall time |
| --- | --- | --- | --- | --- | --- |
| base `origin/main` (`4b614c73`) | 8,639 | 8,482 | 2 | 155 | 2381 s |
| #1302 T1 (`e38994da`) | 8,637 | 8,479 | 3 | 155 | 2083 s |

All three failures also fail on the untouched base when run alone. The base run is short one failure because one of them is intermittent there. The three tests are `test_declared_route_426.py::test_the_same_overkill_route_stays_skipped_for_an_undeclared_task`, `test_wait.py::test_a_working_agent_is_not_a_stall` and `test_wait.py::test_a_weekly_warning_pane_is_not_a_usage_limit_stall`. Each tree was timed once, so treat the wall-time difference as approximate.

The earlier default-tier measurement (2026-10-29, `pytest -n 2`: 3,840 collected, 321 s on `986e9d8d`) is historical and is superseded by this one.
