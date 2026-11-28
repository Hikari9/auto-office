# Cloud coordinator bridge

`office bridge` gives a **trusted worker** a narrow JSON interface to Auto Office.
It runs on the machine that owns the Office checkout, credentials and `runs.db`.
That machine can be a cloud VM or supported managed execution environment; a
personal computer does not have to stay online. The bridge has no HTTP listener,
dashboard, scheduler, autonomous decision maker, or model-selection policy.

The cloud coordinator owns mission authority, distributed ownership, external
resource conflicts and budgets. Auto Office continues to own planning, routing,
execution, independent review, verification and landing. The bridge delegates to
the existing lifecycle and never implements a second engineering lifecycle.

## Trust boundary

Run the bridge in an operator-owned worker, **outside an agent's sandbox**. The
agent must not be able to edit the bridge config, admission program, workspace
registry, Office policy or service credentials, or invoke the Office CLI directly
with the worker's privileges. An authenticated queue/HTTPS transport may invoke
the stdio interface, but exposing stdin or a generic shell to an untrusted agent
does not meet this requirement.

Every operation, including inspection, invokes an operator-configured admission
program. It must query the canonical coordinator service and verify the current
mission owner, fence, lease, delegated policy, action scope and relevant resource
reservation. It must fail closed if any check cannot be performed. A callback that
merely echoes `allowed: true` is useful only in offline tests and is unsafe live.

Keep distributed resource reservations for the **whole accepted Office run**,
including delegated workers and uncertain operations. An admission response is
not a distributed lock. Do not release a reservation because the HTTP caller timed
out, the coordinator lease expired, or the Office command returned. Ownership
transfer must preserve the run reservation and reconcile outstanding work; a new
coordinator cannot race a still-running old worker against the same resource.
The bridge rechecks authority on every new command, but it cannot revoke credentials
already held by an independently running harness. Keep consequential external
credentials behind a fenced tool gateway where required, and drain/cancel/reconcile
in-flight Office work before releasing the protected resource.

## Operator setup

Install the same approved Auto Office release, repository checkout and supported
harness binaries on the cloud worker that would be used locally. Keep Office state
on a durable volume owned by that worker. Never concurrently mount one SQLite file
on different machines or synchronize `runs.db` as a distributed database. Separate
workers have separate Office state and use the coordinator's resource locks.

Create a protected JSON configuration, for example `/etc/office/bridge.json`:

```json
{
  "workspaces": {
    "service": "/srv/workspaces/service"
  },
  "admission_argv": [
    "/usr/bin/node",
    "/srv/coordinator/scripts/cloud-bridge-admission.mjs"
  ],
  "timeout_seconds": 120
}
```

The admission executable and script paths are operator configuration, never
request fields. The executable must have an absolute path; no shell is used.
The configuration must be owned by the effective operator or root and must not
be writable by group or others. Protect its parent directories and referenced
programs too. Repository aliases resolve only through this registry. No remote
request may provide a checkout path, environment variables, executable or arbitrary
CLI switches.

Use `OFFICE_BRIDGE_CONFIG` for that absolute config path. Use `OFFICE_DATA_HOME`
and `OFFICE_STATE_HOME` for the worker's durable Office storage. The admission
program may use the coordinator's scoped service credential, held only by the
trusted worker. `RICO_*`, `CLOUD_RICO_*` and `OFFICE_BRIDGE_*` environment variables
are stripped from Office child processes. Put admission-only secrets under those
names or use a secret manager inside the admission program; do not place them in
generic environment variable names inherited by harnesses. The bridge never
prints admission stderr or an exception's raw payload.

```bash
OFFICE_BRIDGE_CONFIG=/etc/office/bridge.json office bridge < request.json
```

This consumes one bounded UTF-8 JSON request and prints one JSON response. Exit
status is zero on success and 4 on a refusal or indeterminate command. There is no
background service started by the bridge itself.

## Request and admission contract

All requests use these exact top-level keys:

```json
{
  "protocol": "rico-office-v1",
  "request_id": "e5b0387a-4983-4bfc-a462-ae3db87402c8",
  "mission_id": "c423ae2e-b4af-4d21-a2d3-b92a0749d7fe",
  "coordinator_id": "cloud:rico",
  "fence": 1,
  "operation": "start",
  "arguments": {
    "repository": "service",
    "goal": "Implement the approved bounded issue and open a reviewable PR",
    "gear": "S",
    "issue": "https://github.com/example/service/issues/12"
  }
}
```

Both IDs are canonical lowercase UUIDs. `fence` is a positive integer no larger
than `2^53 - 1`. Unsupported keys and operations are rejected before admission.
Use a new request ID for each new action. Retransmit the exact same request for a
transport retry; changing any field, including the fence, under an existing ID
is an idempotency conflict. After ownership transfer, use new request IDs to
inspect or resume the existing mission binding.

The admission program receives this entire validated request as JSON on stdin.
On success it must return:

```json
{
  "allowed": true,
  "mission_id": "c423ae2e-b4af-4d21-a2d3-b92a0749d7fe",
  "request_id": "e5b0387a-4983-4bfc-a462-ae3db87402c8",
  "fence": 1,
  "request_digest": "the lowercase SHA-256 digest of the canonical request"
}
```

The digest is SHA-256 of UTF-8 JSON with object keys recursively sorted, no
insignificant whitespace, literal Unicode and no ASCII escaping. In Python:
`json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)`. All numeric fields are safe integers; there are no float
fields. In JavaScript, recursively sort object keys, preserve array order, then
use `JSON.stringify`. The digest binds the coordinator, operation and arguments
as well as the mission and fence. Every echoed value must match exactly. Missing,
denied, timed-out, malformed or mismatched admission is a refusal.

For in-process integrations, `office.bridge.execute(request, authorize=callback,
workspaces=registry)` exposes the same boundary. The callback must implement the
same live authorization checks as the operator admission executable.

## Operations

| Operation | Arguments | Behavior |
| --- | --- | --- |
| `start` | `repository`, `goal`, `gear`; optional `issue`, `playbook` | Creates one mission-bound Office run through `office start`. Gear is XS/S/M/L/XL; normal Office routing and review policy apply. End state is `ask`; no display-only board card is created. |
| `dispatch` | `tasks` (1–50 distinct task IDs); optional `parallel` boolean | Calls normal Office dispatch on the bound run. Office checks its own plan authorization, dependencies, leases, routes and review constraints. |
| `status` | `{}` | Reads a bounded snapshot of the bound run, tasks, gates and command receipts without starting/reconciling jobs. |
| `cancel` | `reason` | Uses `office close --abandon`, preserving worktrees and evidence. This is terminal; the same run cannot resume after cancellation. |
| `resume` | `{}` | Resumes the bound, nonterminal run through Office's existing recovery. A queue pause is released first, while an explicit global `auto off` remains binding. Root wake stays off for this noninteractive bridge call. |
| `evidence` | Optional `after` cursor and `limit` 1–100 | Returns mission-scoped evidence identifiers, recorded hashes, byte sizes and gate/revision references, with `has_more` and `next_cursor`. |

The mission binding supplies the run ID. A request cannot name another run.
`playbook` is Change, Restructure, Investigate, Prototype or Visual. `issue` must
be an HTTPS GitHub issue URL. The interface deliberately has no arbitrary shell,
configuration, route-policy editing, approval, waiver, merge, deploy or prune
operation. These remain the existing Office/operator flows with their existing
authority checks. No local/cloud authority distinction is added to Office.

`status` does not call `office status`: normal Office status may reconcile and
kick queued jobs. Its snapshot includes up to 100 tasks and 100 gates, and the
latest 40 bridge receipts; it is not an exhaustive event export. `evidence`
exports metadata only. It does not upload paths, raw transcripts, logs, local
files or `meta_json`, and a recorded hash is not a fresh content verification.
Use a separately approved scoped artifact channel when actual evidence content
must leave the worker.

Mutation responses contain a durable `receipt` with `status`, `result`, `error`
and timestamps. `receipt.result` contains an export-safe projection of the Office
outcome: exit code, success flag, exact run ID and refusal category where present.
Free-form output, errors, next-step text, task titles, pause reasons and local
paths stay on the worker. This projection also applies when replaying historical
receipts. `replayed: true` means the bridge returned an existing receipt, not
that Office ran again. A successful mutation means Office accepted that command;
it does not imply that engineering is complete, reviewed, merged or deployed.

## Interrupted work and cancellation

`commands` in `runs.db` records request receipts. A new `bridge_missions` table
binds one mission to one start request and, once confirmed, its exact run. The
receipt, reservation and running claim are created in one SQLite transaction.
A duplicate request never invokes Office twice, and changing the request ID
cannot create a second run for a mission that already has a start reservation.

If Office times out or returns an invalid receipt after it might have acted,
the outcome is `unknown`, not a retryable failure. A worker crash may leave a
receipt `running`; that also does not authorize replay. The resource reservation
must stay held until the outcome is reconciled. The bridge does not infer safety
from a process ID or elapsed time.

A lost start response is recoverable through Office's **existing transactional
session binding**. The bridge starts Office with an internal protocol/request
session key; Office commits that binding atomically with the run. `status`
reports a verified `recovery_candidate` when the run committed but its stdout was
lost. An authorized `resume` binds that exact candidate after checking its
recorded intent and workspace, then uses normal Office recovery. The old unknown
receipt remains in history. No timestamp/goal search is used to select a run.

If there is no correlated run, restore or inspect the original worker and its
command before taking further action. The reservation remains blocked; do not
delete receipts or edit `runs.db` to force a retry. If the operator proves that
the operation had no effect, close the unsuccessful higher-level mission through
the coordinator's documented reconciliation process and submit a new mission.

Cancellation reports known worker termination separately from lifecycle status.
Both pre-cancellation workers and durable cancelled/claimed jobs observed after
the terminal transition are checked, including jobs that started during the
cancellation. Their recorded process-start identities are retained for the check.
Workers without an observable process ID, or processes that remain alive or
cannot be verified, appear in `pending_workers`. Even when all known worker
processes have stopped, `external_effects_verified` stays false: an HTTP write
already accepted by another service can still complete. Revoke consequential
tool access at the coordinator gateway first, request cancellation, then inspect
the actual affected resources before releasing their reservations. Cancelling
one mission does not operate on unrelated local Office runs.

Later `status` calls passively recheck current workers and return
`known_workers_stopped`, `pending_worker_count` and at most 100 `pending_workers`
identifiers/states. The stopped flag becomes true only for a terminal run whose
known observations are all proven stopped. Original cancellation process
identities are retained privately in the local receipt so a later status can
verify exit even after the lifecycle clears its stop request. PIDs, hostnames,
process-start identities and these private observations are never exported.
An older cancelled worker with no verifiable identity remains unverified.
`external_effects_verified` remains false even after all workers drain; a separate
external-resource reconciliation is still required before releasing their holds.

## Harness and deployment limits

Cloud-hosted Claude Code, Codex, Gemini and other installed harness adapters use
the same Office catalog, user policy, launch profiles and fallback rules as local
runs. A provider or harness still needs supported authentication and execution
in that environment. Do not copy unrestricted local auth stores, automate an
interactive login, or assume a subscription grants cloud/API permission. Report
unavailable adapters and missing credentials instead of pretending parity.
Check the installed adapters with `office harness list`, `office harness validate
<id>` and the effective route policy with `office route policy --effective`.
`office harness smoke` may invoke a model; run it only with applicable spend
authorization. The bridge does not provision billable infrastructure or start
models merely to remain available.

Deploy by installing the reviewed package on the trusted worker, applying its
ordinary additive SQLite migration, installing the admission helper and config,
and connecting an authorized transport to stdin. Validate on an isolated worker
first. Roll back by disabling that transport and admission, cancelling/reconciling
the affected mission if necessary, then restoring the prior Office package. The
additive mission table and command history can remain; never discard unresolved
receipts to roll back. Existing local CLI commands remain available independently.

## Verification

`tests/v31/test_bridge.py` covers exact admission, denied/offline admission,
command injection and cross-mission refusal, replay and concurrent duplicates,
lost-start recovery, passive inspection, bounded evidence metadata, cancellation
uncertainty, and the real Office lifecycle with fake harnesses. The normal scoped
PR gate also runs contracts and the named safety smoke tests. Live cloud hosting,
real provider auth, the admission service's distributed fencing, and an actual
external cancellation require deployment-environment evidence; offline fixtures
do not claim those are verified.
