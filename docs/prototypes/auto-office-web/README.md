# Auto Office Web UI - three desktop explorations

Exploratory prototype for [the Web UI Wayfinder map](https://github.com/Hikari9/auto-office/issues/276) and [the layout-selection ticket](https://github.com/Hikari9/auto-office/issues/280). **No option has been approved. This is not a production Web UI.**

Download [prototype.zip](./prototype.zip), extract it, and open `index.html` in a modern desktop browser. It is a self-contained, editable HTML/CSS/JavaScript source file: no install, build, external assets, credentials or network access. The A/B/C buttons switch layouts while retaining issue context.

- **A - Operations ledger:** highest issue/run/PR density, with a persistent inspector. Recommended starting point.
- **B - Live floor:** grouped orchestrator/executor/reviewer activity; better for supervising parallel work, fewer issues per screen.
- **C - Delivery lanes:** triage, in progress, review and ready; clear flow, lower density and more horizontal space.

All issue numbers, counts, runs, PRs, activity, timestamps, host details, identity and command outputs are fictional fixtures. The public project name is used as a visual label only. No real issue, PR or agent action is performed. An active run is not necessarily a currently productive process.

## Try it

Switch repositories on the left; inspect an issue; use O/E/R or the agent selector; open a linked PR and examine its base branch, GitHub checks and Office gates separately. Only an active orchestrator has a composer. Sending produces an escaped, run-bound demo message in memory, not an actual agent message. Drafts remain bound to their original run. Refresh does not erase them. The archived run never borrows current-run evidence.

Use the top-right state selector for disconnected, source-specific GitHub failure, loading, empty and revoked-access examples. Unattached repositories are browse-only. `/` focuses search; Escape closes the inspector. Desktop widths 1440 and 1728 are primary. Narrower displays use an overlay inspector and internal scrolling.

## Verification and limits

`verification.json` inside the package records 18 passing prototype checks using Chromium. The exact HTML was loaded into an in-memory document because this container blocks file/localhost navigation. No JavaScript errors or network requests were observed. These checks do **not** validate a backend, OAuth, live telemetry, real message delivery, performance at production scale, or all accessibility requirements.

The HTML demonstrates UI behavior, not the future production architecture. It does not implement continuous event delivery, virtualized thousands-of-row lists, authentication, permissions, host discovery, real lifecycle commands, persistent storage or production secret redaction. No design, hosting, lifecycle-action or merge authority is implied by interacting with it.

## Durable package

`prototype.zip` contains the complete editable `index.html`, README, verification report and HTML checksum. Packaging is transport only; the source is ordinary unminified HTML/CSS/JavaScript. Keep this branch and package as the canonical reference for later implementation agents. No fonts or private runtime logs are included.

Research evidence is in [RESEARCH.md](./RESEARCH.md). The layout-selection issue remains open for the maintainer's actual choice.
