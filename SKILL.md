---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, verification, independent review, and closeout. Use when explicitly invoked as /auto-office or when the user directly asks to run the Auto Office v3 lifecycle. Route each role by harness, model, and effort using pinned policy/catalog/adapter/config snapshots, hard capability and trust floors, live quota constraints, local evidence, and task shape. Preserve human merge-to-main, no-self-approval, durable handoffs, private telemetry, replay-gated policy changes, and isolated self-improvement.
---

# Auto Office v3

Treat `references/OFFICE-SKILLS-V3-SPEC.md` as normative. This file is the compact control plane.
Load only the protocol/reference needed for the current lifecycle step.

## Permanent invariants

- Keep the lifecycle order fixed. Gears may fund or omit optional stages; never reorder the lifecycle.
- Keep human merge-to-`main` as the permanent authority boundary.
- Never let a producer approve its own work.
- Pin `plugin_commit`, `policy_hash`, `catalog_snapshot_hash`, `adapter_snapshot_hash`, and `effective_config_hash` for each run.
- Never let an active run begin using an unmerged self-improvement policy implicitly.
- Keep raw run evidence private. Public proposals receive only deterministic sanitization, an evidence capsule, privacy lint, and opaque evidence hashes.
- Treat unknown mandatory adapter semantics, missing packet fields, stale ownership, plan/packet version mismatch, privacy-lint failure, protected-path violation, and destructive actions without authority as hard stops.
- Treat stale catalog refresh, missing optional public data, and non-critical telemetry failure as fail-soft conditions that are surfaced and recorded.

## Start or resume a run

Step zero, always, before any other action: run

`python3 scripts/office_runtime.py start --goal <text> --playbook <Change|Restructure|Investigate|Prototype|Visual> [--gear <direct|direct+review|light|quick|express|full>] [--repo <path>]`

Echo the returned `kickoff` block to the user, then use its `state_dir` for every later `check-spoke`/`mark-spoke --state-dir` call. Doing the work inline instead of running this command is the known failure mode this step exists to close — there is no other valid entry point.

`start` mechanically resolves effective config (merging `prompt/CLI > repo > user > plugin default`, emitting `effective_config_hash`; the user tier at `~/.config/auto-office/config.yaml` lives outside the repo, is invisible to repo search, and routinely sets `preferred_seed` chains the plugin default lacks — never hand-read `config/config.default.yaml` and treat it as the answer), pins the catalog/adapter snapshot hashes, policy hash, and base SHA, runs the gear fit test when `--gear` is omitted, and creates durable run state with `phase = "intake"`.

Before freezing intent, run `check-spoke --state-dir <state_dir> --spoke auto-intake`; if it exits nonzero, load `skills/auto-intake/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-intake` before interviewing the user. The orchestrator owns this interview; the planner never talks to the user.

Still manual, after `start` returns: freeze the five orchestrator-owned intent fields as compatibility data, not license to change the normative spec — `goal`, `done_criteria`, `blast_radius`, `named_actions`, `non_goals` — then run `check-spoke --state-dir <state_dir> --spoke auto-planning`; if it exits nonzero, load `skills/auto-planning/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-planning` before any planning work.

After the plan is approved, run `check-spoke --state-dir <state_dir> --spoke auto-loop`; if it exits nonzero, load `skills/auto-loop/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-loop` before driving waves, integration, and the autonomy ceiling.

For takeover/resume, load `protocol/state-and-takeover.md` before any mutable action.

## Fixed lifecycle

Run this order:

1. Resolve intent and scope.
2. Freeze orchestrator contract.
3. Establish baseline.
4. Classify task shape and risk.
5. Resolve product decisions.
6. Produce/refresh plan.
7. Review plan when gear/risk requires it.
8. Generate machine-checkable execution packets.
9. Route and dispatch executors/workers.
10. Self-verify changed work.
11. Integrate: commit and merge dispatch branches in wave order, validate the merged result.
12. Run independent review when funded/required.
13. Run browser/runtime verification for user-facing acceptance paths when reachable.
14. Reconcile findings and amendments.
15. Run closeout checks.
16. Record telemetry and durable state.
17. Perform lazy maintenance on eligible rows.
18. Optionally create isolated improvement proposals.

## Role routing

Before every routed role, run `check-spoke --spoke auto-routing`; if not loaded, load `skills/auto-routing/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-routing`. Route the exact identity:

`harness@version × model_id × effort`

Use the mandatory filter order:

1. explicit hard exclusions;
2. adapter validity/trust;
3. required capabilities;
4. absolute role floor;
5. task-shape requirements;
6. quota safety;
7. preferred/advisory quality anchor;
8. cost;
9. local tie-break evidence.

A harness rejecting the routed model/effort identity is a routing defect, not a retry: record it with `office_runtime.py route-defect`, re-dispatch corrected, and hand the amendment to an `auto-self-improve` subagent. `auto-closeout` gates on `check-route-defects`, so an unamended slug blocks completion.

Never lower an absolute floor for cost or quota. Public benchmark data is a cold-start prior; enough comparable local evidence for the exact routable triple outranks it.

Every plan must contain a `model_assignments` block naming the orchestrator and planner identities. Record the exact invocation model identifier when the harness exposes one, plus canonical `model_id`, effort, harness/version, whether the planner is inline or separately routed, and a concise selection rationale. If the current entry model owns both roles, declare both explicitly rather than implying the planner identity.

Immediately before invoking an executor or reviewer, publish a concise route notice to the user naming the role, exact invocation model identifier (falling back to canonical `model_id` only when no more specific identifier exists), effort, harness/version, and why that route won. Base the explanation on the actual decisive routing evidence—such as task-shape fit, capability/trust floor, preferred seed, quota, cost, or comparable local results—not a generic statement about model quality. Persist the same disclosure in the dispatch envelope/readback.

## Dispatch and mutation

Run `check-spoke --spoke auto-execution`; if not loaded, load `skills/auto-execution/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-execution`. Validate every packet before dispatch. One mutable holder owns a write scope at a time. A holder change is a takeover requiring lease acquisition and stale-state reconciliation.

Use the harness primitive selected by the adapter (`skills/codex-cli`, `skills/claude-cli`, `skills/agy-cli`, `skills/hermes-cli`, or another conforming primitive). `auto-office` owns the lifecycle; adapters own harness execution. Runs are durable across interruptions via SQLite-backed leases and atomic state snapshots. Harness primitives are mechanics only; they never redefine lifecycle authority.

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

## Review and verification

- Run `check-spoke --spoke auto-review`; if not loaded, load `skills/auto-review/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-review`, for plan/code gates and defect exits.
- Run `check-spoke --spoke auto-verification`; if not loaded, load `skills/auto-verification/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-verification`, for targeted tests, known-bad validation, browser/runtime acceptance flows, and evidence quality.
- Require self-verification for every mutable run.
- Require independent verification/review when risk, gear, playbook, repository policy, or user-facing acceptance requires it.

## Defect exits

An accepted `PLAN DEFECT` or `BRIEF DEFECT` must name the contradicted assumption and show evidence. Pause affected execution, increment the owning artifact version, invalidate stale packets, amend through the proper owner, and resume only from the new version.

## Closeout and learning

Reorganize the dispatch surface on every closeout, not only the last: run `node scripts/hooks/close_finished_panes.mjs < /dev/null` to close finished Herdr panes from the spawn ledger, then account for whatever pane remains open. Run `check-spoke --spoke auto-closeout`; if not loaded, load `skills/auto-closeout/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-closeout`. Do not report implementation complete until the required outcome, validation, review, runtime/browser evidence, PR/branch state, blockers, and pinned hashes exist.

At the beginning/closeout of later invocations, run `check-spoke --spoke auto-maintenance`; if not loaded, load `skills/auto-maintenance/SKILL.md` via the Skill tool and `mark-spoke --spoke auto-maintenance`, for lazy labeling, maturity, catalog freshness, and structured local evidence.

Create public self-improvement proposals only through `skills/auto-self-improve/SKILL.md` (run `check-spoke`/`mark-spoke --spoke auto-self-improve` the same way) and only in an isolated branch/worktree. Agents may propose and prove; the maintainer decides what becomes shipped policy.

## Deterministic helpers

Use `python3 scripts/office_runtime.py --help` for packet validation, adapter validation/scaffolding, route selection, snapshot hashing, SQLite recorder initialization, maturity calculation, replay comparison, privacy linting, catalog snapshot creation, and proposal identity hashing.

Spoke-load compliance is checkable, not assumed: `check-spoke --state-dir <run-state-dir> --spoke <name>` (all `check-spoke`/`mark-spoke` references above elide the shared `--state-dir <run-state-dir>` for brevity) exits 0 only if `mark-spoke --spoke <name>` was already recorded for this run's `state.json`. Do not treat "I loaded it earlier in the conversation" as sufficient — if `check-spoke` exits nonzero, load the spoke via the Skill tool and mark it before doing that stage's work, even if this feels redundant.

Use `python3 scripts/check_ecosystem.py` before packaging or proposing plugin changes.

## Reference map

- `protocol/lifecycle.md` — lifecycle and gear semantics.
- `protocol/roles-and-authority.md` — authority, no-self-approval, frozen intent.
- `protocol/state-and-takeover.md` — durable state, leases, resume/takeover.
- `protocol/routing.md` — route identity, filters, quota, cost, exploration.
- `protocol/adapters.md` — adapter contract and trust states.
- `protocol/verification-review.md` — verification floor, findings, defect exits.
- `protocol/telemetry-learning.md` — recorder, attribution, outcomes, rewards, maturity, replay.
- `protocol/privacy-self-improvement.md` — sanitizer, dream compilation, proposal isolation and PR lineage.
- `references/IMPLEMENTATION-NOTES.md` — what this preview implements vs. intentionally leaves provider-specific.
- `references/MIGRATION-V2.md` — branded-office retirement/migration.
- `references/quota-probe.md` — live quota probe contract: fit-test/pre-dispatch checkpoints, per-brand fields, exit codes.
- `references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md` — intake interview, waves, non-blocking orchestration, integration, autonomy ceiling.
