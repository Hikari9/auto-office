# SQLite `runs.db` is the single authoritative run state

Run, family, finding, amendment-delivery, job, lease, and event state live in the existing WAL SQLite `runs.db`, and each semantic transition commits in one transaction. JSON files under the state directory are generated read-only views. We chose this over keeping the JSON files and wrapping every writer in the family `flock` because rolling review needs an amendment and its outbound deliveries to survive a crash together, and a lock cannot make a sequence of file replacements crash-atomic. Decided in [rolling review and gate semantics](https://github.com/Hikari9/auto-office/issues/160); design in [`docs/v31-rolling-review-gates.md`](../v31-rolling-review-gates.md).

## Consequences

Direct edits to `state.json` or the family registry stop having effect. Shell helpers (`review_finding.sh`, `verify.sh`, `office_spawn.sh`) must call the state module instead of writing their own records.
