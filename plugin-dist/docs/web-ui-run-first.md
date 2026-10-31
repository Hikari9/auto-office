# Run-first Auto Office Workbench

The approved **A — T3-style, run-first** layout from [issue #442](https://github.com/Hikari9/auto-office/issues/442) is now implemented as a progressive web shell above the existing Office service. It uses **real Office snapshots, SSE updates, redacted Office activity and capability-gated commands**; this is no longer a mock-data HTML prototype.

## Access

The workbench is **opt-in** behind a reversible flag until maintainer visual sign-off on #442. The original Workstation is the default everywhere, including production.

- `?workbench=1` opens the run-first Workbench for that visit. Settings > Default interface (inside the workbench) stores a per-browser preference (`localStorage` key `office-workbench-mode`) so it opens by default there. `?workbench=0` clears the preference.
- `?classic=1` always opens the original Workstation, even when the preference is set. The workbench provides a **Classic controls** shortcut. Classic pages can be opened directly with `?classic=1&surface=agents`, `allocation`, `settings`, or `issues`.
- Fixture mode behaves the same (classic by default), so existing regression fixtures stay stable. Synthetic fixture data is labeled.

## Links and restore

- Selecting a run pushes a history entry, so Back and Forward move between runs. The URL fragment is `#repo=<repository key>&run=<record id>`; other fragment parameters are preserved. **Copy link** in the run header copies it.
- A link to a run this machine does not have shows a "Run not found" notice instead of another run.
- Persisted in `localStorage`: last selected run, sidebar search, Issue Inbox filter, sidebar collapse and inspector visibility. Panels are not user-resizable, so widths are not stored.
- Not implemented: pin/favorite runs and a run context menu.
- In the Issue Inbox, an issue with more than one run lists each run and asks you to choose.

## Live behavior

- Left: repositories and their **Office run identities**; unstarted issues remain in the Issue Inbox; search and `Cmd/Ctrl+K` find runs and supporting views.
- Center: selected run and its **recorded, server-redacted Office event timeline** from `/api/runs/<run_id>/activity`. This is not a reconstructed provider transcript. The composer appears only where an **exact bound orchestrator session** is supported by Office. Statuses reflect `runs.db` and service freshness; no fictional percent or model is shown.
- Right: Overview, Tasks, Agents, Changes/PRs and Activity. Office gates and GitHub checks are never conflated. Missing telemetry is displayed as unknown/unavailable.
- Global Issue Inbox/Agents/Allocation/Settings remain present, while full operational controls continue in Classic during the migration.

## Authority and failure rules

The existing service, DB schema and safety policy are unchanged: `runs.db` is canonical for run lifecycle; GitHub for issues/PRs. Browser commands carry a per-process token and idempotency keys and are re-validated by `/api/commands`; the UI never grants plan authorization, merges/lands work, deploys or opens a worker/reviewer chat channel. If Office is stale, commands are unavailable. An uncertain command outcome is **not automatically retried**.

## Next iteration

The existing read API exposes **events**, not complete provider chat transcripts, and does not provide full browser diff content. Until explicit, redacted, read-only APIs exist for these, the workbench intentionally shows events and linked GitHub PRs instead of invented transcript/diff content. The classic allocation and configuration controls remain available while the dedicated new shell screens mature.