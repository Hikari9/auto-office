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

1. Resolve user intent and scope.
2. Load effective config using `prompt/CLI > repo > user > plugin default` precedence.
3. Run lazy maintenance for eligible historical rows when practical.
4. Resolve the current immutable catalog and adapter snapshots; refresh may run separately, but route-time itself must not use the network.
5. Establish repository/runtime baseline and classify task shape, risk, blast radius, and size class.
6. Create or reconcile durable family/run state before dispatch.
7. Freeze the orchestrator-owned intent fields. The v3 spec references five frozen fields without naming them; this preview keeps the existing office compatibility seed: `goal`, `done_criteria`, `blast_radius`, `named_actions`, `non_goals`. Treat this mapping as compatibility data, not a license to change the normative spec.
8. Load `skills/auto-planning/SKILL.md` for planning and product-decision resolution.

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
11. Run independent review when funded/required.
12. Run browser/runtime verification for user-facing acceptance paths when reachable.
13. Reconcile findings and amendments.
14. Run closeout checks.
15. Record telemetry and durable state.
16. Perform lazy maintenance on eligible rows.
17. Optionally create isolated improvement proposals.

## Role routing

Load `skills/auto-routing/SKILL.md` before every routed role. Route the exact identity:

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

Never lower an absolute floor for cost or quota. Public benchmark data is a cold-start prior; enough comparable local evidence for the exact routable triple outranks it.

## Dispatch and mutation

Load `skills/auto-execution/SKILL.md`. Validate every packet before dispatch. One mutable holder owns a write scope at a time. A holder change is a takeover requiring lease acquisition and stale-state reconciliation.

Use the harness primitive selected by the adapter (`skills/codex-cli`, `skills/claude-cli`, `skills/agy-cli`, `skills/hermes-cli`, or another conforming primitive). `auto-office` owns the lifecycle; adapters own harness execution. Runs are durable across interruptions via SQLite-backed leases and atomic state snapshots. Harness primitives are mechanics only; they never redefine lifecycle authority.

## Operational tooling

- `scripts/office_spawn.sh` — adapter-driven agent spawner supporting stdin/flag/file prompt transport, process logging, and PID management.
- `scripts/office_liveness.sh` — process liveness, silence timeout, and output stream health monitoring.
- `scripts/office_readback.sh` — completion readback, exit code classification, and adapter failure signature attribution.
- `scripts/office_worktree.sh` — git worktree creation, diff snapshots, and cleanup for dispatches.
- `scripts/verify.sh` — ordered verification gate runner (lint, typecheck, test, runtime).
- `scripts/review_loop.sh` — multi-round verify/review/fix orchestrator enforcing no-self-approval and defect exits.
- `scripts/review_finding.sh` — structured review finding recording and telemetry persistence.
- `scripts/hooks/` — lifecycle hooks (`session_end.sh`, `pre_compact.sh`, `compact_advisor.sh`, `close_panes.sh`, `install_hooks.sh`) ensuring runs remain durable across interruptions and context compaction.
- `scripts/agy-usage.py`, `scripts/claude-usage.py`, `scripts/codex-usage.py` — live per-brand quota probes (stdlib/OAuth reads against each vendor's usage API); each adapter's `quota_probe.command` names its probe. See `references/quota-probe.md`.

## Review and verification

- Load `skills/auto-review/SKILL.md` for plan/code gates and defect exits.
- Load `skills/auto-verification/SKILL.md` for targeted tests, known-bad validation, browser/runtime acceptance flows, and evidence quality.
- Require self-verification for every mutable run.
- Require independent verification/review when risk, gear, playbook, repository policy, or user-facing acceptance requires it.

## Defect exits

An accepted `PLAN DEFECT` or `BRIEF DEFECT` must name the contradicted assumption and show evidence. Pause affected execution, increment the owning artifact version, invalidate stale packets, amend through the proper owner, and resume only from the new version.

## Closeout and learning

Load `skills/auto-closeout/SKILL.md`. Do not report implementation complete until the required outcome, validation, review, runtime/browser evidence, PR/branch state, blockers, and pinned hashes exist.

At the beginning/closeout of later invocations, load `skills/auto-maintenance/SKILL.md` for lazy labeling, maturity, catalog freshness, and structured local evidence.

Create public self-improvement proposals only through `skills/auto-self-improve/SKILL.md` and only in an isolated branch/worktree. Agents may propose and prove; the maintainer decides what becomes shipped policy.

## Deterministic helpers

Use `python3 scripts/office_runtime.py --help` for packet validation, adapter validation/scaffolding, route selection, snapshot hashing, SQLite recorder initialization, maturity calculation, replay comparison, privacy linting, catalog snapshot creation, and proposal identity hashing.

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
