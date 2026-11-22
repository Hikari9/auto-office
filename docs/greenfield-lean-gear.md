# Greenfield / lean gear (#936)

Use `office start "<goal>" --gear greenfield --blast-radius repo` **only for new work**: a new project, or a new component with nothing it must fit into. This is an **explicit opt-in**, not an auto-fit: Office counts nothing and has no detection threshold or config. A mature, multi-owner or independently parallelizable codebase should use the normal XS–XL gears.

## Intake judgement (#1300)

The orchestrator decides whether the job is new work, by that loose definition, after looking at the repository. It always passes its judgement to `office start` as `--new-work yes|no "<why>"`. Office records it in the run's gates as `gates.greenfield_detection = {"judged_by": "orchestrator", "new_work": true|false, "why": "<why>"}`, and `office inspect run` shows it. A value other than `yes` or `no`, or an empty why, is a usage error (`bad-new-work`) and creates no run.

- **New work, no `--gear` (G4):** `office start` refuses with `greenfield-candidate` and creates no run. Its `next:` line names `--gear greenfield` and `--gear XS|S|M|L|XL`. The orchestrator asks the invoker which one, then starts again with that gear. `--size-class` alone does not count as a choice.
- **`--gear greenfield` with `--new-work no` (G2):** Office prints one warning line, records the judgement and starts the run. The invoker's explicit gear wins.
- **`--new-work no`, or no flag, without `--gear`:** Office picks the default gear as before.
- **`--gear greenfield --fastlane` (G3):** allowed. The run pins `gates.fastlane` `on`, the orchestrator leads the one cohesive build with its own strategy at the XL floor, and the convergence review stays required. The one-task, 2-3 phase plan contract and the phase brief are unchanged. Without `--fastlane` (or with `--no-fastlane`) greenfield pins no fastlane gate and keeps its dedicated XL executor.

## Runtime contract

- **One implementation task** (`T1`) with one broad but explicit ownership envelope; the plan has **2 or 3 `phases:` list items**. Phases are internal checkpoints, *not* separate dispatched tasks or Office review gates. A plan with 0, 1, or more than 3 phases, multiple tasks, repeated phases, or a conflicting declared task size is refused before storing it.
- **One dedicated strong executor:** the entire task routes at **XL** (Intelligence Index floor 47 and the eligible XL executor slate), with `deep` routing profile and no forced decomposition. The router still enforces verified invocation, role policy, adapters, quotas and user denials. It does **not** hard-wire Opus or bypass the model floor. When an XL slate entry is denied by the user's role policy or is not in the dispatchable catalog, routing falls back to the next eligible route and the route audit names the skipped entry, so the executor can differ from the first XL slate entry. The executor owns all phases in the same session, may commit checkpoints, and calls `office submit` only once the complete build is ready; an actual RECHECK/repair can still redispatch the task.
- **Fewer fixed reviews:** inline planning by default, **no initial independent plan-review gate only when the intake declares low risk** (unknown risk, and risk the planner classifies itself, keep one plan review, so a planner cannot exempt its own plan; a risk raised after a plan was already submitted does not add one, the same as XS), no Office-funded per-task self-review subagent fanout, and **one independent convergence code review on the composed task result** after deterministic checks. A genuine RECHECK can trigger another review under the existing round cap; unavailable reviewers do not silently count as approval. No serial task chain is created.

**Known limit:** a risk raised after a plan was already submitted does not queue an initial plan review. This is existing behavior shared with XS, not specific to greenfield; it is tracked as a separate issue. Declare the risk at intake, or in the first plan, when a plan review must happen.
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
