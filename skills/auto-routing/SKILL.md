---
name: auto-routing
description: Internal Auto Office v3 router. Use when selecting or explaining a role route across harness, model, and effort; evaluating adapter trust/capabilities/absolute floors/task shape/quota/advisory quality/cost/local evidence; deciding exploration eligibility; or recording a routing decision. Route-time must remain offline and reproducible from pinned snapshots.
---

# Auto Routing

Route `harness@version × model_id × effort`, not model brand alone.

Read `../../protocol/routing.md`, `../../config/config.default.yaml`, the pinned catalog snapshot, pinned adapter snapshot, effective config, and comparable local evidence.

Apply the exact filter order: hard exclusions → adapter trust → capabilities → absolute floor → task shape → quota safety → advisory anchor → cost → local tie-break.

Never lower an absolute floor to save money/quota. Unknown quota is not unlimited. If every qualifying candidate crosses protected reserve, return control to the orchestrator instead of quietly spending it.

Probe CLI headroom before it feeds routing — quota reads are network calls and must happen outside `office_runtime.py route`, which stays offline. Each adapter's `quota_probe.command` names the live probe (`scripts/agy-usage.py`, `scripts/claude-usage.py`, `scripts/codex-usage.py`); run the brand's probe at fit-test and again immediately before that brand's dispatch, never reuse a fit-test reading for a later dispatch. Exit `2` means unknown, not low — treat it as no known-safe alternative. Full contract in `../../references/quota-probe.md`.

Public benchmark data is cold-start evidence. Enough comparable local evidence for the exact routable triple outranks generic priors. Keep runtime reliability evidence harness-specific even when two harnesses expose the same underlying model.

Use `python3 ../../scripts/office_runtime.py route <request.yaml>` for deterministic selection. Record the request/decision hashes with the run.

Treat the route result's `selection_disclosure` as required handoff data. Before any executor, plan-reviewer, or code-reviewer invocation, show the user one concise notice containing role, exact invocation model identifier (or canonical `model_id` fallback), effort, harness/version, and `reason`. Carry that same object into the dispatch envelope/readback. The reason must identify the actual decisive filters or tie-breaks; “best model” is not sufficient.

When the resolved config sets `roles.<role>.preferred_seed`, pass it through in the request as `preferred_seed` (same ordered list of `{model_id, effort, harness?}`). `route` uses it at the advisory-anchor stage: candidates matching an entry pass; if none match, the anchor imposes no restriction. Among passing candidates it then ranks by chain position first (first entry wins if it clears every earlier stage) and cost only as a tie-break within the same rank — so an explicit preference chain overrides `cost_policy`'s money-band elimination for its own entries.
