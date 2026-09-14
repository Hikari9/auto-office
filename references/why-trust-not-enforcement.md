# Why the office trusts agents instead of enforcing on them

Ratified by Rico on 2026-09-14, closing issue #94. This is the office's position
on mechanical enforcement, and it settles a question five review rounds could not.

## The position

Non-determinism is the cost of using agents. It is an expectation, not a defect
to be engineered away. The roster is intelligent enough to comply with a rule
stated in a prompt; building machinery to make compliance impossible to avoid
buys less than it costs, and the machinery itself becomes the thing that breaks.

Guardrails belong in the brief. Correction belongs to the orchestrator, applied
when something actually goes wrong, not to a classifier applied to every call in
advance.

## Three decisions

**Do not classify Bash.** Pass `--yolo`, or state read-only in the prompt, and
trust the executor. Do not attempt to decide from command text whether a command
mutates. Attempting to nail every case is the error, not an unfinished version of
the right idea. The five-round history of `scripts/hooks/pre_tool_use.py` — eleven
bypasses fixed, a `PLAN DEFECT` on the sixth — is what this decision is reacting
to.

**Do not infer the target repository.** Trust that the executor works where its
brief sent it, and correct its course when it does not. This shifts the burden to
where it belongs: the planner and orchestrator must be on top of worktree
locations. Knowing where every dispatch is working is orchestration, and an
orchestrator that needs a hook to tell it is not orchestrating.

**Do not disambiguate user from orchestrator.** Prompts come from the user
and/or the orchestrator. The orchestrator has authority to decide on its own and
acts on the user's behalf toward subagents. `approve-plan --quote` is an audit
record of what was approved, not a credential, and no authenticated channel is
needed to make it legitimate.

## What still holds

Dropping mechanical enforcement does not drop the rules it was trying to enforce.
These are unchanged, and they bind the orchestrator:

- Merge to `main` is not the orchestrator's to authorize. Only an explicit
  per-run statement from the user lifts it (lifecycle spec §9.1). The
  orchestrator's authority over subagents does not extend here.
- A producer does not approve or review its own work. The orchestrator acting for
  the user is about *who may authorize a dispatch*, not about collapsing the
  producer and reviewer into one agent (lifecycle spec §8).
- Protected paths, blast-radius ceilings and destructive-action confirmation stay
  in every brief. `--yolo` removes the sandbox, which makes the stated ceiling the
  entire safety boundary — so state it.
- Phase order still governs, and the runtime still refuses to write `approved`
  outside `approve-plan`. That is cheap, deterministic, and costs nothing to keep.

## Consequence for the hook

`scripts/hooks/pre_tool_use.py` classifies Bash source text, which this document
says not to do. It ships demoted to defence in depth and asserts nothing. Under
this position its classifier is not an unfinished gate but unwanted machinery: it
produces false blocks on legitimate read-only work while proving nothing about
the mutating cases. Removing the classifier and keeping only the phase read is
the change that agrees with this document.
