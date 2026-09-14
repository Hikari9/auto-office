---
name: auto-intake
description: Internal Auto Office v3 intake spoke. Use when the orchestrator must run the grilled-intent interview before freezing intent — covering the twelve-item floor in one or two batched question rounds and deriving the five frozen fields from the answers. The planner never talks to the user; this spoke is orchestrator-owned. Do not use to ask about gear, which is declared, not interviewed.
---

# Auto Intake

The orchestrator conducts this interview directly; the planner is not present (#47).

## The twelve-item floor

Cover all twelve on every run, delivered as one or two batched question rounds, never a back-and-forth conversation:

1. **Outcome** — what is true when this is done, in the user's words.
2. **Done-criteria** — exact commands, reads or observations that prove it.
3. **Blast radius** — repos, environments, live systems; production named or excluded explicitly.
4. **Irreversible steps** — each with preconditions written out.
5. **Waves** — which done-criteria can proceed simultaneously vs. must serialise.
6. **Interfaces** — signatures, schemas, routes, file boundaries parallel tasks must agree on.
7. **Constraints** — stack, conventions, domain skills, untouchables.
8. **Speed vs correctness** — which one is being bought.
9. **Executor count** — one repo or several, one slice or several.
10. **User-owned decisions** — anything that would otherwise be guessed.
11. **Rollback target** — what "undo this" concretely means.
12. **Prior art** — existing work, branches or PRs this must not duplicate or contradict.

## Freezing the five fields

`goal`, `done_criteria`, `blast_radius`, `named_actions`, and `non_goals` are **derived** from the twelve answers, not asked directly. Every frozen value traces back to an answer above; one that doesn't is the defect this spoke exists to prevent.

## Irreversible steps become named_actions

Item 4 becomes a `named_actions:` entry with its preconditions written out exactly — that's the receipt that lets the loop perform it later without stopping. Preconditions you can't yet write exactly mean the interview isn't finished; keep asking.

External sends always stop the loop, regardless of preconditions — never a `named_actions` entry.

## Exit test

The receipt is frozen intent a stranger could build the right thing from: a routed executor in a separate process, with no access to this conversation and no chance to follow up. Anything they'd have to guess means the interview isn't done.

## Gear is not a thirteenth question

Gear is declared in the kickoff block, decided by the fit test from blast radius, reversibility and size class. It ships from there only — this interview never asks for it.
