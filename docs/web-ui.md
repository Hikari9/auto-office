# Web UI

## Observer and projection

`office.web` reads runs.db for the web view. It never writes to it.

### Read-only guarantees

- `observer.open_readonly(path)` opens `file:<path>?mode=ro` with `uri=True` and sets `PRAGMA query_only=ON`. A write
  fails twice over: `query_only` refuses it, and the file handle is read-only even if `query_only` is lifted.
- It never calls `db.connect` or `db.migrate` and never creates directories. A missing runs.db raises
  `FileNotFoundError`.
- Each projection is read inside one deferred read transaction (`read_snapshot`). Under WAL this pins one snapshot, so a
  writer committing mid-read cannot produce a torn view (for example a task whose run is missing).
- Older schemas are read as they are. A missing table or column turns the matching capability `false`; nothing is
  created or altered. Building projections leaves `cursors`, `events`, `schema_meta`, `runs.updated_at`,
  `dispatches.last_seen_at` and the logical content (iterdump hash) unchanged.
- Liveness probes (herdr, ps), telemetry, the reply-file reader and GitHub slugs are injected through
  `projection.Context`. Defaults never shell out: probes report `unknown`.
- The host id lives in `<state home>/web/host-id`. Only the service's explicit startup creates it
  (`identity.ensure_host_id`); reads use `read_host_id` or an injected value.

### Identities

| Thing | Form |
|---|---|
| host | `host:<id>` |
| repository | `repo:github.com/<owner>/<name>` when the slug is known, else `repo-local:<sha256(git_common_dir)[:16]>` (always present as `local_key`) |
| run | `run:<run_id>` |
| task | `task:<run_id>/<T>` |
| dispatch | `dispatch:<id>` |
| orchestrator session | `session:<run_id>/<harness>/<session_id>` (active `session_bindings` only) |
| runtime | `runtime:<office_version>` |
| issue / PR | `issue:<repo key>#<n>` / `pr:<repo key>#<n>` |

### Run projection

`Observer.workspace()` returns `{host, schema, repos, runs}`; `Observer.run(id)` returns one run or `None` (for
example after prune). A run carries:

- `goal`, `phase`, `gear`, `office_version`, `release_line`, `runtime`.
- `end_state` (`{value, source, requirements_version}` from the latest requirements, else `landing_json`) and active
  `authorizations`.
- `issue` from `landing_json.issue` with `provenance: office-record`.
- `tasks`, each with its `route`, and `prs` from `tasks.pr_json` only (`provenance: office-record`). Nothing is
  inferred from text or branch names.
- `gates.office` (counts from the `gates` table) kept apart from `gates.github_checks` (not in runs.db, so
  `unavailable`).
- `owner`: the active session binding, or `{"kind": "none"}`.
- `liveness`: `terminal` (closed/abandoned/terminal_at/pruned_at), `live` (active binding or an unended running
  dispatch), else `resumable`.
- `progress`: `{value, accepted_weight, total_weight, basis}` from task status only. Each non-cancelled task weighs 1.
  `null` when the run has no tasks.
- `agents.columns`: `orchestrators`, `plan_reviewers`, `executors`, `code_reviewers`, `visual_verifiers`, `other`.
  One node per active binding and per latest dispatch for each (task, role). Older attempts go to `history`.
- `capabilities`: schema presence flags plus `legacy_runtime`/`read_only` for runs below release line 3.1.

Each agent node has `harness`, `model`, `effort`, `current_work`, `telemetry` and `state`. `state` keeps separate
evidence fields: `process` (alive/exited/unknown), `activity` (busy/idle/unknown), `paused`, `blocked`, `quota_wait`
(from `stall_kind`/`resets_at`), `reply_written_awaiting_ingestion` (non-empty `reply.txt` for an unended reviewer),
`complete`, `stale` (status stale/superseded) and `unavailable` (failed, cancelled or lost). Telemetry fields `cpu`,
`ram`, `context` and `quota` are `{status: measured|unavailable|unknown, value, unit, source, observed_at}` and never
default to 0.

### Route projection

Per task, from the latest `route_audit.disclosure_json` (schemas/routing-decision.schema.json) and the executor
dispatch's `route_json`: `primary`, `fallbacks`, `reason`, `strength`, `weakness`, `dispatched`, `fallbacks_taken`,
`planner_override` and the `audits` list. With no audit rows it is `{available: false, reason, ...}`. No scoring is
recomputed.

### Activity

`Observer.activity(run_id, limit=50, before_seq=None)` returns newest-first events (max 200) and `next_before_seq` for
the next page. Summaries and payloads are redacted: tokens, keys, `Authorization` values and env-style secrets become
`***`, home-directory paths become `~/…`, and lengths are capped.

### Synthetic workspaces

`synthetic.build_workspace(dir, "small"|"large", seed=0)` writes `runs.db` through `db.connect` plus `github.json`.
Output is deterministic per seed. It includes legacy-route runs, paused, blocked, quota-wait and awaiting-ingestion
dispatches, repositories sharing issue numbers, stacked PRs and a terminal-started run.

## Service

`office web serve` runs the service in the foreground. `office web start` daemonizes it and waits for its pid file at
`<state home>/web/web.pid` (`{pid, host, port, url, fixture}`); the log is `<state home>/web/web.log`. `office web
status` reads the pid file, and `office web stop` sends SIGTERM and waits. The default address is `127.0.0.1:8765`.

Startup is an explicit lifecycle step, the only one that writes:

1. `db.connect(runs.db)` runs the migration once and logs the schema it reached.
2. `identity.ensure_host_id` creates `<state home>/web/host-id` if it is absent.
3. `commands.recover_interrupted` turns `running` receipts whose executor is gone into `unknown`.

After startup every read goes through the read-only `Observer`, one reader at a time. The service keeps one writer
connection, and only for command receipts.

### Snapshot and stream

`GET /api/snapshot` returns `{epoch, rev, freshness, entities, scalars}`:

- `epoch` is random per process. `rev` increases by one per published change.
- `freshness.office` is `{state: live|stale|disconnected, reason, last_ok_at}`. It is `stale` when no observer read
  has succeeded within 15 s and `disconnected` when runs.db is missing, replaced or unreadable. `freshness.github` is
  the worst state across T4's discovery, issue and PR sources, with each source's freshness kept separately.
- `entities` holds `repos`, `issues`, `prs`, `runs`, `tasks`, `agents`, `queue` and `commands`, each keyed by its
  stable id (`repo:…`, `issue:…`, `pr:…`, `run:…`, `task:…`, `session:…`/`dispatch:…`, the queue item id,
  `command:<id>`). Each run carries `controls` (see Capabilities) and `launch` (the web command that started its
  orchestrator, if any, with `provenance: web-launch`).
- `scalars` holds `scheduler` (active count, host gate states, auto mode), `host` (host id and telemetry), `fixture`,
  `launcher` (available, reason) and `schema`.

The poller checks `PRAGMA data_version` and `MAX(events.seq)` every second, plus a GitHub data signature. When one
moves, it rebuilds the snapshot and publishes a delta `{epoch, rev, base_rev, upserts, removes, scalars}`.
`upserts` and `removes` are per collection. `scalars` carries the changed scalars and `freshness` when it moved.

`GET /api/stream` is Server-Sent Events. Each event has `id: <epoch>:<rev>`.

- With no `Last-Event-ID`, the first event is `snapshot` (the full snapshot), followed by `delta` events.
- With a `Last-Event-ID` from this epoch that the delta ring (512 deltas) still covers, the missed deltas follow.
- A foreign epoch, a rev from the future, a malformed id or a gap gets a `resync` event that carries the full
  snapshot. A service restart always produces a new epoch, so a reconnecting client always resyncs after one.

The client applies a delta only when `delta.base_rev` equals its own rev. It ignores a delta whose rev it already
has (a duplicate) and resyncs on a gap or a foreign epoch. `service.apply_delta` is the reference implementation.

Other reads:

- `GET /api/settings[?run=<id>|?repo=<owner/name>]` returns the settings view.
- `GET /api/runs/<id>/activity?limit=&before=` returns T1's redacted activity window.
- `GET /api/commands/<id>` returns one receipt.

## Security

- The service binds loopback only. `--host` accepts `127.0.0.1` (and other 127/8 addresses), `localhost` or `::1`.
  Anything else is refused with `non-loopback-host` before anything is built.
- Every request must carry a `Host` header equal to the bound `host:port` (`localhost:port` is also accepted for
  127.0.0.1). Anything else gets 421 `bad-host`, which guards against DNS rebinding.
- A POST needs `Content-Type: application/json` (otherwise 415 `bad-content-type`) and an `Origin` equal to this
  server's origin when one is sent (otherwise 403 `bad-origin`). It also needs `X-Office-Token` equal to the
  per-process random token embedded in the served `index.html` (`<meta name="office-token">`), otherwise 403
  `bad-token`. The token is compared in constant time and changes on every restart.
- Responses are `no-store`. The page gets a CSP that limits scripts and connections to the same origin. Bodies are
  capped at 64 KiB.
- Fixture mode (`--fixture small|large`) builds T1's synthetic workspace in a temp Office home and serves T4's client
  on a fake transport over its `github.json`. Launcher and executor are fakes, and the page shows a `FIXTURE MODE`
  marker. It never opens the real runs.db and never calls GitHub.

## Commands

`POST /api/commands` takes `{id, kind, target, expect, payload}`. `id` is the client's idempotency key (8 to 128
characters of `[A-Za-z0-9_.:-]`).

1. Shape and kind are checked first. An unknown kind gets 400 `unknown-kind`. There is no merge, land, deploy or shell
   kind.
2. While Office freshness is not `live`, every command is refused with 409 `office-stale`.
3. A repeated `id` with the same request returns the first receipt with `replayed: true` (HTTP 200) and never
   executes again. The same `id` with a different request gets `idempotency-conflict`.
4. A receipt is recorded through T2's `commands.record` before anything runs.
5. The exact target is re-validated against a fresh observer read, with checks chosen per kind rather than one
   combined gate:
   - issue kinds (`start_issue`, `queue_issue`): repository + issue identity (a known open issue) and execution
     readiness, plus no live run (start) or no existing queue item for the issue (queue);
   - run kinds (`resume_run`, `attach_run`): a non-terminal run and its capability;
   - scheduler kinds (`pause`, `resume`, `set_priority`, `demote`, `set_auto_mode`): an existing queue item or run
     plus runtime capability, never GitHub readiness;
   - `change_route`: the current live dispatch;
   - `chat_send`: the active orchestrator binding with a live agent on that pane;
   - settings kinds: a known key and an editable tier.

   T9 completes and tests the full table. On failure the receipt becomes `failed` with a
   named reason, and the API returns 409 with that reason and the receipt. Reasons include `run-missing`,
   `run-terminal`, `capability-missing`, `dispatch-not-current`, `harness-mismatch`,
   `not-awaiting-plan-authorization`, `binding-ended`, `agent-not-live`, `repo-not-ready`, `repo-unknown`,
   `issue-unknown`, `issue-already-queued`, `item-missing`, `unknown-key`, `issue-has-live-run`,
   `issue-has-resumable-run`, `launcher-unavailable` and `expectation-failed`. `expect` keys (for example `phase`,
   `liveness`, `dispatch_id`, `route`, `plan_version`) must equal the freshly read values.
6. It executes in the background (HTTP 202) and the receipt ends `completed`, `failed` or `unknown`.

Execution runs `python -m office …` (this process's runtime). Run-scoped commands run in the run's checkout with
`--run <id>`, so the frontdoor pins them to the run's runtime. Machine-level commands run from the state home.
Exit 0 maps to `completed`, an Office refusal exit (1 to 63) maps to `failed`, and a timeout, signal or crash maps to
`unknown`. An `unknown` receipt is never retried automatically.

| kind | target | payload | runs |
|---|---|---|---|
| `start_issue` | `{repo, issue}` | `end_state`, `title`, `new_run_confirmed` | launcher: orchestrator in a new Herdr pane |
| `queue_issue` | `{repo, issue}` | `priority`, `title` | `office queue add <owner/name#n>` |
| `resume_run` | `{run_id}` | | launcher: orchestrator told to `office resume <run>` |
| `attach_run` | `{run_id}` | | focus the orchestrator's Herdr pane |
| `pause` / `resume` / `demote` | `{run_id[, task_id]}` or `{item}` | `reason` (pause) | `office queue pause/resume/demote` |
| `set_priority` | as above | `level` | `office queue priority` |
| `set_auto_mode` | `{[run_id]}` | `mode: on/off` | `office queue auto` |
| `change_route` | `{run_id, dispatch_id}` | `route` (same harness), `quote` | `office amend route <D> --as … --quote … --restart` |
| `chat_send` | `{host, run_id, session}` | `text`, `resend_of` | `dispatch.submit_prompt` to the orchestrator's pane |
| `settings_set` / `settings_unset` | `{tier: machine/repository, key[, repo/run_id]}` | `value` | `office config --user/--repo …` in that checkout |

Plan authorization is not a web command: the browser shows the copyable `office approve plan --quote "<words>"`
command for the user to run.

`start_issue` is refused when the issue has a live run (attach to it instead). When the issue has a resumable run it
is refused unless `payload.new_run_confirmed` is true and the runtime allows a new run.

### Capabilities

`capabilities.py` derives per-run controls from the run's release line and the runtime that would serve it: this
process when it is on the line and at least the newest registered patch, otherwise the newest registered patch
(frontdoor registry), probed once for `office queue` and `office amend route --restart`. Runs on 3.0 (legacy), 3.1 or
3.2, unregistered lines, or runtimes without those commands get read-only controls, each with a reason string.
`resume_run` and `attach_run` are also off, with the reason, when the launcher is unavailable.

### Launcher and the queue loop

`launcher.py` opens a new Herdr tab in the repository checkout. There it starts the configured orchestrator route
(`scheduler.orchestrator_route`) and delivers an `/auto-office` prompt that carries the issue URL, the authorized end
state and the command receipt id. Without Herdr or the harness, start/resume capability is `false` with the reason,
and the copyable `office start --issue <url> --end-state <s> "…"` command is returned instead.

Each poll, the queue loop runs `scheduler.plan_admission` over the queue. It launches admitted `issue` items through
the launcher, under one receipt per item (`queue-admit:<item>`), so an item is never launched twice. A launched run is
linked back by issue, repository and the launched pane's own session binding. The link is explicit provenance, never
inferred from text.

### Chat

`chat_send` reaches orchestrators only. The target session must be one of the run's active orchestrator bindings,
and a live Herdr agent must be on that exact pane (its own id for a `herdr` binding, or the pane the web launcher
recorded). Worker and reviewer (`dispatch:`) targets are refused. Delivery maps landed to `completed`, held or
unconfirmed to `unknown`, and an agent gone at delivery to `failed`. A resend is a new command whose
`payload.resend_of` names the earlier chat command.

### Settings

`GET /api/settings` lists every configurable key across the tiers `default`, `machine` (user config file),
`repository` (the checkout's `.auto-office/config.yaml`) and `run-pinned` (the run's `policy_json`). Each entry has
the effective `value`, its `source` tier, per-tier `values`, `set_in`, `inherited`, `overridden`, `editable` tiers
and `apply`. `apply` is `immediate` for `scheduler.*` and `intake.*`, `before-dispatch` for `quota.*` and `roles.*`,
`restart` for `paths.*`, and `future-runs` for everything else (pinned at `office start`). Run-pinned values are
never edited.
