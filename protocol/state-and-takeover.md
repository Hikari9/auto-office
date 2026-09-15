# Durable state, pause, resume, takeover

Every handoff envelope includes: run/family/dispatch ids, role, holder id, exact routable triple, gear, playbook, base SHA, pinned policy/catalog/adapter/config hashes, plan/packet versions, and creation time.

Before resuming, reconcile family registry; current plan/packet; Git branch/worktree/base/head/uncommitted state; issue/PR state; pending findings/dispositions; validation evidence; active role/writer leases; and pinned hashes.

A holder change is a takeover. The new holder must acquire the role lease before writing. Once acquired, the old holder has no mutation authority for that scope.

Explicitly reconcile stale packets/plans, killed dispatches, incomplete atomic edits, pending reviewers, uncommitted changes, changed external dependencies, and changed base branch state. Trust no stale state implicitly.

After long pauses, if catalog/harness/base SHA/external dependency boundaries changed materially, repeat the minimum reconnaissance/verification needed to show the plan remains valid.
