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
