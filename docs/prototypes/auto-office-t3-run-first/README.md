# Auto Office — T3-style run-first UI prototype

**Canonical design ticket:** [Hikari9/auto-office #442](https://github.com/Hikari9/auto-office/issues/442)

This is an interactive, single-file HTML prototype of the approved **A — T3-style, run-first** information architecture. It is a visual/interaction reference only, not a production implementation.

## How to preview

Download or open `index.html` directly in a browser (JavaScript enabled). No build system, CDN, authentication, provider service, or local Office server is required. The reference is self-contained and offline-compatible.

Try switching the repository/run list, search, **Cmd/Ctrl+K**, inspecting the task/agent/changes/activity tabs, the plan/review/PR details modals, the Issue Inbox, global Agents, Allocation queue, Settings, and narrow/mobile drawers.

The sample composer accepts text in a **local-only demo timeline** and does not send any messages to a provider or to Auto-Office. Model/effort selectors, issue queues, pause/auto-mode toggles, PR details, agent statuses, timestamps and task updates are entirely fictional sample fixtures.

## Locked contract

- **One left-sidebar thread = one Office run**, grouped by repository.
- The selected run's verified orchestration conversation plus structured Office event timeline is the main content; there are no fictitious provider conversations.
- The contextual right panel contains Overview, Tasks, Agents, Changes and Activity.
- **Issue Inbox**, global **Agents**, **Allocation** and **Settings** remain reachable as secondary destinations.
- GitHub issues with no Office run remain in Issue Inbox; multiple runs for an issue remain distinct.
- Only a verified live orchestrator session may receive messages; workers/reviewers are inspect-only.
- Office `runs.db` owns run lifecycle; GitHub owns issue/PR status; the browser is a non-authoritative UI. No direct web land/merge/deploy.
- Remote access, desktop packaging and arbitrary shell are **not** implied by the T3-style reference.

## Reference fidelity & scope

This prototype is influenced by T3 Code's spatial hierarchy, not its brand or logos. It uses Auto-Office identity and all assets are inline. The source of truth for authority/security behavior remains the existing `docs/web-ui.md` and issue #442.

A high-fidelity mockup used during design was generated in the ChatGPT discussion, but this HTML is the inspectable interactive implementation reference. Browser screenshot approval remains a later review gate before production UI changes.
