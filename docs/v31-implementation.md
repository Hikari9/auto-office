# Auto Office 3.1.0 — implementation

> Superseded for new runs: the review policy here is replaced by [`review-convergence.md`](review-convergence.md) (#337). It still governs runs pinned to the v3.1 review contract.

Status: **implemented** (this document describes the code in `src/office/`). The product and
design contracts it implements are the [v3.1 Wayfinder](https://github.com/Hikari9/auto-office/issues/152):
the [charter](https://github.com/Hikari9/auto-office/issues/153#issuecomment-5832359821),
[rolling review and gates](v31-rolling-review-gates.md) (#160),
[visual gates](https://github.com/Hikari9/auto-office/issues/158#issuecomment-5854877175),
the [CLI prototype](https://github.com/Hikari9/auto-office/issues/159#issuecomment-5854908267), and the
[final ratification](https://github.com/Hikari9/auto-office/issues/157#issuecomment-5854948822).
Where the earlier [CLI architecture](office-cli-architecture.md) disagrees, the later decisions win
(see §14).

## 1. Shape

| Concern | Module |
|---|---|
| Exact version identity | `version.py` |
| Machine paths, repo identity | `paths.py` |
| runs.db schema, migrations, transactions | `db.py` |
| Row access, events, outbox, packets | `state.py` |
| Config tiers, gears, gates, risk | `config.py` |
| Routing, trust, reward (ported from v3) | `routing.py`, `scoring.py` |
| Route requests built by the runtime | `candidates.py`, `adapters.py` |
| Run discovery and session binding | `discovery.py` |
| Version-pinned front door | `frontdoor.py`, `runtime_default.py` |
| 3.0 run detection and exact-commit retention | `legacy.py` |
| start / resume / close / abandon / list | `lifecycle.py` |
| PLAN.md parsing | `planfile.py` |
| Plans and rolling plan review | `plans.py` |
| Dispatch, leases, worktrees, launch, supervisor | `dispatch.py` |
| Submission and immutable revisions | `submit.py` |
| Gates, convergence, acceptance, closeout checks | `gates.py` |
| Integration of the composed result | `integration.py` |
| Amendments, delivery, `ack` | `amend.py` |
| Visual applicability, capture, judgment | `visual.py` |
| Image-capability conformance | `conformance.py` |
| User authority (`approve`) | `authority.py` |
| Durable jobs without a daemon | `jobs.py` |
| Status and the `next:` line | `guide.py` |
| `inspect` | `inspect_cmd.py` |
| `prune` | `prune.py` |
| `raw` compatibility window | `compat.py` |
| Hooks, install, doctor | `hooks.py`, `install.py`, `doctor.py` |
| CLI transport | `cli.py` |

## 2. One state authority

`runs.db` (SQLite, WAL) is the only lifecycle authority. Every semantic transition runs inside
`db.transaction` (`BEGIN IMMEDIATE`), so preconditions read inside the block cannot change before
commit. The v3 recorder tables (`runs`, `dispatches`, `findings`, `validations`, `leases`, …) keep
their definitions and gain only nullable columns, so a retained 3.0 runtime keeps writing to the
same file. New tables: `requirements`, `authorizations`, `plans`, `tasks`, `revisions`, `gates`,
`amendments`, `deliveries`, `events`, `cursors`, `outbox`, `evidence`, `session_bindings`,
`capability_proofs`, `compat_calls`, `visual_refs`, `deviations`, `schema_meta`.

Files under a run directory are generated read-only views (`view.json`) or evidence. Editing
them changes nothing. `.office/active/<run>` and `.office/sessions/*.json` in the primary checkout
are stat-only indexes for the hook fast path.

**Outbox.** A transition that needs outside work (launch an agent, run checks, review, capture,
integrate, nudge a worker) inserts an `outbox` row in the same transaction. `jobs.kick` starts one
short-lived `office _job <id>` per runnable row; the job claims it atomically, works, and records
its result in a second transaction. A claim whose process is dead is reclaimed by the next
`office` command (`status`, `resume`, or any command that enqueues). Nothing stays running.

## 3. Version identity and pinning

`office --version` is exact: a wheel reports `3.1.0`; a source checkout reports
`3.1.0+g<sha12>` and adds `.d<digest>` when runtime files are modified (PEP 440 local version).

- Every run has `office_version`, its MAJOR.MINOR release line (`3.2`); `plugin_commit` keeps
  the exact creating runtime. Runs created before 3.2 hold an exact version (`3.1.0`) and are
  read as its line. Every runtime-owned packet, job, dispatch and event records the exact
  runtime that wrote it; `state.check_packet` and `jobs.execute` compare release lines only.
- PATCH releases (`3.2.0` → `3.2.1`) are bugfix-only and reach every run on their line with no
  action. `frontdoor.ensure_runtime` re-executes a command under the newest registered patch of
  the run's line (`~/.local/share/auto-office/runtimes/<version>.json`, written by
  `office install`), or exits 5 naming the line and `office upgrade`.
- Crossing a MINOR or MAJOR is explicit: `office upgrade [run] [--to X.Y]` dry-runs by default
  and `--apply` commits. It refuses, naming them, while dispatches are live or jobs claimed;
  restamps queued jobs; runs any `upgrade.MIGRATIONS` entry for the pair (the runs.db schema
  itself migrates additively on open); records `run.upgraded` with from/to; and leaves
  requirements, plans, tasks, gates, amendments and authorizations unchanged. Rollback is
  `office upgrade <run> --to <previous line> --apply`, allowed only to a line the run was on.
  `office doctor` and `office status` print the upgrade command for a run on an older line.
- A 3.0 run (its `state.json` has no `office_version`) is pinned to its `plugin_commit`. The
  runtime materializes exactly that commit with `git archive` into `runtimes/legacy-<sha>` and
  serves the run from there (`office resume`, `office raw`, and the guard in
  `scripts/office_runtime.py` all route there). Missing commits are an actionable blocker.

## 4. Discovery and binding

`--run`/`--state-dir` > `OFFICE_STATE_DIR`/`OFFICE_RUN_ID` > session binding > the sole active run
in the repository > exit 3 listing candidates. Modification time never selects a run. Session keys
are, in order: explicit `--harness/--session`, `OFFICE_HARNESS/OFFICE_SESSION`, the Herdr pane
(`HERDR_PANE_ID`, visible to both commands and hooks), and the nearest harness ancestor process.
`office start` and `office resume` bind; hooks, status, and opening a session never create or bind
a run. Dispatched workers carry `OFFICE_RUN_ID`/`OFFICE_DISPATCH_ID` and never run discovery.

## 5. Dispatch

`office dispatch T1 T2 --parallel` launches both; `office dispatch T1 T2` stacks T2 after T1
(launched when T1 is accepted, on T1's accepted revision). The runtime checks plan barriers and
authorization, routes each role (`candidates.route_role` builds the request from the pinned
catalog, installed adapters, capability proofs, and a live quota probe, then calls the v3 router
unchanged), takes one fenced lease per task scope (overlapping live scopes are refused), creates
the worktree, writes the packet and brief, and queues the launch. Workers launch through
`office _supervise`, which records every ending: `success`, `nonzero`, `signal`,
`launch_failed`, `supervisor_error`. Inside Herdr (`HERDR_ENV=1`) the supervisor runs in a pane
split to the right of the caller's pane (`HERDR_PANE_ID`) in the tab the user is watching, with
further dispatches reusing an idle Office pane or stacking down in that column. Closing the run
closes only those panes. Without a caller pane, the run gets its own tab. A launch that does not
start within 45 s falls back to a plain process. Each spawned pane is appended to the run's
`panes.jsonl` in the `herdr-ledger` schema (`pane_id`, `agent`, `kind`, `session_id` when herdr
reports one, `spawned_at`, `status`, and `orchestrator_pane_id` from the orchestrator's
`HERDR_PANE_ID`, else the run's split anchor), so `herdr-ledger sweep` from the orchestrator's pane
can close finished dispatch panes.

A pane-hosted reviewer is read-only and usually cannot write its `reply.txt`. When the file is
absent or empty once the agent settles, the reply is taken from the harness's own session
transcript (Claude `projects/<cwd slug>/*.jsonl`, last assistant text; Codex
`sessions/YYYY/MM/DD/rollout-*.jsonl`, last assistant message), found by the brief path in a
user-role prompt and the dispatch cwd; the pane screen is the last resort. A pane-scraped reply that
does not parse is re-read from the transcript before the route counts as failed. The reply parser
ignores TUI bullets and box-drawing gutters (`• VERDICT: PASS`, `│ FINDING ... │`).

A worker that exits without submitting is relaunched up to `verification.environment_retry_max`
times, then its task is blocked with its worktree preserved. Findings reach a live worker on its
next `office` command (and as a Herdr nudge); a headless worker that has exited gets a fresh fix
session on the same worktree.

## 6. Submission, gates, acceptance

`office submit` (executor) captures the worktree with a temporary index — commits and
uncommitted edits — into an immutable commit under `refs/office/<run>/<task>/R<n>`. The operation
id is (task, lease, tree, applied version): a replay returns the same receipt with no new job.
Files outside the task's scope are refused before anything is recorded.

Gates per revision: `checks` (the task's commands, bound to the revision's files; artifacts the
check creates do not count; a missing command is `UNAVAILABLE`), then `code_review` and `visual`
dispatched together on the same revision. Verdicts are
`PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT | UNAVAILABLE`; a reviewer reply that does not
parse is an adapter failure, retried on a substitute route (a different model after a bad reply,
a different harness after a quota wall) up to the bound, then `UNAVAILABLE`.

`gates.evaluate_acceptance` is the only acceptance evaluator: every required gate `PASS` on the
current revision (visual evidence `COMPARABLE`), the worker's applied version current, no open
plan defect on the scope, dependencies accepted on the revision this one built on. A result for a
superseded revision is stale: audit-only, its findings carried forward. A revision with no gates
is accepted only when policy explicitly requires none (`checks: none`, a gear funding no review,
nothing user-visible). User waivers (`office approve waive T2:visual`) are recorded as named gaps.
A visual review the user had run outside Office (`office approve visual T2 --by <route> --report
<file>`) is recorded as a new visual gate on the current revision: the file must parse as a visual
review with a PASS or CHANGES_REQUIRED verdict. The user attests (the quote) that it came from a
session other than the producer's; the producer's model family is not a bar. Plan submit refuses a visual block capture could never reach (a non-local URL, or
an unreachable local URL with no `start:`) and warns when no capture backend is installed.

**Convergence.** Rounds count per (task, gate). A repeated finding fingerprint across rounds, or
the round budget, triggers the task's single escalation (a different route, told to diagnose);
still failing after it, the task pauses with the preserved revision and passing gates named.

**Integration.** When every task is accepted the runtime merges the accepted revisions onto the
run base in dependency order on `office/<run>/integration`, runs the plan's run-level `checks`, and
runs an integration review only at a real boundary (a dependency on unmerged output, a shared
file, or a declared shared interface). Conflicts and failures are surfaced; `office close` refuses
until integration is accepted and a landing is recorded (`--handoff <pr>` or the commit reachable
from the default branch). The integration worktree is recreated from git on every compose, so it
holds no installed dependencies. The repo declares its install once as `worktree.setup` in
`.auto-office/config.yaml` (pinned into the run policy at `office start`, with `setup_timeout_s` and
`applies_to`, default task, integration, and check); Office runs that command once in each new task,
integration, and check worktree and logs it to `setup.log`. Without it, run-level checks install in
the command (`pnpm install --frozen-lockfile && pnpm lint`). A check that is not found reports
UNAVAILABLE with that instruction. Office runs only the command the repo declares; it never chooses a
package manager itself. Checks must not mutate the tree: a
task check that does (`lint --fix`) makes the gate STALE.

### 6.1 User model overrides (#185)

`office dispatch T1 --as <harness>/<model>[@effort]` runs the executor on a model the user names. It
skips the candidate registry, trust, and floors, because the user's choice is the authority. It still
resolves through a matching catalog row, so `agy/gemini-3.8-flash@medium` invokes `gemini-3.8-flash-medium`.
The dispatch records `override_json` (`by: user`, `declared`, the triple, and any launch form), and
`office inspect task` shows "user override". Stacked starts and relaunches keep the override and launch
form, because both travel in the stored route.

- `--cli "<argv>"` (needs `--as`): starts exactly that argv in a herdr pane (`--kind` comes from the
  executable) through the normal landed-prompt path. Outside herdr, or if the agent does not start, the
  dispatch is left external with a notice. It never runs the adapter's own argv instead.
- `--external`: prepares the worktree, lease, brief and `agent.env`, launches nothing, and emits a
  `launch` notice with the herdr commands. Every dispatch prints brief, env, worktree and those commands.
- `--review-as <harness>/<model>[@effort]` (plus `--review-cli` / `--review-external`): pins the task's
  code reviewer. Independence is per agent, not per model family: every reviewer is a fresh dispatch and
  session, never the producer's, so it may share the executor's model. A pinned reviewer is never
  substituted by another route. An external reviewer is ended
  by a watcher once its review file is written and stops growing (no timeout; revoke ends it).
- A routed reviewer does not exclude the executor's model or family (the `family:<name>` route
  exclusion was removed); the code reviewer defaults to Luna xhigh, then Sonnet 5.5 high.

## 7. Rolling plan review, amendments, authority

Implemented as specified in [v31-rolling-review-gates.md](v31-rolling-review-gates.md): the first
PASS ends review; a first CHANGES_REQUIRED requires an amendment, after which dispatch proceeds
while the re-review runs; a PLAN_DEFECT (closed class list, cited evidence) blocks its scope and
dependants until an independent review names it `CLEARED` on a later plan version; UNAVAILABLE
blocks until a substitute answers or the user waives.

A PLAN_DEFECT is a requirement or assumption problem, not an amendment. The planner traces it to
the requirement or assumption behind it and, unless the cause is plan-only, asks the user how to
redirect it. `office amend plan --contract --redirect P<n>` (or a planner's `office submit
--redirect P<n>`) records the user's quote and the root cause (`src/office/redirect.py`). An
optional `--requirement` records r(n+1), authorized by the same quote when r(n) was authorized.
The redirect cancels queued rounds, resets the plan-review round budget and the escalation, and
sets the next reviewer. `same` resumes the defect's reviewer session when the harness and herdr
allow it, otherwise it runs a fresh session on the exact same route. `fresh`, the default, excludes
that route. The defect still clears only by `CLEARED`, and the re-review brief carries the
redirect. `office approve waive P<n> --quote` closes a defect the user judges wrong without
another review. The runtime does not gate the user exchange itself. It is guidance in `next:`, the
planner brief, and `office submit --help`.

`office amend` re-reads the run's draft `.office/plans/<run>/PLAN.md`: ordinary amendments may change decomposition, ordering,
acceptance and tests; a change to a task's scope or interfaces, a new overlapping task, a new named
action, or authority words in the delta is refused as contract-level. Contract amendments go to the
dedicated planner (affected scopes and dependants pause) or, in inline mode, apply from the edited
plan. An inline contract amendment whose draft is identical to the current plan, or does
not change the entry of a named task, is refused (`plan-not-edited` / `contract-not-edited`) with a
`next:` to edit the plan first; otherwise the version would bump while the task kept its old
contract. Deliveries are combined per task, supersede older unapplied ones, and are `queued →
delivered → applied` (`office ack`); a submission under an older applied version is "amendment
pending" (no round, no acceptance). Authorization binds requirements version + authority envelope,
survives plan bumps, and is invalidated by a requirements change or needed afresh for a new
envelope entry.

## 8. Visual evidence

Applicability is semantic: a `visual:` block in the task, or acceptance that names user-visible
behaviour (then a missing capture target is `UNAVAILABLE`, not a pass). `visual: none` declares no
visual contract. Capture uses Playwright with the local Chrome, limited to local/test origins, a
settled page and loaded fonts; viewports and states come from acceptance (default desktop and
mobile portrait). Each frame records the viewport, state, DOM probes, and screenshot; an HTML
reference is captured the same way and compared by direct DOM measurement (css-px); an image
reference goes to the reviewer as an estimate. Evidence status is separate from the verdict.
Wrong viewport/auth/state, partial capture, missing fonts, and stale references are
`INVALID_COMPARISON`: one automatic recapture, then `UNAVAILABLE`. Broken interactions, horizontal
overflow, and elements clipped outside the viewport are deterministic product failures (no judgment
spent). No reference means "fidelity unmeasured". Visual input keys hash only presentation content,
the reference bytes, the viewport/state contract and the environment, so unrelated edits reuse the
verdict and a reference change invalidates it.

Judgment uses the `visual_reviewer` role. The preference is Gemini 3.8 Flash medium via agy,
then Sonnet 5.5 high, then Luna. `model_family_floors` holds default Gemini routing to 3.7 or newer (an explicit
`--route` is exempt). A route qualifies
only after `conformance.probe_vision` — a generated image with random digits and a coloured square,
sent through that exact harness/model/effort/adapter path in a separate session — returns the right
answer in the required format; the proof is cached under a key that changes with any of those.
Code and visual reviews always run as separate dispatches; the combined-session fallback in the
visual decision is never needed by this runtime.

## 9. Prune

`office prune` reports and never writes (not even git objects). `--run <id>` restricts the dry run and `-f` to that one run and refuses one that is unknown, not finished, or already pruned. `office prune -f` re-checks each
candidate under the write lock and removes only `closed`/`abandoned` runs with no live agent, no
executing job, and no worktree content beyond its last recorded revision. Kept: the `runs` row as a
tombstone (terminal state, `office_version`, requirements/plan/policy identity and hashes, terminal
and prune timestamps, archive digest) plus the cross-run recorder tables that feed trust and
learning. Removed: worktrees, `refs/office/<run>/*`, session bindings, the run directory, and
per-run lifecycle rows. Idempotent; locked or unsafe candidates are skipped and reported.

## 10. Hooks, install, doctor

`office install` registers the current runtime, retains the 3.0 runtime, and writes
`--office-managed` hook entries for Claude Code and Gemini CLI (`session.start`, `prompt.submit`,
`tool.pre`), backing up each config first. Codex, agy and Hermes hooks are not written (their
schemas or denial paths are unverified); explicit commands cover them. The unbound hook path only
reads the environment and stats files. `tool.pre` denies a bound worker's writes outside its own
worktree on Claude and Gemini. `office doctor` checks the runtime registry, pinned runtimes for
every active run (3.1 and 3.0), hooks, the known harness defects, adapters, Playwright, vision
proofs, compatibility-call counts, and hook errors.

## 11. Compatibility window (3.1.x only)

`scripts/` is the retained 3.0 helper surface. It refuses to write a 3.1 run, refuses to create new
runs, and forwards a 3.0 state directory to that run's exact retained commit. `office raw
<subcommand>` warns, records every call in `compat_calls`, answers read-only verbs from canonical
state, runs pure helpers unchanged, and refuses state-writing verbs with their semantic
replacement. Removal in 3.2.0 deletes `scripts/`'s lifecycle helpers, the `raw` command and the 3.0
spokes once `office doctor` shows no remaining consumers.

## 12. Cutover and rollback

1. Merge with 3.1 as the default for new runs (`runtime.new_runs` defaults to `3.1`).
2. `uv tool install auto-office` (or `--editable` from the checkout), `office install`, `office doctor`.
3. Active 3.0 runs drain on their retained commits; they are never rewritten.
4. **Rollback** (future runs only): set `runtime: {new_runs: "3.0"}` in
   `~/.config/auto-office/config.yaml`. `office start` then refuses and names the retained 3.0
   runtime; every existing 3.1 run keeps working on 3.1.
5. Keep each released runtime registered while any run pinned to it is active (`office doctor`
   lists them).

## 13. Provisional values

`verification.environment_retry_max: 2` and `verification.recapture_max: 1` are the only new
bounds. The recapture bound is ratified (#158); the environment bound was left to measurement
(#160 §9) and stays provisional until the matched-fixture evaluation.

## 14. Superseded parts of the earlier CLI architecture

`office gc` → `office prune` (dry run; `-f`). `office-skills` → `auto-office`. `office spoke`,
`office events`, `office route`, and the spoke digest ritual are retired; routing is automatic in
`dispatch` with `--route` and `inspect route` for overrides and diagnosis; delivery is `ack`.
The `--adopt-version` drift policy is replaced by per-run version pinning with no in-place
adoption. Session binding keys add the Herdr pane and harness ancestor process to the explicit
`--harness/--session` pair.

## 15. Traceability

| Requirement | Code | Test |
|---|---|---|
| Duplicate submit reuses the operation | `submit.submit_revision` | `test_gates::test_duplicate_submit_reuses_the_operation` |
| Crash between transition and outside work | `jobs.reclaim/kick` | `test_state_core::test_crash_after_commit_before_external_work_recovers_once` |
| Delivered-but-unapplied amendment cannot satisfy | `amend`, `gates.evaluate_acceptance` | `test_gates::test_amendment_delivered_but_not_applied_cannot_satisfy` |
| Superseded amendment rejected | `amend.ack` | `test_gates::test_superseded_amendment_ack_is_rejected` |
| Concurrent event/cursor writers | `state.emit/advance_cursor` | `test_state_core::test_concurrent_event_writers_neither_lose_nor_duplicate` |
| Dirty edit, unchanged HEAD invalidates | `submit.capture_tree` | `test_gates::test_dirty_edit_with_unchanged_head_invalidates_prior_pass` |
| Stale result rejected | `gates.ingest_task_gate` | `test_gates::test_stale_pass_is_audit_only`, `test_visual::test_unrelated_edit_reuses_…` |
| Wrong visual state → invalid comparison | `visual._capture_one` | `test_visual::test_wrong_state_is_invalid_comparison_then_blocks_after_one_recapture` |
| Broken interaction → real failure | `visual._capture_one` | `test_visual::test_broken_interaction_is_a_product_failure_not_invalid` |
| No image capability → never visual PASS | `conformance`, `candidates` | `test_visual::test_route_without_image_capability_never_passes` |
| Reviewer schema/quota failure → fallback/block | `gates.run_reviewer` | `test_gates::test_invalid_reviewer_reply_falls_back_then_blocks`, `…quota_failure…` |
| Repeated finding → bounded stop | `gates._converge` | `test_gates::test_repeated_finding_stops_after_one_escalation` |
| Every process ending classified | `dispatch.supervise` | `test_gates::test_process_endings_always_classified` |
| Unbound session never starts | `hooks`, `discovery` | `test_discovery_pinning::test_unbound_session_never_starts_or_binds` |
| No latest-mtime selection | `discovery.resolve` | `test_discovery_pinning::test_ambiguous_runs_never_select_latest_mtime` |
| Old-version run usable after new default | `frontdoor` | `test_discovery_pinning::test_old_version_run_stays_usable_after_new_default` |
| Missing pinned runtime → blocker | `frontdoor`, `legacy` | same, and `…missing_pinned_legacy_runtime…` |
| Packet/run version mismatch rejected | `state.check_packet`, `jobs.execute` | `test_state_core::test_packet_version_mismatch_is_rejected`, `…foreign_office_version…` |
| Prune dry run mutates nothing | `prune.dry_run` | `test_prune::test_dry_run_mutates_nothing` |
| Force prune never removes resumable runs | `prune._eligibility` | `test_prune::test_force_prunes_terminal_only_and_keeps_tombstone` |
| Force prune re-checks at deletion | `prune._prune_one` | `test_prune::test_force_rechecks_eligibility_at_deletion` |
| Tombstone kept, artifacts removed | `prune._prune_one` | `test_prune::test_force_prunes_terminal_only_and_keeps_tombstone` |
| `prune --run` selects only that run | `prune.select_run` | `test_prune::test_run_flag_restricts_prune_to_that_run` |
| Rollback affects future runs only | `runtime_default` | `test_discovery_pinning::test_rollback_changes_future_runs_only` |
| Legacy/raw never a second authority | `compat`, `scripts/office_runtime.py` guard | `test_compat_hooks_install::test_raw_…`, `…legacy_helper_cannot_write_a_31_run` |
| No gate green from skipped/unavailable/stale | `gates` | `test_gates::test_missing_check_command_is_never_pass`, `test_rolling_review::test_unavailable_plan_reviewer_blocks_dispatch` |
| Rolling review S1–S3, N1–N3, N11 | `plans`, `amend` | `test_rolling_review.py` |
| Parallel tasks, shared scope, stacking, integration | `dispatch`, `integration` | `test_gates::test_parallel_…`, `…shared_scope…`, `…stacked…`, `…integration_conflict…` |

## 16. Live conformance (exercised 2026-09-27, isolated homes)

All live runs used `OFFICE_DATA_HOME`/`OFFICE_STATE_HOME` under `~/.office-live` or per-job
directories under `~/.office-eval`; no live run read or wrote the user's `runs.db`.

| Surface | Result |
|---|---|
| Vision probe, claude `claude-sonnet-5` high / `claude-opus-5` medium | pass |
| Vision probe, codex `gpt-6-astra` low / `gpt-5.6-sol` max | pass |
| Vision probe, codex `gpt-5.6-luna` xhigh | **fail** (misread the digits); never qualifies for visual judgment |
| Vision probe, Gemini CLI 0.46.0 `gemini-3.1-pro-preview` | **fail**: `IneligibleTierError` on this account |
| Vision probe, agy 1.2.12 `gemini-3.8-flash-medium` / `-low` | pass (image read through `--prompt=` with the path); now the first two `visual_reviewer` preferences |
| agy 1.2.12 model selection | an unknown `--model` slug exits 1 ("invalid model selection"); 1.2.8 silently ignored it |
| Herdr launcher | first live launch lost the supervisor (a long pane command was truncated); fixed with `launch.sh`, a 45s start confirmation and a process fallback |
| `office doctor` on the real machine | found the legacy `~/.gemini/config/hooks.json` and bare-string Hermes hooks in five profiles |
| Live run through the codex orchestrator (eval pilot F01) | planned, dispatched, reviewed, integrated and landed on local `main`; hidden acceptance tests pass |

Defects the live runs found and fixed: harness hook caches (`graft/.cache/`) written into a worktree
were refused as out-of-scope and the executor was relaunched into the same refusal (cache
directories were left out of the revision; since #222 every untracked out-of-scope file and every edit to a
tracked harness config file (`.claude/`, `.codex/`, `.agents/`, `.office/`) is left out with a warning, while
any other tracked out-of-scope edit still blocks the task with the reason); two quota probes ran serially for ~43s per dispatch (now parallel with a shared 2-minute
cache); the run phase stayed `planning` during execution.

## 17. Prospective evaluation (spec §24): status

The harness is `eval/v31/` (`run.py`, `fixtures.py`, `seed_baseline.py`, `analyze.py`): ten
fixtures, a fresh seed repository and isolated homes per job, the same repo-tier routing policy,
trust baseline, orchestrator (`codex exec`, `gpt-5.6-sol` medium, the version's own `SKILL.md` as
its only Office guidance) and scenario events for both versions, hidden acceptance tests, and a
quota guard. Herdr is disabled for both versions, because a Herdr pane does not inherit the
isolated homes; v3 then uses its documented no-Herdr (in-process) dispatch.

**Not completed.** Two calibration pilots ran (F01 and F05 on both versions). They exposed
harness defects (fixed) and exhausted the Codex 5-hour window: four jobs used ~65% of the window
and ~11% of the weekly quota, so the required matrix (at least 60 jobs) needs roughly 1.6 weeks of
Codex quota. The pilot numbers are calibration only and are not the §24 result:

| Pilot job | Orchestrator tool calls (admin) | Hand-authored JSON | Wall | Landed + hidden tests |
|---|---|---|---|---|
| F01 v3.1 | 25 (21) | 0 | 313s | yes |
| F01 v3 | 100 (73) | 12 | 1092s (ended by the credit wall after landing) | yes |
| F05 v3.1 / v3 | invalid: the amendment trigger did not fire (shim bypass, fixed) | | | |

Cost is **unmeasured**: codex runs on a subscription with no per-call price, and the v3 sessions
ended before reporting usage.
