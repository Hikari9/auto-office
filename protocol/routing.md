# Routing

Route identity is `harness@version × model_id × effort`. Public benchmark identity is only `model_id × effort`; never share runtime reliability, quota, dispatch-form, or adapter-trust evidence automatically across harnesses.

Filter in this exact order: hard exclusions → adapter validity/trust → required capabilities → absolute role floor → task shape → quota safety → advisory quality anchor → cost → local tie-break evidence.

Quota reserve defaults to 20%. Unknown quota is not unlimited. Exclude a candidate whose projected dispatch crosses reserve when another qualifying candidate exists. If all qualifying candidates cross reserve, return to the orchestrator for a smaller valid strategy, lower-cost route, or explicit user decision; do not lower capability floors.

Balanced cost policy: eliminate quota-unsafe; estimate money; among candidates within 20% of the cheapest money estimate prefer lower quota burn; then lower wall clock; then stronger local reward. Support `money_saver`, `quota_saver`, `balanced`.

Advisory quality anchor: when the resolved role config has a `preferred_seed` (an ordered `{model_id, effort, harness?}` chain), it drives the anchor directly. A candidate matching an entry passes; with no match the anchor imposes no restriction. Passing candidates are then ordered by chain position before cost — the first entry wins whenever it clears every earlier stage, with cost/quota/wall/reward only breaking ties within the same chain position. This intentionally overrides the balanced money-band elimination for the chain's own entries, since the chain already encodes the user's explicit cost/quality tradeoff.

Exploration is limited to workers, Investigate/Prototype, reversible sandbox, non-production. Default maximum one exploratory dispatch/run, max 10% over rolling 20 eligible workers, and <=125% of cheapest qualifying projected cost without explicit user approval.

Every successful route emits `selection_disclosure`: role, canonical `model_id`, exact `invocation_model_id` when exposed by the harness binding, effort, harness/version, selected triple, and a concise reason derived from the decisive filter/tie-break evidence. This object is durable handoff data. Executor and reviewer dispatches surface it to the user immediately before invocation and preserve it in dispatch/readback evidence.
