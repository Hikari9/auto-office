# Native batch landing

Use batch landing when several accepted Office runs or reviewed PRs must form
**one release**, in a declared order, with **one version bump** and **one full
suite**. Ordinary office land is unchanged.

## Manifest and invocation

Save a JSON manifest (outside the repository or as an untracked file):

~~~json
{
  "id": "release-nov-1",
  "base": "main",
  "version": "3.16.0",
  "version_file": "src/office/_version.py",
  "changelog_file": "CHANGELOG.md",
  "merge_method": "merge",
  "full_suite": ["scripts/validate.sh", "--full"],
  "timeout_seconds": 7200,
  "lanes": [
    {"id": "A", "pr": 101},
    {"id": "B", "run": "accepted-office-run-id", "land_after": ["A"]},
    {"id": "C", "pr": 103, "land_after": ["B"], "version_bump": true}
  ]
}
~~~

Run from the target repository:

~~~sh
office land --batch release.json --dry-run
office land --batch release.json
~~~

Every lane contains **either** one PR number (pr) **or** one accepted Office
run ID (run). A run lane merges its accepted task PRs in Office's normal
dependency order, including its existing safety/merge checks. Such runs need
an approved merge end state; batch landing never implicitly deploys an
e2e run. Plain PR lanes rely on GitHub's required checks and merge policy;
the operator must have approved and reviewed those PRs beforehand.

Each lane may set land_after to other lane IDs. The runtime topologically
sorts them, preserving declaration order among unconstrained lanes. Unknown
IDs, duplicate PRs, cycles, drafts, wrong-base PRs, and unaccepted Office runs
refuse **before any merge**. Required PR checks run immediately before each
merge. Batch landing pins a PR's expected head and refuses an unexpected
change. No lane is merged if a predecessor fails.

One lane MUST set version_bump: true. **Exactly one of the PRs in that lane**
must change version_file, and no other PR in the batch may change that file.
That PR is responsible for the release version and one CHANGELOG header.\nThe owner's pinned PR head is fetched and checked **before any merge**, so a\nwrong version/header fails closed rather than blocking halfway through a batch.
The final merged tree must contain the requested version exactly once, and
the file must differ from the starting version. For Auto Office's version
source the semver must increase. Ordinary non-batch PRs still follow AGENTS.md's
per-PR version policy; **batch members defer their bump to the designated
version-owner PR**. Do not increment a version once per lane. The batch tool
does not make its own unsigned/unguarded commit to main.

A manifest's full_suite is an **argv list**, not a shell pipeline. Supply the
repository's actual exhaustive validation command; this is an operator
decision, not something Office can infer from a passing scoped check.
timeout_seconds defaults to 7200 and is capped at 14400. The full suite
runs in a detached checkout of the final main SHA, **after the last merge**,
not after each PR. A result is logged at Git's common directory under
office/batches/<id>/full-<sha>.log, with an atomic JSON state record beside it.

## Stop, recover, iterate to green

The command stops on the **first failing lane**, naming the lane, PR or check
reason, and already merged lanes. Do not roll back or re-merge them blindly.
Re-executing the same manifest skips completed lanes after verifying their
GitHub state. The batch is fenced by a repository-wide local lock and refuses
a default-branch change it did not record.

If the composed full suite fails, the batch becomes **red**. Fix forward in a
new independently reviewed PR, append a new lane with
land_after: ["last-previous-lane"], and rerun the **same batch id**. The
original lane definitions/order, version and full-suite command are immutable;
only new fix lanes may be appended. The full suite then reruns **once on the
new final SHA**. If the suite failure was transient rather than code-related,
office land --batch release.json --retry-full explicitly retries on the
unchanged SHA. A green batch will not merge anything more; create a new batch
for another release. It does not close Office runs, publish to PyPI, deploy
production, or close GitHub issues automatically.

The state and logs are local to the repository's Git common directory.
Back them up with the release evidence when moving the lander to another
machine. If an external push changes main while a batch is in progress, Office
refuses rather than silently treating it as part of this release.

## Limits

Batch support coordinates landing; it does not write a fix automatically
or replace independent review. The responsible orchestrator/executor
implements the failing fix PR and re-enters the batch loop. Merge and
version validation failures remain real blockers and require an authorized
resolution. Single-run office land retains its existing deploy behavior.
