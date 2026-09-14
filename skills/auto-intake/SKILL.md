---
name: auto-intake
description: Internal Auto Office v3 intake spoke. Use when the orchestrator must run the grilled-intent interview before freezing intent — covering the twelve-item floor in one or two batched question rounds and deriving the five frozen fields from the answers. The planner never talks to the user; this spoke is orchestrator-owned. Do not use to ask about gear, which is declared, not interviewed.
---

# Auto Intake

The orchestrator runs this interview directly. The planner never talks to the user (#47).

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

`goal`, `done_criteria`, `blast_radius`, `named_actions`, and `non_goals` are **derived**
from the twelve answers, not asked directly. Freezing a field the user was never asked
about is the defect this spoke exists to prevent — every frozen value must trace back to
an answer above.

## Irreversible steps become named_actions

An irreversible step (item 4) must become a `named_actions:` entry with its preconditions
written out exactly — that entry is what later lets the loop perform it without stopping.
If you cannot yet write the preconditions out exactly, the interview is not finished; keep
asking.

External sends are never `named_actions`, regardless of preconditions. They always stop
the loop.

## Exit test

A stranger with no access to this conversation — a routed executor in a separate process
that cannot ask a follow-up — could build the right thing from the frozen intent alone. If
they would have to guess, the interview is not done.

## Gear is not a thirteenth question

Gear is declared in the kickoff block, decided by the fit test from blast radius,
reversibility and size class. Never ask the user for it here.
