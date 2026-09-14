---
name: auto-loop
description: Internal Auto Office v3 loop-driver spoke. Use after plan approval to dispatch waves without blocking, integrate dispatch branches through a validated merge, adjudicate contract disagreements, and hold the autonomy ceiling — proceeding end to end while never merging to main without an explicit user statement.
---

# Auto Loop

Dispatch every task in the wave before waiting on any of them. One worktree per dispatch, cut from the run's pinned base SHA; the orchestrator commits and merges, workers never touch git. Two tasks in one wave never share a write scope — a wave is only real if it draws as disjoint. Every brief's completion criterion is a command whose output settles it, not a target — "under 120 lines" is a wish, "`wc -l` reports under 120" is a receipt (spec §4).

A dispatch counts as in flight only once it has a receipt — an observed transition to working, or output in the pane — not a status read at dispatch time; harness `idle` semantics differ across codex and agy (spec §6). Polls while any dispatch is live. Between polls the orchestrator only touches write scopes no live dispatch holds: planning later waves, reviewing returned output, preparing briefs, reading state.

Arm a background monitor over every dispatch at or before dispatch time, kept armed for the whole run. Completion is an event from it, not a wait, and it fires on every terminal state — finished, idle, blocked, unknown, disappeared — not just success. Talking to the user never suspends it. If the monitor is lost or unarmed, re-poll before asserting any dispatch's state.

A wave ends when every dispatch in it has returned, or been declared dead by two independent liveness signals — the receipt is both signals; one alone is not a death. On each completion event: collect the result, close the pane of an agent that actually finished, update the spawn ledger. Leave `blocked` and `unknown` panes open — they're unresolved, not completions, and closing one destroys the evidence needed to recover (spec §6).

At integration: commit each worker's tree on its own dispatch branch, authored by the orchestrator; merge dispatch branches into the run's integration branch in wave order; resolve conflicts at the orchestrator, never by re-dispatching a worker into a tree it no longer owns; then run the plan's validation commands on the merged result. The receipt is that merged-result run — a green worker tree only proves itself, since N parallel trees have never coexisted until then. Re-merging a dispatch branch after its merge was reverted does not reapply it — git treats it as already merged and reverted, producing conflict markers instead of the fix; take corrected files straight from the owning branch, or revert the revert first (spec §7).

When a test against the pinned contract disagrees with the implementation, name which one is wrong and why: repo convention outranks an ambiguous contract clause, and a contract clause outranks an implementation's convenience. This is an adjudication, not a re-plan. Review round caps live in `auto-review`. A verification asserting only exit codes can't catch a gate that fails closed on everything — assert the reason it gives, not just its exit status (spec §8.1).

After approval the run proceeds end to end with no further go-aheads: bootstrap a plan-only commit, named branch, and draft PR before the first task; commit each wave as it goes green; run verification and review rounds; remove the plan file and mark the PR ready at closeout; merge dispatch branches into its own working branch as needed. Merging to `main` stays the user's call — their most recent explicit statement, in the approval or the conversation, governs over any earlier default. Absent that sentence, the run stops at a ready PR.

Report blockers, then carry out the authorized action — a defect exit pauses and reports, it doesn't decline an authority decision. A defect must concern the artifact it names; a scope objection raised to dodge an authority call is really that disagreement in disguise. If the harness itself blocks an action, ask the user instead of rephrasing past the guard (spec §9.1, §9.2).

It stops for exactly two things: an external send, and a user-owned decision the plan didn't anticipate. Everything the plan named — production applies included — it executes without asking again.
