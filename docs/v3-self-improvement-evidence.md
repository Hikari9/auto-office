# V3 self-improvement evidence — one complete cycle

One end-to-end pass through `skills/auto-self-improve`, executed against this run's own private
telemetry rather than a fixture. Every command below was run; the outputs are transcribed
verbatim except for absolute paths, which are elided so this file passes its own privacy lint.

The cycle is: private rows → deterministic sanitization → dream → public proposal → idempotent
append to a standing branch → independent review → stop at the maintainer-merge boundary.

## 1. Compile a dream from private telemetry

```
python3 scripts/office_propose.py compile-dream \
  --db <runs.db> --state-dir <run-state-dir> --run-id <run-id> --family-id <family-id> \
  --out dream.json
```

Sources: 2 route defects recorded by this run, 8 findings in the recorder database. Two patterns
compiled:

| pattern | stream | occurrences |
|---|---|---|
| `route-identity-silently-defaulted` | `catalog-policy` | 2 |
| `gate-satisfiable-by-excluded-evidence` | `learned-pattern` | 5 |

Both are real to this run. The first is the claude harness accepting an under-specified routed
identity and substituting a default effort instead of failing, observed twice under different
kinds (`invalid-invocation-slug`, `unsupported-effort`) — so a published route notice named an
identity the dispatch did not run under. The second is the defect class that survived four
consecutive correct repairs during review: a gate satisfiable by exactly the evidence it exists
to exclude.

A single route defect does **not** compile into the first pattern; two on one harness do. One
occurrence is an incident, not a pattern. `test_a_single_defect_is_not_a_pattern` holds that line.

## 2. Sanitization is real, and the run id never leaves

The dream carries no run id, no path, no email, no URL, no commit sha. It carries a
`lineage_digest` — `sha256("run" || run_id)` — which the originating machine can reproduce from
a run id it already holds and nobody else can invert. That is the lineage requirement satisfied
without exposure.

`test_dream_never_carries_the_run_id` asserts both halves: the raw id is absent from the
serialized dream, and the digest still matches what the local machine computes.

## 3. Privacy lint on the public proposal

```
python3 scripts/office_runtime.py privacy-lint proposal.md
{
  "findings": [],
  "valid": true
}
```

Lint is a **gate in the appender**, not a report beside it. `office_propose.append` runs
`privacy_findings` on the proposal text and returns `privacy_lint_failed` without creating the
branch directory at all. `test_private_material_is_refused_not_reported` proves the rejecting
branch by appending a proposal with an absolute path in it and asserting no markdown file exists
anywhere under the public branch afterward.

## 4. Append to the standing branch, then replay it

Standing branch: `office/v3/standing-proposals`, in its own worktree, cut from `main` — never in
this run's integration branch history.

First append:

```
{
  "appended": true,
  "identity_hash": "sha256:f3a12df1e1a98054e39692df32a3c02d429201fd0a58687e9da799f696af5148",
  "path": "proposals/f3a12df1e1a98054e39692df32a3c02d429201fd0a58687e9da799f696af5148.md"
}
```

Replay, same dream, same command:

```
{
  "appended": false,
  "identity_hash": "sha256:f3a12df1e1a98054e39692df32a3c02d429201fd0a58687e9da799f696af5148",
  "path": "proposals/f3a12df1e1a98054e39692df32a3c02d429201fd0a58687e9da799f696af5148.md",
  "reason": "identity_already_present"
}
```

After the replay: one file under `proposals/`, one line in `PROPOSALS.md`, one row in `lineage`.
The index file is byte-identical before and after.

The identity is keyed on the **dream**, not the rendered prose, so rewording `render()` does not
produce a second copy of a proposal already on the branch.

**The idempotence test does not pass for the wrong reason.** An appender that refused everything
after its first write would satisfy it too, so `test_a_different_dream_does_append` asserts a
second, distinct dream still appends. Fail-before was proven directly: replacing `append` with a
version that omits the identity check makes
`test_replaying_the_same_dream_appends_nothing` fail on `assertFalse(second["appended"])`.

## 5. Lineage recorded once

`test_lineage_row_is_written_once_across_a_replay` initializes a recorder database, appends
twice, and asserts exactly one `lineage` row with `component_kind='proposal'`,
`parent_id=<lineage_digest>`, `event='appended'`.

## 6. Merge boundary intact

The proposal is committed on `office/v3/standing-proposals` and is **not merged** anywhere. It
is not reachable from this run's integration branch. Activation requires a maintainer merge to
`main` plus the next clean runtime load, per `protocol/privacy-self-improvement.md`. No run
activates its own proposal.

## 7. Graduation bar

`references/self-improvement-graduation-bar.md` states the eight conditions that decide whether a
proposal on the standing branch is eligible for that maintainer merge. Conditions 1 (privacy) and
7 (idempotent identity) are enforced by `scripts/office_propose.py` and covered by
`tests/test_propose.py`; condition 4 (at least two independent evidence rows per pattern) is
enforced by the compiler; condition 8 is the human decision the bar exists to inform.

## What this does not claim

The appender writes to a branch in a worktree. It does not open or update a GitHub pull request —
`references/IMPLEMENTATION-NOTES.md` already records PR creation as orchestration work rather
than implemented behavior, and this cycle does not change that. "Standing PR" is satisfied here
as a standing **branch** with a durable, idempotent append; wiring it to a hosted PR is
orchestration on top, not a different mechanism.

Independent review of this task is recorded with the run's T7 review, not here — a producer
describing its own work is a pass, never an approval.
