# Auto Office Web UI v2 — refined prototype options

This is a local, fictional/sanitized prototype shaped by real Auto Office v3 run artifacts. It does not perform any real GitHub, Auto Office, Herdr, provider, merge, production, or shell mutation.

## Locked product direction reflected here

- C-inspired dark visual language, but an **issue-first table**, not delivery columns.
- **Issues** is the home surface. New issues have a primary copyable `office start ...` command and an explicit Auto Queue checkbox. Running rows expose owner, phase, weighted task progress, priority and authorization.
- **Agents** is a separate machine-wide graph grouped into actual roles: Orchestrators, Plan Reviewers, Executors, Code Reviewers and Visual Verifiers. The graph can be filtered by repository and run status. Historical dispatches are intentionally hidden from the default topology and remain run history.
- **Allocation** is a machine-wide, CPU-scheduler-like control plane. Auto mode is on by default, with a resource-aware ready queue, parallel admission, preemption, aging, dependency boosts, provider quota constraints and permitted fallback routing.
- Manual Pause means demote work toward the end of the scheduler queue. If the paused work is the final runnable work in that session, the session's auto mode remains paused until explicitly resumed.
- Auto Intake authorization inherits repository defaults, with `Preview only` as the machine fallback. The Issues table can override a queued issue to `Preview only`, `PR / no merge`, `Merge to main`, or `Production` when supported.
- Allowed provider/harness fallbacks happen automatically when capacity/quota requires it and are shown as notifications plus durable card/run history. A running harness remains fixed; model + effort can interrupt and restart the current model work.
- Chat is available only to orchestrators. Advanced shell access is represented as **Open Herdr pane**, matching the existing local workflow rather than exposing an unrestricted browser shell by default.
- **Settings** cascade Machine → Repository → Run and show the source of the effective value.

## Selected shell and comparison options

**Selected direction: Workstation (Option 2).** It is now the default shell. The visual accents are blue, green and purple; yellow/red remain reserved for semantic warning/error states.

1. **Command Deck** — retained only as a comparison reference.
2. **Workstation — SELECTED** — narrow vertical surface rail + repo rail. Most tool-like and closest to an IDE / n8n workstation.
3. **Context Rail** — retained as a comparison reference for ambient operational awareness.

Use the top option switcher (or keys 1–3) only to compare the historical alternatives. Product handoff should implement the Workstation shell unless a later decision supersedes it.

## Real-run-informed details

The supplied run archive showed real Auto Office states and structures used to shape this prototype: executing/closed phases; accepted, running, submitted, changes-required, paused and planned tasks; multiple revisions; distinct plan-reviewer, executor, code-reviewer and Herdr pane records; archived runs; and high dispatch counts on long-lived runs. The prototype intentionally avoids publishing source machine paths or raw private log contents.
