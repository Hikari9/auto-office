# Verification and review

Every mutable run self-verifies. Independent verification is required by risk, gear, playbook, repository policy, or user-facing acceptance requirements.

Prefer targeted tests → regression tests → type/lint/static → build/package → focused runtime → broader suites when justified. A passing gate is not automatically trusted; where practical, show critical gates fail on known-bad/mutated input before accepting green.

User-facing work requires browser validation when a reachable local/preview runtime can reasonably be produced. Execute the actual acceptance flow, not merely the homepage.

## Review tiers (issue-35#decision-4)

Review is local first; a final/integration adversary is boundary-triggered, never a default second pass:

- **Local.** Each executor owns its own implementation and review loop: self-verify, spawn/receive a code adversary when funded, accept a finding and fix it or reject it with stronger evidence. `review_mode: independent_adversary`.
- **Inline.** For cheap, reversible, low-risk work, self-review/validation substitutes for an independent adversary; review funding stays mode/risk/routing dependent, not a blanket waiver. `review_mode: labeled-inline` — never represented as `independent_adversary`.
- **Integration.** A final orchestrator-spawned adversary exists only when two or more executors produce dependent or merging landings that must compose across a shared interface. It is triggered by the integration boundary, not by executor count: a single-executor family, or independent parallel changes with no cross-scope dependency, receives no mandatory second review. `review_mode: integration_adversary`; scope is the integrated diff, not a re-review of each executor's code.

`disposition_owner` (`executor | planner | orchestrator`) records who is authoritative for a finding's next action; ordinarily this is the executor (or planner, for a plan-scoped finding). A local defect returns to its responsible executor; a plan/spec defect wakes the planner; an unresolved cross-executor conflict goes to the orchestrator for integration coordination; a genuinely unresolved evidence conflict or user-owned decision escalates to the user rather than being settled by whichever role happens to hold the gate.

Reviewer finding states: `accepted-material`, `accepted-minor`, `rejected-on-evidence`, `pending`/`deferred`. Only accepted-material receives full gate credit. Use the same reviewer session across rounds when possible to retain prior uncertainty/findings, but never the producer session.

A `PLAN DEFECT`/`BRIEF DEFECT` is valid only when the raising role followed the artifact enough to test its assumption, provides concrete contradictory evidence, and shows that continuing would violate outcome/safety. Accepted defect pauses affected scope, increments artifact version, invalidates stale packets, amends through the proper owner, then resumes from the new version.
