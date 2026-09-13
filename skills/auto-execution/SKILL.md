---
name: auto-execution
description: Internal Auto Office v3 execution spoke. Use after an accepted plan to validate execution packets, acquire mutable role/write-scope ownership, dispatch executors or workers through the selected harness adapter, enforce protected paths and blast-radius limits, handle takeover/version checks, self-review mutations, and return durable evidence without widening scope.
---

# Auto Execution

Before dispatch, validate both the run envelope and execution packet. Reject missing/contradictory mandatory fields. Require the router's `selection_disclosure`, publish it to the user before launching the executor/worker, and preserve it in the dispatch record and readback.

A packet must include base SHA, task scope, observable outcome, blast radius, allowed mutations, protected paths, validation commands, known-bad behavior to exclude, self-review, and rollback/restore notes.

Acquire the role lease/write scope. Never allow two independent mutable holders for the same scope. Treat takeover as a holder change requiring stale-state reconciliation.

Use the selected harness primitive; do not let a primitive redefine lifecycle authority. The producer self-reviews and self-verifies, but self-review is never independent approval.

If evidence contradicts the plan/brief assumption and continuing would violate outcome/safety, raise a supported `PLAN DEFECT` or `BRIEF DEFECT` instead of improvising a requirement change.
