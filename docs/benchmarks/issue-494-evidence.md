# Issue #494 evidence: executor/worker route discovery

This is the T6 evidence for locked Option A: an explicitly discovery-eligible, unverified executor or worker
route is proven by one exact conformance probe and then tried once, bounded, with a known-working fallback.
It lists what was run, what it showed, and what was not achieved. Nothing here is a trust grant.

Run branch `office/d9f6f6d3/T6`, stacked on T5 (#512) and T4. Reference tree for the old behaviour: `eaf155a`.

## Result

| Criterion | Evidence | Status |
|---|---|---|
| S1-S12 each have a named test, isolated homes, scripted fake harnesses, no real model | `tests/v31/test_issue_494_scenarios.py`: 84 tests, `test_s01_...` to `test_s12_...` | pass |
| S1 starts from an empty `route_probes` table and reaches the trial only through the D5 preflight | `test_s01_discovery_assignment_from_a_cold_start_through_the_d5_preflight_only` | pass |
| S2 and S9 include the cold-start probe-fail path with no trial row | `test_s02_a_failed_probe_never_becomes_a_trial_...` (5 reason classes), `test_s09_a_cold_start_probe_failure_...` | pass |
| S11 repeated same-key probes keep separate histories | `test_s11_repeated_probes_of_one_fingerprint_keep_separate_histories` | pass |
| `scripts/route_replay.py` on a copy of the operator's runs.db | see "Routing replay" | run, counts below |
| Live exact probe and one real reversible trial reaching an accepted revision | see "Live evidence" | probe pass; task accepted through Office's review; trial row ended `abandoned` (defect found, below) |
| `scripts/validate.sh` and `pytest -n 2 --all` | see "Gates" | pass with the default `false` and with the shipped default `true` (after A10's three test edits) |
| Default flip to `true` | see "Default activation" | applied after S1-S12, `validate.sh` and `--all` passed; amendment A10 (plan p8) widened scope to the three default-pinning tests |

## Scenario suite

`.venv/bin/python -m pytest -n 2 tests/v31/test_issue_494_scenarios.py` runs the unit tier (79 tests). With `--all`
the five CLI tests join (84). Every test uses an isolated Office home, one scripted fake `codex`
(`tests/fixtures/route_probe/fake_harness.py`) and a real `runs.db`. The suite reuses T3's `World`, T4's `Cold`
(a persisted run with tasks, driving the real `dispatch.dispatch`) and T5's `Seed`.

| Scenario | Primary test | What it pins |
|---|---|---|
| S1 discovery assignment | `test_s01_...` | cold start (no probe, no event, no trial), probe before any dispatch row, recompute carries the handle, trial on one lease, no trust act or override |
| S2 failed probe | `test_s02_...` | unsupported effort, auth/quota, transient, isolation and conformance failures each keep their class, dispatch the known-working fallback, write no trial; sibling efforts stay routable; other harnesses stay routable |
| S3 exact scope | `test_s03_...` | a pass never qualifies another effort or model; a changed harness version, adapter hash or launch profile is a new key |
| S4 user denial | `test_s04_...` | automatic routing, plan `route:`, `--as`, `--review-as`, `amend route`, `--route` and the probe all refuse; nothing is reserved or launched; only a policy change re-enables |
| S5 overkill | `test_s05_...` | skipped only in automatic selection that matches role and size; manual selection and out-of-scope selection stay; shipped defaults hold no entries |
| S6 cost | `test_s06_...` | no route is removed for cost without a user ceiling; a user ceiling is a hard removal that names its tier; the shipped scale is not a ceiling |
| S7 unchanged authority | `test_s07_...` | planner, reviewers and verifiers never take a trial; L/XL, production, production-data and irreversible never get one; quarantine, quota reserve, capability, permission and archive gates still block; review stays independent and landing human |
| S8 atomic allocation | `test_s08_...` | 2 probes and 1 trial per run across a parallel wave; the rolling cap is 15 percent of the last 20 (boundary table); two concurrent dispatch commands cannot both take the last rolling slot; the reservation commits with the dispatch row or not at all |
| S9 safe fallback | `test_s09_...` | a trial that fails before work releases its lease and session and runs the fallback; a worker that could still write is stopped first; started work is never discarded; user-pinned routes are never switched; no trial without a known-working fallback |
| S10 learning without trust | `test_s10_...` | accepted trials teach the route through the existing maturity rules; no trust act; a standing quarantine is not cleared; launch, harness, adapter, environment, quota and brief failures are not the model's; a scripted end-to-end run |
| S11 audit | `test_s11_...` | the repeated-probe regression (below) and every route state in `office inspect route` |
| S12 migration and replay | `test_s12_...` | recorded requests replay to the `eaf155a` hashes; pinned pre-change runs, v3.1 runs and a 3.0 legacy run never discover; archived and confirmed-unsupported rows never return; DB changes are additive and the previous runtime still opens the file |

### S11 repeated same-key probe regression

One fingerprint is probed in run A under policy digest P1: the probe fails (transient), and a later reservation of the
same key is never finished and expires. The same key is then probed in run B under P2 and another plan version: it
passes and is tried. A standalone unbound `doctor.probe_route_record` hits the cache on that key, and another unbound
probe of a different effort runs fresh. Checked:

* every attempt keeps its own events, each with its run (or NULL), plan version, policy digest, outcome and timestamp;
* run A's `route_discovery_events` rows are byte-identical before and after run B and the unbound probes;
* the mutable `route_probes` row now holds B's pass while A's failure and expiry stay in the events;
* `office inspect route` for run A lists A's two attempts and not B's; for run B the reverse; both list the unbound ones.

### Do the tests catch regressions

Each mutation below was applied to `src/`, the named test was run, and the file was restored. All failed as required.

| Mutation | Failing test |
|---|---|
| the probe ignores a denial | `test_s04_a_denied_route_is_excluded_from_every_selection_path_and_never_probed` |
| the rolling cap is not rechecked in the dispatch transaction | `test_s08_the_rolling_cap_binds_in_the_transaction_when_another_run_takes_the_last_slot` |
| the probe key drops the effort | `test_s03_a_pass_qualifies_only_its_exact_fingerprint` |
| the per-run trial cap is not rechecked | `test_s08_per_run_caps_hold_across_a_parallel_wave` |
| a run pinned before this change may discover | `test_s12_runs_pinned_before_this_change_keep_their_config_and_route_identically` |
| the fallback starts without stopping the worker | `test_s09_a_worker_that_could_still_write_is_stopped_before_the_fallback_starts` |
| reviewers discover too | `test_s07_a_planner_reviewer_or_verifier_never_takes_a_trial_route[planner]` |
| environment failures count against the model | `test_s10_launch_harness_adapter_environment_and_quota_failures_are_not_the_models[transient-launch]` |
| audit events lose their run | `test_s11_repeated_probes_of_one_fingerprint_keep_separate_histories` |
| the shipped tier becomes a hard ceiling | `test_s06_a_number_that_came_from_the_shipped_tier_is_never_a_hard_ceiling` (a first version of the S6 test still passed this mutation, so the test was strengthened) |
| a failed probe still becomes a trial | `test_s02_a_failed_probe_never_becomes_a_trial_and_dispatches_the_known_working_fallback` |

## Routing replay

`scripts/route_replay.py` replays the recorded executor/worker `route_audit` decisions of a copy of `runs.db`. The
table keeps the scored decision, not the raw request, so each request is rebuilt from the copy with HEAD's own
builder (`candidates.route_role`, no quota probe, no model call). It never opens the live file: it refuses it, or takes
a copy through SQLite's read-only backup API (`--snapshot-live`). Its self-test (`--self-test`) builds a database,
checks the source is untouched, that discovery-off and pinned pre-change decisions are identical, that the simulated
caps bind, that an unreadable user or repo policy (T3 refuses every route with `policy-unreadable`, A14) is counted as
`refused:policy-unreadable` rather than compared, and that no goal or title text reaches the report. A replay that skips
every recorded decision exits 3.

Policies compared: `pinned` (the run's own), `pinned-off` (the same with discovery forced off), `head-off`, `head-on`
(HEAD's resolved policy; shipped caps; allocation simulated in recorded order). A trial counts as "would have been"
only under the assumption that the probe passes: no model was called.

Run on a copy of the operator's runs.db taken 2026-10-10 (counts only; no goal, title or brief text was read into the report):

| | count |
|---|---|
| executor/worker decisions replayed | 696 (632 plan previews, 53 dispatches, 11 reroutes), 27 runs, 0 skipped |
| runs pinned before discovery existed | 27 of 27 |
| pinned vs the same policy with discovery forced off | identical 696, different 0 |
| pinned now vs the primary recorded then | same 420, different 276 (catalog, evidence and quota drift) |
| HEAD with discovery off: decisions without a discovery block | 696 of 696 |
| HEAD with discovery on: selection unchanged | 696 of 696 |
| dispatch decisions that would have drawn a probe | 0 of 64 |
| of those, would have become a trial | 0 |
| plan previews that showed a probe candidate (spend nothing) | 15 of 632 |
| dispatch decisions stopped by | risk 43, margin 17, rolling cap 4 |
| caps that bound at dispatch | rolling cap 4 (a cold window: six decisions are needed before 15 percent allows one trial) |

Reading: discovery off changes nothing for any recorded run. Discovery on would have changed none of the 64 recorded
dispatches: most operator tasks were not S/M tasks of declared repo or local blast radius (the risk gate), and the rest
were stopped by the cost/margin bound or by the cold rolling window. The first cap to bind in practice is the rolling
window, then the per-run caps; no run in the history would have reached either.

## Live evidence

Goal: one real exact probe of a discovery-eligible route and one real reversible trial, reaching an accepted revision
through an isolated Office's normal review. Authority: requirements r3 named action, at most 2 probes and 1 trial.

Isolation. A venv built from `git archive` of the run branch at `2112889` (`/tmp/t6-live/venv`), a scratch git repo
at `/tmp/t6-live/repo`, and a wrapper that runs every command with a scrubbed environment (no `OFFICE_*`,
`AUTO_OFFICE_*` or `HERDR_*` variables of the orchestrating run) and
`OFFICE_DATA_HOME`, `OFFICE_STATE_HOME`, `OFFICE_USER_CONFIG` and `AUTO_OFFICE_RUNS_DB` under `/tmp/t6-live`. The
installed tool, the recovered 3.4.1 runtime, the operator's runs.db, this run's orchestration state and the root
checkout were not used or written. `uv tool install`, `office install` and `office upgrade` were not run. Real `codex`
and `claude` were the installed harnesses; `OFFICE_LAUNCHER=sync` and `OFFICE_JOBS=inline` made the run synchronous.

Isolated user policy: discovery on, `max_trial_percent_rolling_20: 100` (so the draw lands), exploration off, and
denials that keep the draw on one route: `codex/gpt-6.1-sol` at low, high, xhigh and max, `claude/claude-haiku-5-5`,
`claude/haiku`, `agy/gemini-3.8-flash@high`. Codex 0.162.0 lists `gpt-6.1-sol` with low, medium, high, xhigh, max and
ultra; the catalog's discovery-eligible efforts are the first five, so `medium` was probed. The known-working fallback
and the reviewer came from the shipped trust baseline (`claude@2/claude-sonnet-5-5@high`, `codex@0/gpt-6-luna@high`):
no trust act was written.

Commands, in order (each through the wrapper, from `/tmp/t6-live/repo`):

```
office start "add() returns the sum" --gear direct+review --planner inline --size-class S
office submit                      # plan p1, blast_radius: repo
office doctor --probe-route codex/gpt-6.1-sol@medium
office approve plan --quote "pre-authorized in requirements r3 (T6 brief, named action: real reversible installed-harness probe and trial) for this isolated scratch run"
office dispatch T1
```

Receipts (`/tmp/t6-live/runs.db`, read back with sqlite3):

| | |
|---|---|
| probe | `codex/gpt-6.1-sol@medium` fingerprint `codex\|0.162.0\|gpt-6.1-sol\|medium\|sha256:ee59ad06...\|worker`: pass in 14 s; model and effort read back from the harness metadata; reply, write, write boundary and process cleanup verified. Events: `probe-reserved` (manual, no run) then `probe-result` pass |
| preflight | the D5 preflight found that pass fresh: `probe-cache-hit` (cached-fresh), so the run spent no second probe |
| trial | `dispatch-linked` (trial), `trial-reserved`, `trial-launched` on dispatch `D4a75136c`, `codex@0/gpt-6.1-sol@medium`, fallback `claude@2/claude-sonnet-5-5@high`; `route_audit` dispatch row: primary and dispatched route both the trial route |
| executor | edited `calc.py`, committed `17af395b`, wrote its self-review ledger (36,739 tokens) |
| review | `D36f0b5a1`, `code_reviewer` on `codex@0/gpt-6-luna@high` (not the trial route), lane convergence approved; T1 `accepted` on `R1-18c727ac`; integration verified |
| authority | `adapter_trust_acts` 0 rows, `recorded_overrides` 0 rows; trust for the trial route reads `valid-unverified` |
| learner | `office inspect learner`: `codex/gpt-6.1-sol@medium 1/1 landed`; "trial evidence (quality only; trials never change adapter trust): 1/1 trial dispatches landed, trust valid-unverified" |

What did not go cleanly, and why:

1. The trial row ended `abandoned`, not `submitted`/`accepted`. The headless launch (no Herdr pane) never writes the
   dispatch's `agent.env`, but the brief and `office preflight` tell the executor to run `. <agent.env> && office submit`.
   The executor did the work, preflight said ready, the sourcing line failed (`missing .../agent.env`, exit 127), the
   executor raised a blocker and exited with no revision, and Office recorded `trial-abandoned` ("the worker ended
   without a submission and without a recovery"). This is not specific to trials or to `OFFICE_LAUNCHER=sync`: any
   executor launched headless is told to source a file the headless path does not create
   (`dispatch._launch_external` and the Herdr path call `write_agent_env`; `launch` with `sync` or the detached
   process does not). It is outside T6's scope and is reported, not fixed.
2. To finish the evidence, the operator wrote the missing file with Office's own `dispatch.write_agent_env` (what a pane
   launch does), ran `office preflight` (ready) and the printed `. agent.env && office submit` from the worktree. That
   submit, not the executor's, captured `R1-18c727ac`. Everything after it was Office's: checks, the independent
   convergence review, acceptance, integration. The learner reads the accepted revision as the trial route's evidence.
3. Because the trial row is `abandoned`, no `trial-accepted` terminal event was recorded in the live run
   (`record_trial_outcomes` skips an abandoned trial). S10's scripted CLI test covers the clean path
   (`probe-reserved` ... `trial-submitted`, `trial-accepted`); the live run shows the learner's route-attributed
   evidence without it.
4. A second trial was not run to get a clean row: the named action allows one trial.
5. Not exercised live: a failing probe, an unsupported effort and the fallback launch. Those are covered by the
   scripted suite (S2, S9). The quota probe for `agy` timed out (20 s) during the run and was reported as unknown;
   `codex` and `claude` quotas read fine.

## Gates

Run in this worktree with `.venv` (Python 3.12, `uv pip install -e '.[test,visual]'`), `PATH` led by `.venv/bin` and
`OFFICE_PINNED_LEGACY=1`, as `scripts/validate.sh` sets them.

| Gate | Tree | Result |
|---|---|---|
| `pytest -n 2 tests/v31/test_issue_494_scenarios.py` | shipped default `false` | 79 passed (unit tier) |
| `pytest -n 2 --all tests/v31/test_issue_494_scenarios.py` | shipped default `false` | 84 passed |
| `scripts/route_replay.py --self-test` | shipped default `false` | passed |
| `scripts/validate.sh` (ecosystem check, unit tier, adapter validation) | shipped default `false` | passed |
| `pytest -n 2 --all` | shipped default `false` | 3453 passed, 18 failed on the first run, which had no gate environment: 17 need the venv on `PATH` or the `visual` extra and fail the same way on the base commit `d066334`; 1 (`test_inprocess_launch ... [hup]`) is a load flake. All 17 pass with `validate.sh`'s environment and the `visual` extra; `[hup]` passes alone and in a 35-test re-run |
| `scripts/validate.sh` | shipped default `true`, before A10's test edits (trial flip) | 1988 passed, **2 failed** |
| `pytest -n 2 --all` | shipped default `true`, before A10's test edits (trial flip) | 3610 passed, **3 failed**, 1 flake (`[hup]` again, under load) |
| `scripts/validate.sh` | shipped default `true`, A10 applied (`25d0280`) | 1990 passed, 1 skipped, 0 failed |
| `pytest -n 2 --all` | shipped default `true`, A10 applied (`25d0280`) | 3614 passed, 2 skipped, 0 failed (`[hup]` passed) |
| `pytest -n 2 tests/v31/test_issue_494_scenarios.py`, `scripts/route_replay.py --self-test` | shipped default `true` | 79 passed; self-test passed |
| `scripts/validate.sh` | after restack onto T4 R15 / T5 R14 and A14 (`cc4956e`) | passed |
| `pytest -n 2 --all` | after restack and A14 (`cc4956e`) | 3632 passed, 2 skipped, 1 failed: a browser test worker crashed (`test_layout.py::test_scroll_buttons_...`); it and the other four files that failed in an earlier run overlapping another suite pass on rerun (89 passed) |

The three failures in the trial flip were the same three tests, each pinning the old shipped default `off` (listed
under "Default activation"). A10 updated exactly those, and nothing else in the repository depends on the default.

## Default activation

State at this commit: **`routing.discovery.enabled` is `true` in `config/config.default.yaml`** (roles executor and
worker only; the other settings are unchanged).

The flip was applied only after S1-S12, `scripts/validate.sh` and `pytest -n 2 --all` passed with the default `false`.
Trying it first showed that `validate.sh` and `--all` fail on three existing tests that pin the previous shipped
default. They are outside T6's original scope (T1's and T3's files), so a scope request (#3029) was raised and the
default stayed `false` until the orchestrator answered with amendment A10 (plan p8). A10 covers exactly these edits,
in commit `25d0280`:

1. `tests/v31/test_route_policy_config.py::test_new_defaults_have_no_hard_ceiling_or_user_policy` now expects the
   shipped settings to equal `{**DISCOVERY_DEFAULTS, "enabled": True}` and asserts the code constant
   `DISCOVERY_DEFAULTS["enabled"]` is still `False` (a config without the key stays off).
2. `tests/v31/test_route_policy_config.py::test_policy_digest_covers_settings_policy_and_ceiling_source_only` flips
   the digest with `routing.discovery.enabled=false`, since `true` is now the shipped value and does not change the digest.
3. `tests/v31/test_route_discovery_routing.py::test_route_role_with_discovery_off_a_pinned_run_a_manual_route_or_a_reviewer_never_discovers`
   asserts the shipped setting is on, then runs its no-pool check on a copy with `enabled` set false.

No invariant of S1-S12 failed. With the flip applied, the gates in "Gates" all pass, so the default was not reverted.

What the flip changes: a run started after it pins `discovery.enabled: true` for executor and worker, and discovery
then draws at most 15 percent of recent executor/worker decisions, 2 probes and 1 trial per run. A run started
before it, a `v3.1` review-contract run and a 3.0 legacy run keep their pinned policy and never discover (S12). The
replay above shows the effect on the operator's history: none of the 64 recorded dispatch decisions would have drawn a probe.

Not achieved, with cause: the live trial row ended `abandoned` rather than `accepted` (the headless `agent.env` gap
in "Live evidence" and "Findings outside T6's scope"). The task itself was accepted through Office's review, and the
clean `trial-accepted` path is covered by S10's scripted test only.

## Findings outside T6's scope

* A headless launch (no Herdr pane) never writes the dispatch's `agent.env`, which the brief and `office preflight`
  tell the executor to source before `office submit` (`src/office/dispatch.py`, `launch()` outside the Herdr and
  external paths; the brief text is in `briefs.py`). A headless executor cannot submit. Found in the live run.
* When a trial's worker ends without submitting, the trial is recorded `abandoned`. If the revision is then submitted
  by someone else (as in the live run), the task is accepted but the trial row stays `abandoned` and gets no
  `trial-accepted` event. The learner still reads the dispatch as landed.

