# Why: closeout and learning rationale

Rationale for pinned rules in the "Closeout and learning" section of `SKILL.md`.

## Why every closeout reorganizes the dispatch surface, not only the last

Herdr panes accumulate across every closeout in a run, not only the final one. An agent that
only tidies panes at the very end leaves finished panes open through every intermediate closeout,
which is how the dispatch surface silently fills up with dead panes over a long run. Running
`close_finished_panes.mjs` at each closeout keeps the surface accounted for continuously instead
of relying on a final sweep to catch everything.

## Why self-improvement is propose-and-prove, not propose-and-ship

Agents may propose a policy change and prove it with replay evidence, but the maintainer decides
what becomes shipped policy. This split exists because a policy change made inside a live run
would let an agent alter the rules it is currently being judged against — the same conflict of
interest the no-self-approval invariant closes elsewhere in the lifecycle.
