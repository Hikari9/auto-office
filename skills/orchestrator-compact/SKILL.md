---
name: orchestrator-compact
description: Respond to an Auto Office `ctx-high:` status notice by checking whether the orchestrator should compact.
---

# Orchestrator Compact

Use this at a natural boundary after `office status` reports `ctx-high:`. The notice appears once per high-context episode and rearms only after context falls below the configured threshold.

1. Run `/compactable` and follow its verdict.
2. If it says `NOT YET`, keep working and check again at the next natural boundary.
3. If it says `COMPACT NOW`, compact the session, verify the compacted state, and continue the active task.

Never compact without the `COMPACT NOW` verdict. Do not interrupt an active operation or a pending user decision to compact.
