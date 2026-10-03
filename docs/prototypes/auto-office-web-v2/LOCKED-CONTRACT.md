# Locked Auto Office Web UI prototype contract

Status: **LOCKED by maintainer on 2026-10-03** for implementation reference.

Canonical shell: **Workstation (Option 2 / B)**.

## Visual system

- Desktop-first, dark workstation shell.
- Permanent narrow product rail, then repository rail, then working canvas.
- Accent palette: **blue + green + purple**.
- Yellow and red are reserved for semantic warning/error states.
- Dense, operational, tool-like UI rather than decorative dashboard styling.

## Primary surfaces

### Issues — default home
- Dense table, not kanban lanes.
- Show repository, issue, Office owner/orchestrator, phase, weighted task progress, priority, authorization, PR/run links, and queue/run state.
- An issue without a run exposes a primary copyable `office start ...` action.
- Auto Queue is explicit authorization for unattended local launch once capacity permits.
- Authorization inherits repository defaults, with `Preview only` as machine fallback, and may be overridden per queued issue to supported targets such as `PR / no merge`, `Merge to main`, or `Production`.

### Agents — workstation graph
- Machine-wide graph with repository and status filters.
- Role columns: Orchestrators → Plan Reviewers → Executors → Code Reviewers → Visual Verifiers.
- Default graph shows current topology; historical dispatches/retries live in run history rather than cluttering the graph.
- Nodes expose truthful state, harness, model, effort, CPU/RAM where measurable, context/quota where available, and current work.
- Only orchestrators expose chat.
- Advanced shell access should hand off to the existing Herdr pane rather than expose an unrestricted browser shell by default.
- Before a dispatch starts, Auto Routing may choose harness/provider/model/effort according to planner classification and fallback policy.
- Once an agent is running, harness stays fixed; changing model + effort interrupts and restarts that current model work while preserving durable run/worktree state.

### Allocation — machine scheduler
- One machine-wide scheduler across all Auto Office runs, including runs started from terminal sessions.
- Auto mode is on by default, manually overridable.
- Scheduler should behave like a resource-aware preemptive CPU scheduler: ready queue, parallel admission, weighted priorities, aging, dependency/critical-path boosts, preemption, CPU/RAM pressure feedback, and provider quota/headroom constraints.
- Manual Pause means demote that work toward the end of the queue. If it becomes the final runnable work in that session, that session's auto mode remains paused until explicitly resumed.
- Active orchestrators and work that unblocks dependency chains get protected priority; idle/already-paused agents should not consume active capacity.
- Provider fallback is automatic when already allowed by policy: e.g. AGY quota exhausted → Claude fallback instead of unnecessary delay. The reroute must produce a visible notification and durable run/card history entry.
- The UI may show a derived recommended concurrency/load only when supported by observed machine data; never hardcode a fake optimal agent count.
- CPU/RAM capacity and provider/account quota are separate resources.

### Settings
- Cascade Machine → Repository → Run.
- Show the source of each effective value.
- Planner classification is automatic but overridable before launch.
- Auto Routing maps classification to allowed routes/fallbacks; users should not have to manually route every run.
- Auto Intake repository defaults include authorization behavior and whether queued work may proceed to Ready to Land. Actual merge/landing authority remains explicit policy.

## Interaction and state rules

- Local-only control surface; queued scheduling continues through a lightweight local background Office service even if the browser closes.
- Observe all local Auto Office runs, including terminal-started runs.
- `runs.db` remains lifecycle authority; GitHub remains issue/PR authority; browser state is non-authoritative.
- Preserve source freshness boundaries and explicit stale/disconnected/unavailable states.
- Do not fabricate CPU, RAM, context, quota, or progress when telemetry is unavailable.
- Weighted progress may be shown only from real task/phase structure; it must not be an elapsed-time guess.
- Reroutes/fallbacks are both immediately notified and inspectable later from the relevant run/agent card.
- Issue drafts/commands and orchestrator chat must remain bound to the exact selected run/session and must never silently retarget.

## Responsive behavior

- Primary target: 1440px+ desktop widths.
- At narrower desktop widths, preserve table usability with horizontal/internal scrolling and reduce secondary graph columns rather than compressing everything into unreadable cards.
- Mobile-first redesign is out of scope for this effort; small screens require only a graceful fallback.

## Implementation authority

This artifact is the canonical visual and interaction reference for implementation unless a later explicit decision supersedes it. It does not itself authorize production deployment, agent execution, merge, or destructive actions.
