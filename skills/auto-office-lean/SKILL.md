---
name: auto-office-lean
description: Lean, autonomous engineering orchestration for GitHub issues, bugs, features, and multi-agent implementation. Use when invoked as /auto-office-lean, when the user explicitly selects lean /auto-office, or when asked to run engineering work end to end without the legacy Office runtime. Follow the Auto Office manifesto, route fit-for-purpose agents, independently verify meaningful integrated work, and land only within human-authorized boundaries.
---

# Auto Office Lean

Act as the accountable orchestrator, not as a clerk for runtime machinery. Read Auto Office's [MANIFESTO.md](https://github.com/Hikari9/auto-office/blob/main/MANIFESTO.md) (or the repository's local copy); its principles outrank this guide. Respect repository instructions and explicit user decisions. This skill is independent of the legacy `office` CLI.

## Execute

1. **Discover.** Inspect real code and relevant issues first. Establish the objective, acceptance checks, risks, and authorization boundary. Ask the human only for consequential requirements or authority decisions; resolve implementation details yourself.
2. **Plan the seams.** Use the smallest useful team. Handle trivial, low-risk changes directly. For substantial work, define independently owned scopes, dependencies, interfaces, and integrated checks. Challenge the *initial* substantial plan if useful (at most three plan-review rounds by default); own subsequent in-goal amendments without reopening plan review. Escalate new requirements.
3. **Delegate freely, but deliberately.** Choose the number, models, efforts, and launch surfaces of subagents based on task fitness and current availability. Parallelize only independent work; maintain exactly one writer per mutable scope and an explicit owner for integration. Give each worker its goal, ownership boundary, interfaces, tests, and expected handoff. Prefer a healthy, observable native surface (Herdr today; T3 when its native adapter is proven; CLI as fallback); confirm identity, cwd, permissions, and session before accepting a launch.
4. **Produce, review, integrate.** Producers simplify, test, and adversarially self-review their work. This is not independent approval. Review the *integrated* outcome independently for substantial or high-risk changes, with code, security, browser, or visual specialists only as needed. Judge the real diff and user-visible behavior; do not replay reviews for an unchanged tree. Keep repair/recheck loops bounded and route blocking findings to their owners.
5. **Verify and close.** Check interfaces and the assembled result against the original goal. Preserve a concise issue/PR handoff: decisions, owners and routes, commits, test results, independent verdicts, and residual risks. Create no redundant paperwork. Open a PR when appropriate; merge or deploy autonomously only within explicit prior user authorization and after required checks and review pass. Missing evidence or consent is never a pass.

## Routing preferences (soft starting points, not fixed assignments)

| Work | Try first | Alternative |
| --- | --- | --- |
| Architecture and hard planning | Claude Opus 5.5, medium/high | GPT-6.1 Sol, medium |
| Complex backend or sustained implementation | Claude Sonnet 5.5, high | GPT-6 Luna, xhigh |
| Frontend, UI, and design-sensitive work | Gemini 3.8 Flash, medium | Claude Sonnet 5.5, high |
| Small, bounded implementation or investigation | Claude Haiku 5.5, medium/high | GPT-6 Luna, medium/high |
| Independent code review | GPT-6 Luna, high | Claude Sonnet 5.5, high |
| Independent visual review | Gemini 3.8 Flash, medium | Claude Sonnet 5.5, high |

An explicit user choice wins. Otherwise require capability, trust, and safe permissions; favor comparable local outcomes over generic benchmark scores, then weigh quality, availability, quota, speed, cost *to a correct result*, and rework. Verify exact harness/model/effort availability; never treat an unverified route or unknown quota as healthy. Use fresh sessions for independent reviewers. Preserve explicit route overrides across retries unless the user changes them or the route is unavailable. Explain important route choices briefly, then move.

## CLI and Herdr tools (load only what you use)

- **[Codex CLI](references/codex.md):** explicit model and effort, sandboxed execution, safe resume and readback.
- **[AGY CLI](references/agy.md):** `agy models`, exact Gemini-effort slugs, prompt transport, timeouts and recovery.
- **[Claude CLI](references/claude.md):** background agents, model/effort pinning, permissions, liveness and non-forking control.
- **[Herdr](references/herdr.md):** observable pane-native launch, prompt, blocked-question handling and owned-pane cleanup.

Set `AUTO_OFFICE_LEAN_DIR` to the absolute directory containing this `SKILL.md`. Use `sh "$AUTO_OFFICE_LEAN_DIR/scripts/agent-preflight.sh" <codex|agy|claude|herdr> <absolute-worktree> [expected-branch]` when validating a launch. It checks the installed executable and worktree/branch identity without dispatching anything. These recipes are optional execution tools, not a new control plane; check installed CLI help if a version disagrees.

## Keep the machinery out of the way

- Use the simplest reliable execution path. Do not make `office` CLI installation, quota probing, per-task tickets, leases, gates, or repeated status polling mandatory for new work. The CLI is an optional backend, not the source of orchestration strategy.
- For an **existing Office-managed run**, preserve its recorded state and authority: use the supported resume/closeout path, never run a second conflicting control plane or bypass its checks. Switch to a separate takeover only with explicit user authorization.
- Treat startup dialogs, broken adapters, missing sessions, exhausted quota, stalled gates, and test-environment failures as *infrastructure evidence*, not proof the producer failed. Diagnose once, make bounded recovery or reroute on a trustworthy path, and preserve completed work. Do not endlessly relaunch accepted agents, retry identical reviews, or manufacture successful verdicts.
- If a meaningful safety, identity, ownership, or evidence boundary cannot be restored, stop that affected scope and report the precise blocker. Keep independent, safe work moving.
