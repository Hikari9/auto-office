---
name: auto-self-improve
description: Internal Auto Office v3 self-improvement primitive. Use only to create isolated learned-pattern or catalog/policy proposal work from historical evidence after deterministic sanitization, replay/evals where required, privacy lint, deterministic proposal identity, independent review, and lineage metadata. Never activate unmerged policy into the current run or merge its own proposal to main.
---

# Auto Self Improve

Work in a separate worktree/branch from the family whose policy is pinned.

For learned patterns: private rows → deterministic sanitizer → minimal redacted evidence capsule → pattern compiler → deterministic privacy lint → public pattern with opaque evidence hash. Learned patterns may not directly change capability floors, reward definitions, hard exclusions, destructive permissions, maturity policy, or security boundaries.

**Routing-identity amendments are a first-class catalog proposal.** A run that recorded a
`route-defect` hands you the attempted slug, the harness error, and the working correction. Amend
the catalog row's `invocation_model_id`/`invocation_harness` (do not rename `model_id` — the
canonical name is spec data), cite the defect id as evidence, and return the proposal ref so the
originating run can close the defect. The evidence here is a harness error string, so sanitize it
like any other row before it leaves the private side.

For catalog/policy proposals: include replay where required, eval results, diff explanation, policy hash change, and independent review.

Use deterministic identity hashes before append/retry. Fetch/reconcile latest proposal branch, skip existing identity, apply if absent, commit/push, and retry a non-fast-forward once from fresh state. Never duplicate content because of races.

A merged PR is terminal; create a successor PR with lineage. Activation occurs only after maintainer merge to main plus the next clean runtime load.
