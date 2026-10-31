# Greenfield / lean gear (#936)

Use `office start "<goal>" --gear greenfield --blast-radius repo` **only when intentionally starting a new or near-empty repository**. This is an **explicit opt-in**, not an auto-fit from a file-count heuristic: the planner/orchestrator must inspect the repo before using it. A mature, multi-owner or independently parallelizable codebase should use the normal XS–XL gears.

## Runtime contract

- **One implementation task** (`T1`) with one broad but explicit ownership envelope; the plan has **2 or 3 `phases:` list items**. Phases are internal checkpoints, *not* separate dispatched tasks or Office review gates. A plan with 0, 1, or more than 3 phases, multiple tasks, repeated phases, or a conflicting declared task size is refused before storing it.
- **One dedicated strong executor:** the entire task routes at **XL** (Intelligence Index floor 47 and the eligible XL executor slate), with `deep` routing profile and no forced decomposition. The router still enforces verified invocation, role policy, adapters, quotas and user denials. It does **not** hard-wire Opus or bypass the model floor. The executor owns all phases in the same session, may commit checkpoints, and calls `office submit` only once the complete build is ready; an actual RECHECK/repair can still redispatch the task.
- **Fewer fixed reviews:** inline planning by default, **no initial independent plan-review gate**, no Office-funded per-task self-review subagent fanout, and **one independent convergence code review on the composed task result** after deterministic checks. A genuine RECHECK can trigger another review under the existing round cap; unavailable reviewers do not silently count as approval. No serial task chain is created.
- **Producer self-review is retained**, not replaced: low-risk builds use the `single` four-lens tier; high-risk builds use the existing `deep` tier. The ledger, preflight, regression/mutation checks and self-review fix rounds retain their ordinary semantics. Visual review still applies to user-visible acceptance.
- **Safety stays unchanged:** requirements and scope approval, task lease/worktree fencing, explicit action authorization, risk-classification review floor, independent reviewer/producer separation, checks, integration, visual acceptance, landing authority and human merge/deployment boundary all remain in force. `greenfield` never auto-fits and cannot be used to reinterpret old pinned runs.

## Example `PLAN.md`

```markdown
## Requirements
done:
- the app provides a working end-to-end v1 flow
blast_radius: repo
checks: python3 -m pytest -q

## Tasks
### T1: Build the complete v1
scope: src/**, tests/**, pyproject.toml, README.md
depends: none
phases:
- Establish project skeleton, interfaces and data model
- Implement the complete working user-facing core with tests
- Integrate, document and verify the end-to-end experience
checks: python3 -m pytest -q
accept:
- the end-to-end v1 flow works with regression coverage
visual: none
```

Each phase is a named checkpoint **inside T1**. If the project genuinely needs multiple concurrent executors or independent ownership lanes, switch to a normal run gear rather than trying to squeeze extra Office tasks into `greenfield`.

## Why it exists

The #936 comparison found that a single strong executor completed a six-phase greenfield v1 in approximately 60 minutes and $17.56 while an Office run on the same spec had only one of nine tasks done after $12.70+ of cost. The purpose is to remove unnecessary planning/review/handoff overhead while retaining one real final quality gate; those observations are not a guaranteed speedup or token estimate.
