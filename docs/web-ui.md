# Web UI

`office web` serves a local workstation over every Auto Office run on this machine: Issues, Agents, Allocation and
Settings. Start it with `office web serve` (foreground) or `office web start|stop|status` (daemon), default URL
`http://127.0.0.1:8765/`; `office web serve --fixture small` serves a synthetic demo. Screenshots at 1440x900 are in
[web-ui/screenshots](web-ui/screenshots/): [Issues](web-ui/screenshots/issues.png),
[Agents](web-ui/screenshots/agents.png), [Allocation](web-ui/screenshots/allocation.png),
[Settings](web-ui/screenshots/settings.png) and the [disconnected state](web-ui/screenshots/disconnected.png).

## Architecture

```text
browser (static ES modules, no build step)
  store.js   SSE snapshot + deltas, resync on gap/epoch     app.js  shell, Issues, banners, receipts
  agents.js / chat.js / routing.js / allocation.js / settings.js   one module per surface   scrollhint.js  sideways-scroll hint
        |  GET /api/stream (SSE)   POST /api/commands (token)   GET /api/settings, /api/activity
local service (office.web, one process, loopback only)
  observer.py  read-only runs.db        projection.py  runs, tasks, agents, routes
  github.py    issues/PRs + freshness   service.py     snapshot, deltas, command validation, queue loop
  executor.py  runs the `office` CLI    launcher.py    orchestrator in a Herdr pane (quota-aware route)
        |  reads                                  |  writes only through the office CLI and receipts
runs.db (lifecycle authority)             GitHub (issue and PR authority)
```

## Data and source of truth

- runs.db is the lifecycle authority: runs, tasks, dispatches, gates, scheduler items and command receipts. The service
  reads it through a read-only connection and writes only command receipts (`commands`). Every other mutation runs the
  `office` CLI, which records its own rows and events.
- GitHub is the issue and PR authority. Issues and PR states come from the GitHub API, each source with its own
  freshness (`fresh`, `stale`, `rate_limited`, `revoked`, `unauthenticated`, `disabled`).
- The browser is never authoritative. It mirrors the last snapshot plus deltas and shows Office and GitHub freshness;
  a value the service did not send (CPU, RAM, quota, progress, GitHub checks) is shown as unavailable.
- Weighted progress counts accepted tasks over non-cancelled tasks from runs.db. It is never time-based.

## Schema v6

Schema v6 (T2) adds `commands` (idempotent command receipts: id, kind, target, payload hash, origin, status, result),
`sched_items` (queued issues, runs and tasks with priority, pause and demotion) and `sched_state` (auto mode per
scope). `db.connect` migrates once at service start. The observer reads older schemas as they are: a missing table
turns the matching capability off instead of failing. Machine-level orchestrator launch notices are `events` rows
under the run id `office-web` (`queue.orchestrator_fallback`, `queue.orchestrator_held`).

## Compatibility

- Runs on release line 3.3 get web controls when the serving runtime has `office queue` and
  `office amend route --restart`. Runs on 3.0 (legacy), 3.1 and 3.2 are shown read-only with the reason.
- Web commands run the `office` CLI in the run's checkout, so the frontdoor pins each run to its own runtime.
- Terminal-started runs are observed exactly like web-started ones; the scheduler projects them as active work.

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

1. Shape and kind are checked first. An unknown kind gets 400 `unknown-kind`. There is no merge, land, deploy, shell
   or plan-approval kind (see "Landing stays in `office land`"). The target must match the kind's declared shape (see "Target validation"), otherwise 400
   `bad-target`.
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
   `run-terminal`, `capability-missing`, `dispatch-not-current`, `harness-mismatch`, `binding-ended`, `agent-not-live`,
   `repo-not-ready`, `repo-unknown`, `issue-unknown`, `issue-already-queued`, `item-missing`, `unknown-key`, `issue-has-live-run`,
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

There is no plan-authorization path in the browser. When a run waits for plan authorization (it has a plan and no
active plan authorization for its requirements version), its run card shows the copyable
`office approve plan --quote "<words>"` command, and `approve_plan` is refused as an unknown kind.

### Target validation

`service.TARGETS` and `service.KIND_TARGET` declare what each kind targets. `Command.parse` checks the shape (required
keys, optional keys, types, nothing else); the kind's validator then checks the thing it names against a fresh read.
Every kind first needs Office freshness `live`. A mismatch fails closed with the named reason.

| kind | target | then requires |
|---|---|---|
| `start_issue` | issue `{repo, issue}` | a discovered repository (`repo-unknown`) and known issue (`issue-unknown`), execution-ready, and no live run for the issue (`issue-has-live-run`) |
| `queue_issue` | issue `{repo, issue}` | as above, and no queue item for the issue yet (`issue-already-queued`) |
| `resume_run` / `attach_run` | run `{run_id}` | an existing non-terminal run with the capability; resume also needs no live orchestrator (`run-live`) |
| `pause` / `resume` / `set_priority` / `demote` | scheduler item `{item}` or `{run_id[, task_id]}` | an existing item (`item-missing`) or a non-terminal run with the capability, and its task; never GitHub readiness |
| `set_auto_mode` | scheduler scope `{}` or `{run_id}` | only a live Office source (a run scope also needs that run) |
| `change_route` | dispatch `{run_id, dispatch_id}` | the exact current live dispatch, the same harness, and the capability |
| `chat_send` | orchestrator session `{run_id, session[, host]}` | this host, the run's active orchestrator binding and a live Herdr agent on that pane |
| `settings_set` / `settings_unset` | settings tier `{tier, key[, repo / run_id]}` | a known key (`unknown-key`) and an editable tier: machine, or repository with an attached checkout |

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

Every orchestrator launch (start, resume and the queue loop) takes its harness from `orchestrator_route`:
`scheduler.orchestrator_route` unless that harness's quota is known to be at or below `quota.reserve_percent`, then the
first harness in `scheduler.orchestrator_fallbacks` whose quota is known to be above it. Unknown quota never falls
back. A fallback is recorded in the receipt (`result.orchestrator = {harness, fallback_from, reason}`) and as a
`queue.orchestrator_fallback` event; with no allowed fallback the queued item stays queued and a
`queue.orchestrator_held` event records why (once per reason), while a manual start is refused with
`orchestrator-quota-exhausted`. The snapshot carries the recent notices (`scalars.orchestrator_notices`): the page shows
the last hour's as banners and each item's latest reroute or hold on its Allocation row. Fixture mode never probes
quota.

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

## Verification

| suite | what it proves |
|---|---|
| `tests/web/test_api_commands.py`, `test_target_validation.py`, `test_launcher.py` | receipts, idempotency, per-kind target validation, orchestrator fallback |
| `tests/web/test_readback.py` | web pause, priority, resume and settings writes read back through `commands`, `sched_items`, `events`, `office queue list --json` and `office config --list --show-origin` (real `office` CLI) |
| `tests/web/browser/test_e2e.py` | the real `office web serve --fixture small` process in Chromium: every surface at 1440x900, 1920x1080 and 1100x800, repo switching, issue to run/PR drilldown, Start/Attach/Resume eligibility, pause, exact-target chat, duplicate and stale command refusal, GitHub revoked and rate limit, restart and resync, routing display, unavailable telemetry, the plan-approval command |
| `tests/web/test_land_via_office.py` | the command catalog has no land, merge or deploy kind (HTTP 400, no receipt); a start with end state `merge` or `e2e` reaches `office start --end-state`, which records it in the frozen requirements; `office land` before acceptance is refused (`not-ready`) |
| `tests/web/browser/test_layout.py` | DOM measurements at 1100x800, 1440x900 and 1920x1080: Issues headers never overlap, long titles and repositories end in an ellipsis with a `title`; the Allocation work cell never breaks mid-word and its Auto, Decision and Controls columns fit or scroll into view; the Agents graph's last column is visible or reachable; scroll positions survive a rebuild. Screenshots go to `$OFFICE_WEB_SHOTS` (default: the test's tmp directory) |
| `tests/web/test_perf.py` | the budgets below on `--fixture large` |

Run them with `uv run --frozen --extra test --extra visual pytest -q -m integration tests/web` (Chromium via
Playwright, or an installed Chrome).

### Wide tables and landing

Layout rules the browser tests pin down:

- Issues keeps its twelve columns in a horizontally scrolling table. Its minimum width is the sum of the column
  tracks, so the last column never spills past its row. Header labels, issue titles, repositories and owners that do
  not fit end in an ellipsis, and the full text is the element's `title`.
- Allocation tables scroll inside their own container (never the surface or the page) below about 1300 px. Their
  `Work` cell is a plain block: words wrap at spaces and never break mid-word.
- The Agents graph shows its five role columns without scrolling from 1440 px; with the inspector open or on a
  narrower window it scrolls sideways.
- Native scrollbars are hidden or overlaid on many systems, so a table or graph that scrolls sideways shows a
  hint ("More columns off-screen", with scroll left and right buttons) while columns are cut off, and no hint when
  everything fits (`scrollhint.js`). A rebuild of a surface keeps the scroll position of regions marked
  `data-scroll-key`.

#### Landing stays in `office land`

The browser starts runs and steers the scheduler; it never lands work. A start carries the operator's end state
(`preview`, `merge`, `e2e` or `ask`): the launched orchestrator is told to run
`office start --issue <url> --end-state <state>`, which freezes it in the run's requirements. Nothing in the web
service merges, lands or deploys. Landing is `office land` and takes the normal gates, whatever the end state: with
nothing accepted it is refused with `not-ready` ("nothing verified to land"), and an end state of `merge` or `e2e`
does not change that.

### Performance

Measured on an Apple M1 Pro (8 cores, 16 GB), macOS 26.6.2, Python 3.14, Playwright Chromium, `--fixture large`
(40 repositories, 2,270 issue rows, 320 runs, 3,200 tasks); three runs, median shown. Budgets in `test_perf.py` carry
3x to 6x headroom.

| measure | measured | budget |
|---|---|---|
| snapshot build (`poll(force=True)`) | 0.48 s | 2.0 s |
| snapshot payload (JSON) | 15.2 MB | 30 MB |
| delta latency (runs.db write to delta, 0.2 s poll) | 0.76 s | 2.5 s |
| first render (navigation to first issue row) | 0.67 s | 3.0 s |
| table scroll, p95 frame over 60 jumps | 7.5 ms | 40 ms |
| reconnect to resync (fresh stream to full snapshot) | 0.56 s | 3.0 s |
