---
name: auto-update-benchmarks
description: "Refresh missing Artificial Analysis routing benchmark scores for the bound Auto Office run through one low-cost background subagent. Use only when the user explicitly invokes /auto-update-benchmarks or directly asks to update the routing benchmarks. Never invoke from intake or on your own initiative."
---

# Auto Update Benchmarks

Invoking this skill is the user's opt-in. Intake never asks about it.

1. Bind the run (`office list`, then `office resume <id>` if needed).
2. Run `office benchmarks brief`. If it says every row has a score, report that and stop.
   If it refuses with `benchmark-refresh-spent`, report that the run's one refresh is used and stop.
3. Otherwise start one background subagent on the smallest capable model at low effort, with the
   printed brief file as its whole prompt. Keep working on the run while it runs.
4. The subagent ends with `office benchmarks submit <delta>`. Read the submit result from the run's
   events (`office inspect`), not from the subagent's self-report, and relay it in one line.
   A rejected delta changes no score and blocks nothing. Do not retry it or hand-edit the delta.

One refresh per run. Office never fetches anything itself, and an accepted snapshot never
replaces a trusted score; routes decided after acceptance record the snapshot hash.
