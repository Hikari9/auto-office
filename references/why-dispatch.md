# Why: dispatch and mutation rationale

Rationale for pinned rules in the "Dispatch and mutation" section of `SKILL.md`.

## Why one mutable holder at a time

Runs are durable across interruptions via SQLite-backed leases and atomic state snapshots. That
durability guarantee only holds if exactly one holder can mutate a write scope at any moment —
two concurrent holders racing to snapshot state is how a durable run becomes a corrupted one. A
holder change is therefore modeled as a takeover: it requires lease acquisition and stale-state
reconciliation, not a bare write.

## Why harness primitives never redefine lifecycle authority

`auto-office` owns the lifecycle; adapters (`skills/codex-cli`, `skills/claude-cli`,
`skills/agy-cli`, `skills/hermes-cli`, or another conforming primitive) own harness execution
mechanics only. Letting a harness primitive make lifecycle decisions would mean the lifecycle
behaves differently per harness, which defeats the point of having one pinned lifecycle at all.
