---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, verification, independent review, and closeout. Use when explicitly invoked as /auto-office or when the user directly asks to run the Auto Office v3 lifecycle. Route each role by harness, model, and effort using pinned policy/catalog/adapter/config snapshots, hard capability and trust floors, live quota constraints, local evidence, and task shape. Preserve human merge-to-main, no-self-approval, durable handoffs, private telemetry, replay-gated policy changes, and isolated self-improvement.
---

# Auto Office v3

Treat `references/OFFICE-SKILLS-V3-SPEC.md` as normative. This file is the compact control plane.
Load only the protocol/reference needed for the current lifecycle step.

## Permanent invariants

- Keep the lifecycle order fixed. Gears may fund or omit optional stages; never reorder the lifecycle.
- Keep merge-to-`main` a boundary no agent lifts on its own initiative; only an explicit
  per-run user statement lifts it (spec 9.1).
- Every independent approval and review gate is held by an agent that did not produce the work,
  except the named section 5.1/5.2 inline waiver for small orchestrator edits (lifecycle spec §8).
  Self-verification is a pass, never an approval (lifecycle spec §8).
- Every lifecycle phase that is entered has a recorded self-review checkpoint before it advances.
  The phase owner re-reads the objective and done criteria, checks current state and evidence,
  records residual risks or decisions, and either proceeds, amends, or stops. Optional phases
  that are omitted are covered by self-review of the omission decision; self-review never
  substitutes for an independent gate that the run's risk, gear, or policy requires
  (lifecycle spec §8.0).
- Pin `plugin_commit`, `policy_hash`, `catalog_snapshot_hash`, `adapter_snapshot_hash`, and `effective_config_hash` for each run.
- Never let an active run begin using an unmerged self-improvement policy implicitly.
- Keep raw run evidence private. Public proposals receive only deterministic sanitization, an evidence capsule, privacy lint, and opaque evidence hashes.
- Treat unknown mandatory adapter semantics, missing packet fields, stale ownership, plan/packet version mismatch, privacy-lint failure, protected-path violation, and destructive actions without authority as hard stops.
- Treat stale catalog refresh, missing optional public data, and non-critical telemetry failure as fail-soft conditions that are surfaced and recorded.
- Do not use the system `/tmp` directory. Prefer the runs directory with a `tmp` subfolder (`<state_dir>/tmp` or `<runs_dir>/tmp`) for temporary files, scratch artifacts, and worker buffers.

## Spoke receipts

Stage lines below name a spoke only; this protocol applies to each. Before doing that stage's work, run `check-spoke --state-dir <run-state-dir> --spoke <name>`; if it exits nonzero, load `skills/<name>/SKILL.md` via the Skill tool and record `mark-spoke --state-dir <run-state-dir> --spoke <name> --digest <value from spoke-digest --spoke <name>>`. The receipt is `check-spoke`'s exit code, not a memory of having loaded the spoke.

`mark-spoke` requires the digest because a receipt you can mint by typing a spoke's name is the gated agent attesting to its own compliance. The digest does not prove you understood the spoke — nothing here can — it proves you located that file at its current version, which is what makes batch-marking spokes you never opened a deliberate act rather than a convenience. **Mark one spoke per invocation, immediately after loading it**, never several in one shell call. `--unverified` exists for a spoke genuinely absent from disk and is reported at closeout.

## Start or resume a run

Step zero, always, before any other action: run

`python3 scripts/office_runtime.py start --goal <text> --playbook <Change|Restructure|Investigate|Prototype|Visual> [--gear <direct|direct+review|light|quick|express|full>] [--repo <path>] [--blast-radius <local|repo|production|production-data>] [--size-class <S|M|L|XL>]`

Echo the returned `kickoff` block to the user, then use its `state_dir` for every later `check-spoke`/`mark-spoke --state-dir` call. Why: `references/why-start.md`.

`start` resolves effective config (`prompt/CLI > repo > user > plugin default`, emitting `effective_config_hash`; check `~/.config/auto-office/config.yaml` directly), pins the catalog/adapter snapshot hashes, policy hash, and base SHA, runs the gear fit test when `--gear` is omitted, creates `state_dir` with a `tmp/` subfolder (avoiding `/tmp`), and creates durable run state with `phase = "intake"`. Why: `references/why-start.md`. Pass `--blast-radius`/`--size-class` from whatever provisional intent is in hand (a best guess, unset is never low risk); `production`/`production-data` or `L`/`XL` escalates the fit test into `express` and forces `direct`'s `risk_forced` review gates even under an explicit `--gear` pin — re-run `resolve-gates --state-dir <dir>` once the planner freezes the real `blast_radius` so a plan riskier than the kickoff guess gets its gates recomputed before packets ship.

After `start` returns, capture only provisional intent from the user's request — do not interview or freeze anything yet. Before any repository reconnaissance, interview, or planning-spoke check, automatically load the `file-issue` skill and create or reuse exactly one tracking GitHub issue from that raw request. Do not ask for a draft or routine approval: run the skill's duplicate searches, file immediately when the repository and GitHub access are available, and stop before planning if an external blocker prevents safe filing. Record the issue number or URL on the family registry with `python3 scripts/office_runtime.py family-update --family-id <family_id> --state-dir <state_dir> --issue <number-or-url>`.

Only after the tracking issue is recorded, take the `auto-planning` receipt. The planner interacts directly with the user, runs the twelve-item interview from `skills/auto-intake/SKILL.md` (`check-spoke`/`mark-spoke --spoke auto-intake`), updates the tracking issue body with the approved plan summary or plan link, and freezes the five execution fields — `goal`, `done_criteria`, `blast_radius`, `named_actions`, `non_goals` — at the end of that discovery, not before it. Why: `references/why-start.md`.

After the plan is approved, take the `auto-loop` receipt before driving waves, integration, and the autonomy ceiling.

For takeover/resume, load `protocol/state-and-takeover.md` before any mutable action. For concurrent families, sticky focus, or an amendment mid-run, load `protocol/families-and-amendments.md`.

## Fixed lifecycle

The order is fixed and lives in one place: `protocol/lifecycle.md`. Gears may fund or omit optional stages; nothing reorders it.

## Role routing

Before every routed role, take the `auto-routing` receipt. Route the exact identity:

`harness@version × model_id × effort`

The mandatory filter order lives in `protocol/routing.md`. Hold one invariant without loading it: adapter trust, the absolute floor, and tie-break evidence are derived from recorded evidence and never caller-supplied — trust only ever falls automatically, and only a recorded, attributed act raises it or overrides a derived gate.

A harness rejecting the routed model/effort identity is a routing defect, not a retry: record it with `office_runtime.py route-defect`, re-dispatch corrected, and hand the amendment to an `auto-self-improve` subagent. `auto-closeout` gates on `check-route-defects`, so an unamended slug blocks completion. Never lower an absolute floor for cost or quota. Why: `references/why-routing.md`.

Every plan must contain a `model_assignments` block naming the orchestrator and planner identities: exact invocation model identifier (when exposed), canonical `model_id`, effort, harness/version, whether the planner is inline or separately routed, and a concise selection rationale. If the current entry model owns both roles, declare both explicitly.

Immediately before invoking an executor or reviewer, publish a route notice naming the role, exact invocation model identifier (or canonical `model_id`), effort, harness/version, and the decisive routing evidence (task-shape fit, capability/trust floor, preferred seed, quota, cost, or comparable local results). Persist the same disclosure in the dispatch envelope/readback. Why: `references/why-routing.md`.

## Dispatch and mutation

Take the `auto-execution` receipt. Validate every packet before dispatch. One mutable holder owns a write scope at a time; a holder change is a takeover requiring lease acquisition and stale-state reconciliation. Use the harness primitive selected by the adapter (`skills/codex-cli`, `skills/claude-cli`, `skills/agy-cli`, `skills/hermes-cli`, or another conforming primitive): `auto-office` owns the lifecycle, adapters own harness execution only. Why: `references/why-dispatch.md`.

Every dispatch — including a run's only dispatch — gets its own git worktree cut from the run's pinned base SHA; never dispatch an executor into the repo's main checkout. This holds regardless of executor count or gear: a single-executor run is not an exception, because the orchestrator never knows who else — another human, another agent, another session — may be concurrently working in that same checkout. The executor commits its own work locally to its own dispatch branch (never pushes) as a checkpoint before reporting done, so its work survives an orchestrator mistake during later verification or integration; see `skills/auto-loop/SKILL.md` for the full dispatch/integration lifecycle this feeds into.

Before the first executor or reviewer dispatch, check `echo "$HERDR_ENV"` and `which herdr`. When `HERDR_ENV=1` and `herdr` is reachable, dispatch through a Herdr pane (`skills/herdr`) rather than an in-process subagent tool, even when the in-process tool is available and would produce a working result: a visible pane is how the user watches, steers, and interrupts delegated work, and that visibility is the reason this precondition exists, not a preference to weigh against convenience. The failure mode is defaulting to whatever dispatch tool is already loaded without ever checking. Fall back to an in-process dispatch only when `HERDR_ENV` is genuinely unset or `herdr` is unreachable, and say so explicitly when routing. This applies to every dispatch mechanism, not just in-process subagent tools: a backgrounded bare CLI launch is equally invisible, and a launch wrapped in `env -i` strips `HERDR_ENV` and `herdr` from the child's `PATH`, so it cannot be caught by a hook keyed on that variable and will not appear in `herdr agent list` or the pane ledger. Check `HERDR_ENV` in the parent before dispatch, and carry it into any sanitised child environment.

## Operational tooling

- `scripts/office_spawn.sh` — adapter-driven agent spawner supporting stdin/flag/file prompt transport, process logging, and PID management.
- `scripts/office_liveness.sh` — process liveness, silence timeout, and output stream health monitoring.
- `scripts/office_readback.sh` — completion readback, exit code classification, and adapter failure signature attribution.
- `scripts/office_worktree.sh` — git worktree creation, diff snapshots, and cleanup for dispatches.
- `scripts/verify.sh` — ordered verification gate runner (lint, typecheck, test, runtime).
- `scripts/review_loop.sh` — multi-round verify/review/fix orchestrator enforcing no-self-approval and defect exits.
- `scripts/review_finding.sh` — structured review finding recording and telemetry persistence.
- `scripts/hooks/` — lifecycle hooks (`session_end.sh`, `pre_compact.sh`, `compact_advisor.sh`, `close_panes.sh`, `close_finished_panes.mjs`, `install_hooks.sh`) ensuring runs remain durable across interruptions and context compaction.
- `scripts/agy-usage.py`, `scripts/claude-usage.py`, `scripts/codex-usage.py` — live per-brand quota probes (stdlib/OAuth reads against each vendor's usage API); each adapter's `quota_probe.command` names its probe. See `references/quota-probe.md`.
- `scripts/office_shortcut.sh` — per-repo shorthand for `office_runtime.py --state-dir` calls (`check-spoke`, `mark-spoke`, raw passthrough). Copy it into a consuming repo at `<repo>/.office/bin/office` to resolve the current run's state-dir automatically instead of retyping `python3 .../office_runtime.py <subcommand> --state-dir <path>` for every call.

## Review and verification

- `auto-review` — plan/code gates and defect exits.
- `auto-verification` — targeted tests, known-bad validation, browser/runtime acceptance flows, evidence quality.
- Require a recorded self-review checkpoint for every entered lifecycle phase; a missing or
  unverifiable checkpoint blocks advancement.
- Require self-verification for every mutable run.
- Require independent verification/review when risk, gear, playbook, repository policy, or user-facing acceptance requires it.

## Defect exits

An accepted `PLAN DEFECT` or `BRIEF DEFECT` must name the contradicted assumption and show evidence. Pause affected execution, increment the owning artifact version, invalidate stale packets, amend through the proper owner, and resume only from the new version.

## Closeout and learning

Reorganize the dispatch surface on every closeout: run `node scripts/hooks/close_finished_panes.mjs < /dev/null` to close finished Herdr panes from the spawn ledger, then account for whatever pane remains open. Auto-delete used worktrees created for the run after merging (`python3 scripts/office_runtime.py cleanup-worktrees --state-dir <state_dir>`). Take the `auto-closeout` receipt. If a tracking issue exists, include `Closes #N` in the PR body when the work is complete; leave the issue open when the run stops short or remains unresolved. Do not report implementation complete until the required outcome, validation, review, runtime/browser evidence, PR/branch state, blockers, and pinned hashes exist. Why: `references/why-closeout.md`.

At the beginning/closeout of later invocations, take the `auto-maintenance` receipt for lazy labeling, maturity, catalog freshness, and structured local evidence. Create public self-improvement proposals only through the `auto-self-improve` spoke, and only in an isolated branch/worktree. Why: `references/why-closeout.md`.

## Deterministic helpers

Use `python3 scripts/office_runtime.py --help` for packet validation, adapter validation/scaffolding, route selection, snapshot hashing, SQLite recorder initialization, maturity calculation, replay comparison, privacy linting, catalog snapshot creation, proposal identity hashing, and `resolve-gates` (re-derive `plan_review`/`independent_code_review` funding and round caps from a run's current gear plus risk inputs, without re-running `start`).

Use `python3 scripts/check_ecosystem.py` before packaging or proposing plugin changes.

## Reference map

- `protocol/lifecycle.md` — lifecycle and gear semantics.
- `protocol/roles-and-authority.md` — authority, no-self-approval, frozen intent.
- `protocol/state-and-takeover.md` — durable state, leases, resume/takeover.
- `protocol/routing.md` — route identity, filters, quota, cost, exploration.
- `protocol/families-and-amendments.md` — family registry, sticky focus, amendment ownership, conditional compaction.
- `protocol/adapters.md` — adapter contract and trust states.
- `protocol/verification-review.md` — verification floor, findings, defect exits.
- `protocol/telemetry-learning.md` — recorder, attribution, outcomes, rewards, maturity, replay.
- `protocol/privacy-self-improvement.md` — sanitizer, dream compilation, proposal isolation and PR lineage.
- `references/IMPLEMENTATION-NOTES.md` — what this preview implements vs. intentionally leaves provider-specific.
- `references/MIGRATION-V2.md` — branded-office retirement/migration.
- `references/quota-probe.md` — live quota probe contract: fit-test/pre-dispatch checkpoints, per-brand fields, exit codes.
- `references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md` — intake interview, waves, non-blocking orchestration, integration, autonomy ceiling.
- `references/why-start.md`, `references/why-routing.md`, `references/why-dispatch.md`, `references/why-closeout.md` — why the pinned rules exist, for disputing a rule; not needed to follow it.
