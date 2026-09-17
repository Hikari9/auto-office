---
name: auto-execution
description: Internal Auto Office v3 execution spoke. Use after an accepted plan to validate execution packets, acquire mutable role/write-scope ownership, dispatch executors or workers through the selected harness adapter, enforce protected paths and blast-radius limits, handle takeover/version checks, self-review mutations, and return durable evidence without widening scope.
---

# Auto Execution

Before dispatch, validate both the run envelope and execution packet. Reject missing/contradictory mandatory fields. Require the router's `selection_disclosure`, publish it to the user before launching the executor/worker, and preserve it in the dispatch record and readback.

A packet must include base SHA, task scope, observable outcome, blast radius, allowed mutations, protected paths, validation commands, known-bad behavior to exclude, self-review, and rollback/restore notes.

Enter dispatch from `state.json.phase == "approved"`; `intake`, `planned`, or anything else is a hard stop, not a retry. `--quote` is an audit record of what was approved, not a credential; the orchestrator may approve on the user's behalf toward subagents, so a quote it wrote is legitimate. A producer approving its own work is not. Never fabricate or backfill the `approval` block — its value is the record, and a record of something that did not happen is worth less than none.

**The brief's delivery channel must be reachable under the dispatch's sandbox.** A read-only dispatch cannot deliver its result as a file. This is not a hypothetical: a reviewer dispatched read-only was asked to write its findings to a path, reviewed correctly for sixteen minutes at high effort, then spent further turns trying a text editor, a terminal, an IDE and a browser as write fallbacks before reporting it could not deliver. The findings survived only because the orchestrator read the pane. State the delivery channel in the packet (`output.delivery`) and let `validate-packet` reject the contradiction before the reasoning budget is spent, not after.

Acquire the role lease/write scope: one mutable holder per scope. Treat takeover as a holder change requiring stale-state reconciliation.

Use the selected harness primitive; a primitive never redefines lifecycle authority. The producer self-reviews and self-verifies, but that is a pass, never an approval (lifecycle spec §8) — independent approval remains held by an agent that did not produce the work.

If evidence contradicts the plan/brief assumption and continuing would violate outcome/safety, raise a supported `PLAN DEFECT` or `BRIEF DEFECT` instead of improvising a requirement change.
