# Native batch landing

Use batch landing when several accepted Office runs or reviewed PRs must form
**one release**, in a declared order, with **one version bump** and **one full
suite that runs before anything reaches main**. Ordinary office land is
unchanged.

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
run ID (run). A run lane lands its accepted task PRs in Office's dependency
order. Such runs need an approved merge end state; batch landing never
implicitly deploys an e2e run. Plain PR lanes rely on GitHub's required checks
and merge policy; the operator must have approved and reviewed those PRs
beforehand.

Each lane may set land_after to other lane IDs. The runtime topologically
sorts them, preserving declaration order among unconstrained lanes. Unknown
IDs, duplicate PRs, cycles, drafts, wrong-base PRs, unaccepted Office runs,
and stacked run PRs under a squash or rebase method refuse **before any
merge**.

One lane MUST set version_bump: true. **Exactly one of the PRs in that lane**
must change version_file, and no other PR in the batch may change that file.
That PR is responsible for the release version and one CHANGELOG header. The
version check runs on the **composed** tree, before the suite, so a wrong
version or header fails closed before any merge. The composed tree must
contain the requested version exactly once, and the file must differ from the
starting version. For Auto Office's version source the semver must increase.
Ordinary non-batch PRs still follow AGENTS.md's per-PR version policy; **batch
members defer their bump to the designated version-owner PR**. Do not
increment a version once per lane. The batch tool does not make its own
unsigned/unguarded commit to main.

A manifest's full_suite is an **argv list**, not a shell pipeline. Supply the
repository's actual exhaustive validation command; this is an operator
decision, not something Office can infer from a passing scoped check.
timeout_seconds defaults to 7200 and is capped at 14400.

## Order of operations

1. **Preflight.** Resolve every lane, check PR state, bases, drafts, and the
   version owner. `--dry-run` stops here. Preflight writes no PR, branch or
   default-branch state; it may apply pending runs.db schema migrations.
2. **Compose (local only).** Merge each lane's pinned PR head, in lane order,
   onto the current default branch in a throwaway checkout. A conflict refuses
   the batch with nothing merged. The composed commit is recorded.
3. **Verify version (local only).** Check the version and changelog on the
   composed tree.
4. **Full suite, once, on the composed SHA.** It runs in a detached checkout
   of that commit, with GH_*, GITHUB_*, token, secret, password, key, AWS_*,
   SSH_AUTH_SOCK and OFFICE_* variables removed, in its own process group. A
   timeout kills the whole group. Each attempt's log is kept under Git's common
   directory at office/batches/<id>/full-<sha>-<attempt>.log.
5. **Merge, only after a green suite.** For each lane in order, Office checks
   that main has not moved, checks that each PR head still equals the head the
   suite validated, waits for required checks, re-checks main, then merges with
   `--match-head-commit <validated head>`. Every merge, run lanes included,
   uses that flag and never `--admin`. Office records main after each PR, so a
   restart inside a multi-PR lane resumes instead of refusing.
6. **Tree proof.** After each lane, the default branch tree must equal that
   lane's composed tree. A mismatch blocks the batch before the next lane. After
   the last lane, Office runs record their landing, and the batch becomes
   **green**.

If the suite fails, nothing has merged. If a merge fails, the batch is
**blocked** with the lanes already merged recorded. A blocked batch that merged
nothing recomposes on rerun, so a PR whose head changed is revalidated.

## Stop, recover, iterate to green

The command stops on the **first failing lane**, naming the lane, PR or check
reason, and already merged lanes. Do not roll back or re-merge them blindly.
Re-executing the same manifest resumes after the last merged lane, after
verifying GitHub state. The batch is fenced by a repository-wide local lock
and refuses a default-branch change it did not record.

If the composed full suite fails, the batch becomes **red** and nothing has
merged. Fix forward in a new independently reviewed PR, append a new lane with
land_after: ["last-previous-lane"], and rerun the **same batch id**. The
original lane definitions/order, version and full-suite command are immutable;
only new fix lanes may be appended, and only while the batch is red. The fix
lanes are composed onto the current default branch, and the full suite reruns
**once on the new composed SHA**. If the suite failure was transient rather
than code-related, office land --batch release.json --retry-full explicitly
retries on the unchanged composed SHA, provided the default branch has not
moved. A green batch will not merge anything more; create a new batch for
another release. It does not close Office runs, publish to PyPI, deploy
production, or close GitHub issues automatically.

If the default branch moves while the suite runs, or a run's integration
changes during it, the composition is stale. Nothing merges, and the next run
recomposes and revalidates.

The state and logs are local to the repository's Git common directory.
Review them before copying them into release evidence when moving the lander
to another machine; suite output can contain anything the tests print. If an external push changes main while a batch is in progress, Office
refuses rather than silently treating it as part of this release.

## Limits

Batch support coordinates landing; it does not write a fix automatically
or replace independent review. The responsible orchestrator/executor
implements the failing fix PR and re-enters the batch loop. Merge and
version validation failures remain real blockers and require an authorized
resolution. Single-run office land retains its existing deploy behavior.
Stacked run PRs are retargeted and merged on their pinned heads, which only
the merge method supports. A stacked PR must rest on the batch base or on
another PR of the same run.

GitHub does not pin the base branch. Office re-checks main before each merge
and after each lane, so an external push can land once before the batch stops.
Require branch protection that blocks direct pushes to the default branch.

The worker guard (OFFICE_DISPATCH_ID) is advisory. Withhold direct merge rights
from worker sessions. A branch that is behind main, and needs an update
before GitHub accepts the merge, blocks the batch; Office does not update the
branch because that would change the pinned head.
