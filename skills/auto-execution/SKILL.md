---
name: auto-execution
description: Internal Auto Office v3 execution spoke. Use after an accepted plan to validate execution packets, acquire mutable role/write-scope ownership, dispatch executors or workers through the selected harness adapter, enforce protected paths and blast-radius limits, handle takeover/version checks, self-review mutations, and return durable evidence without widening scope.
---

# Auto Execution

Before dispatch, validate both the run envelope and execution packet. Reject missing/contradictory mandatory fields. Require the router's `selection_disclosure`, publish it to the user before launching the executor/worker, and preserve it in the dispatch record and readback.

A packet must include base SHA, task scope, observable outcome, blast radius, allowed mutations, protected paths, validation commands, known-bad behavior to exclude, self-review, and rollback/restore notes.

Enter dispatch from `state.json.phase == "approved"`; `intake`, `planned`, or anything else is a hard stop, not a retry. A producer running `approve-plan` on its own behalf is a protocol violation. `--quote` carries the user's verbatim words as an audit record of what they approved; nothing authenticates it, so an agent that invents one produces a state that reads `approved` and is not (#94). The invariant therefore rests on the producer, not on a check — treat the `approval` block as authoritative and never fabricate or backfill it.

Acquire the role lease/write scope: one mutable holder per scope. Treat takeover as a holder change requiring stale-state reconciliation.

Use the selected harness primitive; a primitive never redefines lifecycle authority. The producer self-reviews and self-verifies, but that is a pass, never an approval (lifecycle spec §8) — independent approval remains held by an agent that did not produce the work.

If evidence contradicts the plan/brief assumption and continuing would violate outcome/safety, raise a supported `PLAN DEFECT` or `BRIEF DEFECT` instead of improvising a requirement change.
