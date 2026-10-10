# Routing evidence report

`scripts/routing_evidence_report.py` turns one `runs.db` into aggregate counts that say how executor routes behaved: which routes ran, how often a task landed on the route it started on, how many revisions it took, and what the reviewers saw. It is the read side of the routing-evidence instrumentation (#526, #527, #528): the dispatch, task, gate and finding columns written by the earlier PRs of the stack are read here, and nothing in routing, scoring, trust or the learner reads this report.

```bash
scripts/routing_evidence_report.py --db /tmp/runs-copy.db            # text
scripts/routing_evidence_report.py --db /tmp/runs-copy.db --format json
```

Exit codes: `0` report printed, `1` printed but `PRAGMA integrity_check` failed, `2` the file is missing or not a database, the SQLite is older than 3.25 (JSON1 and window functions are needed), or an unexpected error occurred (only its type is printed, never a traceback). The `episodes` section imports `office.route_learning`; run the script with the project's Python (or from a checkout, where `src/` is added to the path) or that section reports itself unavailable. A WAL-mode database in a read-only directory cannot be opened read-only unless its `-wal` and `-shm` files already exist: copy the three files to a writable directory.

## What it guarantees

- **Read only.** The file is opened with a SQLite URI `mode=ro`, then `PRAGMA query_only=ON`, through `sqlite3.connect` directly. It never calls `office.db.connect`, which migrates and enters WAL mode. A read-only open of a WAL database can create empty `-wal` and `-shm` files beside it; the main file's bytes are not changed. The test suite proves the source hash and modification time are unchanged.
- **Aggregate only.** Only counts, dates (day precision) and categorical values are printed. No goal, title, prompt, path, summary, commit, token, run id, task id or dispatch id is selected, and the source path is not printed. Categorical values (tags, sizes, receipt kinds, attribution bases) are clamped to their documented sets, and role, harness, model, effort, run phase and task status must look like an identifier (`LABELS` in the script: lowercase words, a model may have one `/`); anything else prints as `other`, so free text, paths or control characters in those columns cannot reach the report. A malformed row degrades the section that reads it to `available: false` with the exception type, never the report.
- **Unknown stays unknown.** A column the database lacks reads as NULL, a NULL is reported as `unknown` (or `not-recorded`), and nothing is backfilled or inferred from timestamps. A table the database lacks makes the sections that need it `available: false` with the table names, and `schema_gaps_read_as_unknown` lists every `table.column` read as NULL.
- **Missing cost is unknown.** A route with no recorded cost prints `total: unknown`, never `0`.

## Provenance

The `source` block is not SQL. It prints: `sha256` of the main file before the first query and `sha256_after` after the last (`hash_unchanged` is their equality), `size_bytes`, `integrity_check` (`ok`, or the first five messages), `schema_version` (`schema_meta` key `office_schema`, `absent` if there is none), `journal_mode`, whether `-wal` and `-shm` files sat beside it before the report opened it (`side_files_created_by_report` says whether opening left new ones), and `run_date_range` (see `runs.span`). The statements it runs are `PRAGMA integrity_check`, `PRAGMA journal_mode`, `PRAGMA table_info(<table>)` for the tables below, and:

```sql
SELECT value FROM schema_meta WHERE key = 'office_schema'
```

Caveats printed with every report:

- sha256 and size_bytes cover the main database file only. Committed frames still in a -wal file are read by the report but are not covered by the hash: copy the .db, -wal and -shm files together, or checkpoint first, for a point-in-time snapshot.
- The report cannot tell whether this file is the live runs.db or a copy, when it was taken, or what was pruned before it: population.runs.pruned counts runs Office itself marked pruned, nothing else.
- A NULL column is unknown, never a default: runs recorded before an evidence column existed stay unknown.

## How the SQL is organised

Every metric is one statement appended to the same prelude of normalized views. The prelude is the text below for a database with every column; where a column is absent the view selects `NULL AS <column>` and where a table is absent the view has no rows. The route columns `harness`, `model` and `effort` fall back to the dispatch's `harness@version/model@effort` triple, split at the first `/` and the last `@`, as `route_learning` splits it. A route key is `harness/model@effort` and is NULL when harness, model or effort is unknown, so a route with an unknown part is never equal to another and is counted as unknown, not as a mismatch.

A metric whose SQL starts with a comma adds CTEs of its own after the prelude. `tests/v31/test_routing_evidence_report.py` fails if this page and the script differ.

```sql
WITH dr AS (SELECT p.*, CASE WHEN p.harness IS NOT NULL AND p.model IS NOT NULL AND p.effort IS NOT NULL
    THEN p.harness || '/' || p.model || '@' || p.effort END AS route FROM (SELECT d.id, d.run_id,
    d.role, d.task_id, d.started_at, d.ended_at, d.wall_clock_seconds, d.money_actual, d.size_class,
    d.predecessor_dispatch_id, d.descriptor_json, COALESCE(NULLIF(d.harness, ''), CASE WHEN
    instr(d.triple, '/') > 0 THEN CASE WHEN instr(substr(d.triple, 1, instr(d.triple, '/') - 1), '@') >
    0 THEN substr(d.triple, 1, instr(d.triple, '@') - 1) ELSE substr(d.triple, 1, instr(d.triple, '/') -
    1) END END) AS harness, COALESCE(NULLIF(d.model, ''), CASE WHEN instr(d.triple, '/') > 0 AND
    length(rtrim(d.triple, replace(d.triple, '@', ''))) > instr(d.triple, '/') THEN substr(d.triple,
    instr(d.triple, '/') + 1, length(rtrim(d.triple, replace(d.triple, '@', ''))) - instr(d.triple, '/')
    - 1) END) AS model, COALESCE(NULLIF(d.effort, ''), CASE WHEN instr(d.triple, '/') > 0 AND
    length(rtrim(d.triple, replace(d.triple, '@', ''))) > instr(d.triple, '/') THEN
    NULLIF(substr(d.triple, length(rtrim(d.triple, replace(d.triple, '@', ''))) + 1), '') END) AS effort
    FROM dispatches d) p),
tk AS (SELECT t.run_id, t.id AS task_id, t.status, t.accepted_revision_id, t.descriptor_json,
    t.first_executor_dispatch_id, CASE WHEN t.status = 'accepted' AND t.accepted_revision_id IS NOT NULL
    THEN 1 ELSE 0 END AS is_accepted FROM tasks t),
rv AS (SELECT r.id, r.run_id, r.task_id, r.seq, r.dispatch_id, r.self_review_json FROM revisions r),
gt AS (SELECT g.id, g.run_id, g.subject, g.scope, CASE WHEN json_valid(g.members_json) THEN CASE WHEN
    json_type(g.members_json) = 'array' THEN g.members_json END END AS members_json FROM gates g),
fd AS (SELECT f.id, f.run_id, f.gate_id, f.code, f.contract, f.attribution_basis, f.attributed_task,
    f.clone_of FROM findings f),
rn AS (SELECT r.id, r.created_at, r.phase, r.risk_json, r.pruned_at FROM runs r),
tr AS (SELECT t.run_id, t.task_id, t.is_accepted, fe.id AS first_id, fe.route AS first_route, pd.id AS
    producer_id, pd.route AS producer_route FROM tk t LEFT JOIN dr fe ON fe.id =
    t.first_executor_dispatch_id AND fe.run_id = t.run_id AND fe.task_id = t.task_id AND fe.role =
    'executor' LEFT JOIN rv ar ON ar.id = t.accepted_revision_id AND ar.run_id = t.run_id AND ar.task_id
    = t.task_id LEFT JOIN dr pd ON pd.id = ar.dispatch_id AND pd.run_id = t.run_id AND pd.task_id =
    t.task_id),
ed AS (SELECT d.run_id, d.task_id, d.id AS to_id, p.route AS from_route, d.route AS to_route FROM dr d
    JOIN dr p ON p.id = d.predecessor_dispatch_id AND p.run_id = d.run_id AND p.task_id = d.task_id AND
    p.role = 'executor' AND p.id <> d.id WHERE d.role = 'executor')
```

## Metrics

Each entry gives the metric id, the denominator, the SQL (run after the prelude) and how it is reported. Percentages are not printed: divide a count by the denominator named here.

### `runs.span`

**Denominator:** every row of runs.

**Reported as:** `sections.population.runs`, `source.run_date_range`.

```sql
SELECT COUNT(*) AS runs, MIN(substr(created_at, 1, 10)) AS first_day, MAX(substr(created_at, 1, 10)) AS
    last_day, COALESCE(SUM(pruned_at IS NOT NULL), 0) AS pruned FROM rn
```

`pruned` counts runs Office marked `pruned_at`; the report cannot see anything pruned before the file was copied.

### `runs.by_phase`

**Denominator:** every row of runs.

**Reported as:** `sections.population.runs_by_phase`.

```sql
SELECT COALESCE(phase, 'unknown') AS phase, COUNT(*) AS runs FROM rn GROUP BY 1 ORDER BY 1
```



### `size.run`

**Denominator:** every row of runs; the size is the run's risk size_class.

**Reported as:** `sections.size.run_size`.

```sql
SELECT COALESCE(CASE WHEN json_valid(risk_json) THEN json_extract(risk_json, '$.size_class') END,
    'unknown') AS size, COUNT(*) AS runs FROM rn GROUP BY 1 ORDER BY 1
```

Run size is the run's own risk record. It is not task size and is never used as one.

### `tasks.by_status`

**Denominator:** every row of tasks.

**Reported as:** `sections.population.tasks_by_status`.

```sql
SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS tasks FROM tk GROUP BY 1 ORDER BY 1
```

Accepted tasks are the `accepted` rows; every other status is unresolved in the sections below.

### `size.task`

**Denominator:** every row of tasks; the size is the planner's task_size in the task descriptor.

**Reported as:** `sections.size.task_size`.

```sql
SELECT COALESCE(CASE WHEN json_valid(descriptor_json) THEN json_extract(descriptor_json, '$.task_size')
    END, 'unknown') AS size, COUNT(*) AS tasks FROM tk GROUP BY 1 ORDER BY 1
```

The planner's `task_size` as it stands on the task now. A value outside S, M, L, XL is reported as `other`; no descriptor or no key is `unknown`.

### `size.dispatch_snapshot`

**Denominator:** every executor dispatch; the size is dispatches.size_class, the task size snapshotted at launch.

**Reported as:** `sections.size.dispatch_task_size_snapshot`.

```sql
SELECT COALESCE(harness, 'unknown') AS harness, COALESCE(model, 'unknown') AS model, COALESCE(effort,
    'unknown') AS effort, COALESCE(size_class, 'unknown') AS size, COUNT(*) AS dispatches FROM dr WHERE
    role = 'executor' GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
```

The task size copied onto the executor dispatch when it launched. A dispatch launched before that existed has NULL, shown as `unknown`; the report cannot tell a 3.0 recorder row that used the column for something else, so read it only for runs that recorded it.

### `dispatch.routes`

**Denominator:** every row of dispatches; terminal = ended_at is set, pending = it is not.

**Reported as:** `sections.dispatches.routes`.

```sql
SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, COALESCE(model,
    'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS dispatches,
    COALESCE(SUM(ended_at IS NOT NULL), 0) AS terminal, COALESCE(SUM(ended_at IS NULL), 0) AS pending
    FROM dr GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
```

Role, harness, model and effort are the dispatch's columns, each falling back to the `harness@version/model@effort` triple. A dispatch with neither is `unknown`. Terminal and pending are the only statuses: a dispatch that ended is terminal whatever its outcome.

### `dispatch.strict_successes`

**Denominator:** executor and worker dispatches; a success is a dispatch whose revision is its task's accepted_revision_id (route_learning's success definition, ended or not).

**Reported as:** `sections.dispatches.routes[].strict_successes`.

```sql
SELECT d.role AS role, COALESCE(d.harness, 'unknown') AS harness, COALESCE(d.model, 'unknown') AS model,
    COALESCE(d.effort, 'unknown') AS effort, COUNT(DISTINCT d.id) AS strict_successes FROM tk t JOIN rv
    r ON r.id = t.accepted_revision_id AND r.run_id = t.run_id AND r.task_id = t.task_id JOIN dr d ON
    d.id = r.dispatch_id AND d.run_id = t.run_id AND d.task_id = t.task_id WHERE d.role IN ('executor',
    'worker') GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
```

Raw dispatches, not episodes: a task that needed three dispatches on one route and landed on the third has one strict success here. The definition is the one `route_learning.derive_outcomes` uses for `success`, without its settled-and-ended filter. `null` for roles that never produce a revision, and when the tasks or revisions table is absent.

### `dispatch.latency`

**Denominator:** terminal dispatches (ended_at set); recorded = wall_clock_seconds, else ended_at - started_at, when it is not negative; the rest are unknown.

**Reported as:** `sections.latency_and_cost.latency`.

```sql
, lat AS (SELECT role, harness, model, effort, CASE WHEN COALESCE(wall_clock_seconds,
    (julianday(ended_at) - julianday(started_at)) * 86400.0) >= 0 THEN COALESCE(wall_clock_seconds,
    (julianday(ended_at) - julianday(started_at)) * 86400.0) END AS secs FROM dr WHERE ended_at IS NOT
    NULL), rk AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY role, harness, model, effort ORDER BY secs
    IS NULL, secs) AS pos, COUNT(secs) OVER (PARTITION BY role, harness, model, effort) AS n FROM lat)
    SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, COALESCE(model,
    'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS terminal, COUNT(secs) AS
    recorded, COUNT(*) - COUNT(secs) AS unknown, AVG(secs) AS mean_seconds, AVG(CASE WHEN secs IS NOT
    NULL AND pos IN ((n + 1) / 2, (n + 2) / 2) THEN secs END) AS median_seconds, MAX(secs) AS
    max_seconds FROM rk GROUP BY role, harness, model, effort ORDER BY 1, 2, 3, 4
```

Latency is the recorded `wall_clock_seconds`, else the ended_at minus started_at difference, and only when it is not negative. Dispatches with neither are `unknown`, never zero. The median of an even count is the mean of the two middle values.

### `dispatch.cost`

**Denominator:** terminal dispatches (ended_at set); recorded = money_actual is not NULL, the rest are unknown.

**Reported as:** `sections.latency_and_cost.cost`.

```sql
SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, COALESCE(model,
    'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS terminal,
    COUNT(money_actual) AS recorded, COUNT(*) - COUNT(money_actual) AS unknown, SUM(money_actual) AS
    total FROM dr WHERE ended_at IS NOT NULL GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
```

Cost is `money_actual` as the harness adapter recorded it; the unit is not recorded. `total` is `unknown` when no terminal dispatch of the route recorded one, and is a partial sum when some did (see `unknown`).

### `task.accepted_paths`

**Denominator:** accepted tasks (status accepted with an accepted_revision_id); first executor is tasks.first_executor_dispatch_id, accepted producer is the dispatch of the accepted revision.

**Reported as:** `sections.task_paths.accepted`.

```sql
SELECT COUNT(*) AS accepted_tasks, COALESCE(SUM(first_id IS NULL), 0) AS first_executor_unknown,
    COALESCE(SUM(producer_id IS NULL), 0) AS producer_unknown, COALESCE(SUM(first_id IS NOT NULL AND
    producer_id IS NOT NULL), 0) AS comparable, COALESCE(SUM(first_id = producer_id), 0) AS
    first_executor_is_producer, COALESCE(SUM(first_id <> producer_id AND first_route IS NOT NULL AND
    first_route = producer_route), 0) AS later_dispatch_same_route, COALESCE(SUM(first_id <> producer_id
    AND first_route IS NOT NULL AND producer_route IS NOT NULL AND first_route <> producer_route), 0) AS
    first_final_mismatch, COALESCE(SUM(first_id <> producer_id AND (first_route IS NULL OR
    producer_route IS NULL)), 0) AS route_unknown FROM tr WHERE is_accepted = 1
```

First executor is read only from `tasks.first_executor_dispatch_id`: a NULL there is unknown, never inferred from timestamps. The accepted producer is the dispatch of the task's accepted revision, which is also provable on older databases. `first_final_mismatch` compares route keys (harness, model, effort), so a different dispatch on the same route is `later_dispatch_same_route`.

### `task.handoffs`

**Denominator:** tasks, split into accepted and unresolved; an edge is an executor dispatch whose recorded predecessor is an executor dispatch of the same run and task.

**Reported as:** `sections.task_paths.handoffs_by_task_state`.

```sql
, tc AS (SELECT tr.run_id, tr.task_id, tr.is_accepted, tr.first_route, tr.producer_route,
    COALESCE(SUM(ed.from_route IS NOT NULL AND ed.to_route IS NOT NULL AND ed.from_route = ed.to_route),
    0) AS same_route_retries, COALESCE(SUM(ed.from_route IS NOT NULL AND ed.to_route IS NOT NULL AND
    ed.from_route <> ed.to_route), 0) AS cross_route_handoffs, COALESCE(SUM(ed.from_route IS NOT NULL
    AND ed.to_route IS NOT NULL AND ed.from_route <> ed.to_route AND ed.to_id IS NOT tr.producer_id), 0)
    AS intermediate_cross_route_handoffs, COALESCE(SUM(ed.to_id IS NOT NULL AND (ed.from_route IS NULL
    OR ed.to_route IS NULL)), 0) AS route_unknown_edges FROM tr LEFT JOIN ed ON ed.run_id = tr.run_id
    AND ed.task_id = tr.task_id GROUP BY tr.run_id, tr.task_id) SELECT CASE WHEN is_accepted = 1 THEN
    'accepted' ELSE 'unresolved' END AS task_state, COUNT(*) AS tasks, SUM(same_route_retries) AS
    same_route_retries, SUM(same_route_retries > 0) AS tasks_with_same_route_retry,
    SUM(cross_route_handoffs) AS cross_route_handoffs, SUM(cross_route_handoffs > 0) AS
    tasks_with_cross_route_handoff, SUM(intermediate_cross_route_handoffs) AS
    intermediate_cross_route_handoffs, SUM(cross_route_handoffs > 0 AND first_route IS NOT NULL AND
    first_route = producer_route) AS swap_away_and_back_tasks, SUM(route_unknown_edges) AS
    route_unknown_handoffs FROM tc GROUP BY 1 ORDER BY 1
```

An edge is one executor dispatch and its recorded predecessor, both executor dispatches of the same run and task. `same_route_retries` are edges whose routes match, `cross_route_handoffs` edges whose routes differ, and `intermediate_cross_route_handoffs` the cross-route edges that do not land on the accepted producer. `swap_away_and_back_tasks` have a first route equal to the producer's route and at least one cross-route handoff: the first/final comparison alone hides them. An edge with an unrecorded route is `route_unknown_handoffs`.

### `task.links`

**Denominator:** every executor dispatch; first = it is its task's first_executor_dispatch_id, linked = it has a valid recorded predecessor, unknown = neither.

**Reported as:** `sections.task_paths.executor_dispatch_links`.

```sql
SELECT COUNT(*) AS executor_dispatches, COALESCE(SUM(t.first_executor_dispatch_id = d.id), 0) AS first,
    COALESCE(SUM(d.id IN (SELECT to_id FROM ed)), 0) AS linked,
    COALESCE(SUM(COALESCE(t.first_executor_dispatch_id <> d.id, 1) AND d.id NOT IN (SELECT to_id FROM
    ed)), 0) AS unknown FROM dr d LEFT JOIN tk t ON t.run_id = d.run_id AND t.task_id = d.task_id WHERE
    d.role = 'executor'
```

Says how much of the handoff graph is knowable: `unknown` executor dispatches are neither a task's recorded first executor nor linked to a predecessor, so their handoffs are not in the counts above.

### `task.revisions_to_accept`

**Denominator:** accepted tasks: revisions of the task up to and including the accepted one (NULL = the accepted revision row is missing); unresolved tasks: revisions submitted so far, shown apart.

**Reported as:** `sections.task_paths.revisions_to_accept`, `sections.task_paths.unresolved_revisions_so_far`.

```sql
SELECT 'accepted' AS task_state, n AS revisions, COUNT(*) AS tasks FROM (SELECT t.run_id, t.task_id,
    CASE WHEN ar.id IS NULL THEN NULL ELSE COUNT(r.id) END AS n FROM tk t LEFT JOIN rv ar ON ar.id =
    t.accepted_revision_id AND ar.run_id = t.run_id AND ar.task_id = t.task_id LEFT JOIN rv r ON
    r.run_id = t.run_id AND r.task_id = t.task_id AND r.seq <= ar.seq WHERE t.is_accepted = 1 GROUP BY
    t.run_id, t.task_id) GROUP BY n UNION ALL SELECT 'unresolved', n, COUNT(*) FROM (SELECT t.run_id,
    t.task_id, COUNT(r.id) AS n FROM tk t LEFT JOIN rv r ON r.run_id = t.run_id AND r.task_id =
    t.task_id WHERE t.is_accepted = 0 GROUP BY t.run_id, t.task_id) GROUP BY n ORDER BY 1, 2
```

`mean` and `median` are taken over the histogram. Unresolved tasks (any status other than accepted, including cancelled) are shown apart and never enter the accepted mean.

### `self_review`

**Denominator:** every row of revisions; missing = self_review_json is NULL.

**Reported as:** `sections.self_review.by_receipt`.

```sql
SELECT CASE WHEN r.self_review_json IS NULL THEN 'missing' WHEN NOT json_valid(r.self_review_json) THEN
    'unreadable' ELSE COALESCE(json_extract(r.self_review_json, '$.kind'), 'unreadable') END AS kind,
    CASE WHEN json_valid(r.self_review_json) THEN json_extract(r.self_review_json, '$.type') END AS
    exempt_type, COUNT(*) AS revisions, COALESCE(SUM(r.id IN (SELECT accepted_revision_id FROM tk WHERE
    is_accepted = 1)), 0) AS accepted_revisions FROM rv r GROUP BY 1, 2 ORDER BY 1, 2
```

`kind` is the receipt's own `kind` (`unknown` for every revision when the database has no `self_review_json` column); `exempt_type` is its `type` when exempt (`read-only`, `empty`, `trivial`, `mechanical`, else `other`). `missing` is a NULL receipt: a revision recorded before receipts existed, or by a run that did not enforce them. `unreadable` is a receipt that is not JSON or has no kind.

### `lanes.exposure`

**Denominator:** gates with subject lane; a pair is one distinct (run, lane scope, member task) from the frozen members_json (distinct text entries of a JSON array); any other members_json is unknown membership.

**Reported as:** `sections.findings.lanes`.

```sql
, lm AS (SELECT DISTINCT g.run_id, COALESCE(g.scope, '') AS scope, j.value AS task_id FROM gt g,
    json_each(COALESCE(g.members_json, '[]')) j WHERE g.subject = 'lane' AND j.type = 'text') SELECT
    (SELECT COUNT(*) FROM gt WHERE subject = 'lane') AS lane_gates, (SELECT COUNT(*) FROM gt WHERE
    subject = 'lane' AND members_json IS NULL) AS membership_unknown_gates, (SELECT COUNT(DISTINCT
    run_id || char(31) || COALESCE(scope, '')) FROM gt WHERE subject = 'lane') AS lanes, (SELECT
    COUNT(*) FROM lm) AS lane_task_pairs, (SELECT COUNT(DISTINCT run_id || char(31) || task_id) FROM lm)
    AS exposed_tasks, (SELECT COUNT(*) FROM (SELECT 1 FROM lm GROUP BY run_id, task_id HAVING COUNT(*) >
    1)) AS tasks_in_several_lanes
```

A lane gate is one review round of one lane or shared scope. Only a JSON array of text ids is a membership: NULL, an object, a scalar or malformed JSON is unknown membership, and duplicate or non-text entries are not members. A lane with no scope is one lane with an empty scope name. `lane_task_pairs` count each (run, lane, task) once however many rounds reviewed it. `tasks_in_several_lanes` are tasks that are members of more than one lane.

### `findings.attribution`

**Denominator:** convergence-v1 findings, one per (run, gate, code): the rows recorded once per repair owner count once; basis NULL = recorded before attribution existed.

**Reported as:** `sections.findings.by_attribution`, `sections.findings.totals`.

```sql
, uf AS (SELECT f.run_id, f.gate_id, f.code, COUNT(*) AS recorded_rows, COALESCE(SUM(f.clone_of IS NOT
    NULL), 0) AS clone_rows, MIN(f.attribution_basis) AS basis, MIN(g.members_json) AS members_json FROM
    fd f LEFT JOIN gt g ON g.id = f.gate_id WHERE f.contract = 'convergence-v1' AND f.gate_id IS NOT
    NULL GROUP BY f.run_id, f.gate_id, f.code), um AS (SELECT uf.*, (SELECT COUNT(DISTINCT j.value) FROM
    json_each(COALESCE(uf.members_json, '[]')) j WHERE j.type = 'text') AS members FROM uf) SELECT
    COALESCE(basis, 'unknown') AS basis, COUNT(*) AS unique_findings, SUM(recorded_rows) AS
    recorded_rows, SUM(recorded_rows) - COUNT(*) AS duplicate_rows, SUM(clone_rows) AS
    clone_marked_rows, SUM(members) AS lane_exposure, SUM(members_json IS NULL) AS membership_unknown
    FROM um GROUP BY 1 ORDER BY 1
```

A finding is one (run, gate, code): a finding recorded once per repair owner has several rows and counts once, with or without `clone_of`, so historical duplicates also count once. `duplicate_rows` is rows beyond the first, `clone_marked_rows` those carrying `clone_of`. `lane_exposure` is the number of member tasks the finding's gate put in front of it, so it is the count of tasks exposed, against `attributed_findings` which name one. `basis` is the first row's recorded `attribution_basis`; NULL is `unknown`.

### `tags.tasks`

**Denominator:** every row of tasks, once per tag; not-recorded = the descriptor has no such key.

**Reported as:** `sections.tags.tasks`.

```sql
SELECT k.key AS tag, CASE WHEN t.descriptor_json IS NULL THEN 'descriptor-null' WHEN NOT
    json_valid(t.descriptor_json) THEN 'descriptor-unreadable' WHEN json_extract(t.descriptor_json, '$.'
    || k.key) IS NULL THEN 'not-recorded' ELSE CAST(json_extract(t.descriptor_json, '$.' || k.key) AS
    TEXT) END AS value, COUNT(*) AS tasks FROM tk t, (SELECT 'evidence_domain' AS key UNION ALL SELECT
    'intent' UNION ALL SELECT 'difficulty_estimate' UNION ALL SELECT 'brief_shape') k GROUP BY 1, 2
    ORDER BY 1, 2
```

`declared` counts tasks with a value for the tag (an explicit `unknown` is a declared value). `unknown_not_recorded` is the sum of `descriptor-null` (historical), `descriptor-unreadable` and `not-recorded` (the descriptor has no such key). A value outside the tag's documented set is `other`.

### `tags.dispatches`

**Denominator:** every executor dispatch, once per tag; the descriptor is the one snapshotted at launch.

**Reported as:** `sections.tags.executor_dispatches`.

```sql
SELECT k.key AS tag, CASE WHEN d.descriptor_json IS NULL THEN 'descriptor-null' WHEN NOT
    json_valid(d.descriptor_json) THEN 'descriptor-unreadable' WHEN json_extract(d.descriptor_json, '$.'
    || k.key) IS NULL THEN 'not-recorded' ELSE CAST(json_extract(d.descriptor_json, '$.' || k.key) AS
    TEXT) END AS value, COUNT(*) AS dispatches FROM dr d, (SELECT 'evidence_domain' AS key UNION ALL
    SELECT 'intent' UNION ALL SELECT 'difficulty_estimate' UNION ALL SELECT 'brief_shape') k WHERE
    d.role = 'executor' GROUP BY 1, 2 ORDER BY 1, 2
```

The same counts over the descriptor snapshotted onto each executor dispatch.

### `episodes`

Accepted task-route episodes are not SQL. They come from `office.route_learning.derive_outcomes` and `route_learning.episodes`, which only `SELECT` (the report's connection is `query_only`, so a write would raise, and the tests prove the file hash is unchanged). **Denominator:** executor and worker dispatches that have ended and whose task has since moved on (accepted, cancelled, relaunched) or whose run ended (`settled_dispatches`). An **episode** is one (run, task, route): a task that needed a fix round on the same route is one episode with two attempts. `accepted_episodes` is episodes where one dispatch landed; the rest are `failed_episodes`, split by the learner's attribution class in `failed_by_attribution`. `multi_attempt_episodes` counts episodes with more than one dispatch, which is where strict dispatch successes and episodes diverge. Reported as `sections.episodes.routes` and `totals`; the section is unavailable when the `office` package cannot be imported or the dispatches, tasks, revisions or outcome_labels table is absent. A dispatch with no recorded harness or model is not an outcome, as in the learner.

### `provenance`

See the Provenance section above.

## Reading it

- **Strict successes vs episodes.** `dispatch.strict_successes` is the raw count of dispatches whose revision was accepted. `episodes` counts task-route pairs. Compare them per route: where a route has retries, episodes are fewer than settled dispatches, and a route that was handed a task and then lost it has a failed episode without a failed task.
- **Task size vs run size.** `size.task` is what the planner said about each task, `size.dispatch_snapshot` what was copied onto its executor dispatch at launch, and `size.run` the run's risk size. They are three different questions and are printed under three labels.
- **Lane exposure vs findings.** A finding in a shared reviewed scope is in front of every member task (`lane_exposure`). Attribution (`reviewer-declared`, `unique-path`) names one task; `unassigned` could not. Attribution is evidence only: it did not decide which producer repaired the finding.
- **Historical NULL.** Every evidence column was added after runs existed. Runs from before it read as `unknown` in first executor, links, attribution and tags; the report does not guess.
