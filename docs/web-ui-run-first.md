# Run-first Auto Office Workbench

The approved **A — T3-style, run-first** layout from [issue #442](https://github.com/Hikari9/auto-office/issues/442) is now implemented as a progressive web shell above the existing Office service. It uses **real Office snapshots, SSE updates, redacted Office activity and capability-gated commands**; this is no longer a mock-data HTML prototype.

## Access

- Production `office web serve` or `office web start` defaults to the new **run-first Workbench**.
- `?classic=1` opens the original Workstation (Issues, Agents, Allocation, Settings). The new shell provides a **Classic controls** shortcut; classic pages can be opened directly with `?classic=1&surface=agents`, `allocation`, `settings`, or `issues`.
- Fixture mode defaults to **classic** so existing regression fixtures stay stable. Visit `?workbench=1` while running `office web serve --fixture small` to inspect and test the new run-first shell. Synthetic fixture data is labeled.

## Live behavior

- Left: repositories and their **Office run identities**; unstarted issues remain in the Issue Inbox; search and `Cmd/Ctrl+K` find runs and supporting views.
- Center: selected run and its **recorded, server-redacted Office event timeline** from `/api/runs/<run_id>/activity`. This is not a reconstructed provider transcript. The composer appears only where an **exact bound orchestrator session** is supported by Office. Statuses reflect `runs.db` and service freshness; no fictional percent or model is shown.
- Right: Overview, Tasks, Agents, Changes/PRs and Activity. Office gates and GitHub checks are never conflated. Missing telemetry is displayed as unknown/unavailable.
- Global Issue Inbox/Agents/Allocation/Settings remain present, while full operational controls continue in Classic during the migration.

## Authority and failure rules

The existing service, DB schema and safety policy are unchanged: `runs.db` is canonical for run lifecycle; GitHub for issues/PRs. Browser commands carry a per-process token and idempotency keys and are re-validated by `/api/commands`; the UI never grants plan authorization, merges/lands work, deploys or opens a worker/reviewer chat channel. If Office is stale, commands are unavailable. An uncertain command outcome is **not automatically retried**.

## Next iteration

The existing read API exposes **events**, not complete provider chat transcripts, and does not provide full browser diff content. Until explicit, redacted, read-only APIs exist for these, the workbench intentionally shows events and linked GitHub PRs instead of invented transcript/diff content. The classic allocation and configuration controls remain available while the dedicated new shell screens mature.