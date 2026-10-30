---
name: auto-takeover
description: Auto Office 3.2 orchestrator mode for when the office runtime itself is the bottleneck (repeated submit refusals, plan re-review pausing live work, exhausted escalation, quota stalls). Only an explicit user request starts it; the orchestrator may only suggest it. The orchestrator stops driving work through office, composes one integration branch, fans the remaining work into file-disjoint Herdr lanes, keeps every Office invariant (independent review by a fresh agent session, mutation-verified tests, checks on the integrated tree, the user's merge boundary), and exits through `office close --landed-externally`.
---

# Auto Takeover

**When.** The runtime blocks more than it helps: refused submits keep recurring, plan re-review pauses
work that isn't affected, escalation is exhausted on a small finding, or workers stall on quota. Suggest
a takeover with that evidence. Only the user's explicit words start one, so quote them in the
tracking issue.

**Entry.**
1. `office status` and `office inspect task <T> --verbose` for each task. Sort tasks into accepted,
   usable (pushed, checks pass, findings small), and to-do.
2. Let live workers finish or stop them. Never revoke accepted work. Don't run `office` commands
   that relaunch anything after this point.
3. Record the takeover, with the user's quote and the inventory, on the tracking issue.

**Strategy.**
- Compose one integration branch from main, merging accepted and usable task branches in dependency
  order. Run the run's full checks once on it, in one worktree.
- Split the remaining work into lanes by file disjointness, not plan waves. Docs lanes run beside code.
  Each lane gets its own worktree and branch, cut from the integration head.
- Run lane workers in Herdr panes (the `herdr` skill). In-session subagents are the fallback when
  Herdr is unavailable.
- Append-only registries (gate manifests, endpoint and grant lists, shared mocks) stay shared across
  lanes. Resolve them when merging lanes back. Do not serialize lanes over them.
- The orchestrator may make a small fix itself, a few lines inside one finding. It goes through the
  same independent review as any other change.
- Review once per lane, on the lane's composed branch, not per worker. Merge each lane into the
  integration branch once its review is APPROVED. Rerun the full checks on the integrated tree after every merge.
- On RECHECK, send every blocking finding to its owners at once and have the same reviewer recheck. After 3
  RECHECK rounds, stop and ask the user. Fix or disposition APPROVED findings before landing, with no re-review.

**Invariants (unchanged from Office).**
- No self-approval. Every producer's work, the orchestrator's included, gets an independent reviewer:
  a fresh agent session, never the producer's own (the same model is allowed). The verdict comes from the reviewer's report, not the producer's.
- New tests are mutation-verified. Break the fix, see the test fail, then restore the fix.
- Checks are judged on the integrated tree, not on a lane.
- Merging to main is the user's boundary. The run's end state still applies; e2e means merge, deploy
  and verify. Production and external actions need explicit approval in the current session.
- Raw evidence stays private. Issues and PRs get summaries.

**Exit.**
1. Open one PR from the integration branch with `Closes #<issue>`. Land it per the end state.
2. `office close --landed-externally <merged-pr-url>`. If the merge lacks an accepted revision, ask
   the user and add `--quote "<words>"`.
3. Close or supersede each per-task Office PR with a comment pointing at the integration PR.

**Worker brief template.**
```
CONTRACT <PLAN.md task + issue link; acceptance criteria verbatim>
WORKTREE <abs path> on <branch>, cut from <integration sha>; write only there
SCOPE <files>; shared registries: <files> (add your entries only)
ENV <hazards: prod-bound .env, node via nvm, never git stash, never symlink node_modules>
GATE <exact check command>; it must pass before you report
MUTATION break the fix, show the new test fail, restore it
COMMIT trailer: <Co-Authored-By line>; push the branch, do not merge
REPORT sha | gate pass/fail counts | mutation + result | out-of-scope files + reason | open risks
```

**Review brief template.**
```
ROLE independent reviewer (a fresh session, not the producer's); change nothing
SUBJECT <branch/sha> against <base sha>; contract: <criteria>
FINDING <id> | high|medium|low | blocking|non-blocking | <file:line> | <what is wrong> | <smallest fix> | owner: <lane>
VERDICT: APPROVED | RECHECK | INTAKE_GAP   (INTAKE_GAP: name the user decision, why evidence can't settle it, what it affects)
NEXT <recommended next action>
```
