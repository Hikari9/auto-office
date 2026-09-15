# Privacy and self-improvement

Raw telemetry, repo identities, hosts, organizations, people, local paths, credentials, and project-specific material remain private.

Dream compilation: private rows → deterministic sanitizer → minimal evidence capsule → pattern compiler → deterministic privacy lint → proposed public pattern. Public attribution uses opaque evidence hashes/source counts/schema version; the local machine alone maps hashes back to private rows.

Deterministically remove/replace repo/org/person names, domains/hosts, emails, absolute paths, credentials/tokens, issue-specific prose, long source spans, and unique customer/project identifiers before model compilation. Reject suspicious material before public push; model self-certification is never the only privacy boundary.

A running family uses only its pinned policy. Self-improvement work occurs in another worktree/branch. Unmerged changes do not activate into the current run. Default activation requires maintainer merge to main plus next clean runtime load.

Two proposal streams: learned patterns (sanitized, non-binding only) and catalog/policy proposals. Learned patterns cannot directly change capability floors, reward definitions, hard exclusions, destructive permissions, maturity policy, or security boundaries. Behavior-changing proposals require replay where applicable, evals, diff explanation, policy hash change, and independent review.

Merged PRs are terminal. Open successor PRs with lineage instead of “reopening” merged PRs. Generated proposal identities must be deterministic/idempotent; on concurrent push races refresh, check identity, apply if absent, retry non-fast-forward once.
