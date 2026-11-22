---
title: "Per-task self-review audits run only on XL runs since 3.28.5; S, M and L use the executor's in-session fan-out"
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 1, tokens: 2013 }
id: l-8fb47052
scope: repo
status: active
replaces: l-5997cbdd
src: agent
touches:
  - src/office/self_review_subagents.py
  - config/config.default.yaml
---

## Decided
Since 3.28.5 (#1233, 2026-11-17) Office funds the separate per-task self-review audit (`self_review_subagent` gate, a reviewer pane per task) only on XL runs. Rico's pick: "Separate panes on XL only". S, M and L runs rely on the executor's in-session subagent fan-out and then the one convergence review. XL keeps per-task audits plus Goldfish. Runs pin the gear list at start, so a run started before 3.28.5 without the pin keeps the old S to XL behaviour.

## Tried and ruled out
Calling the extra reviews in run 1e00cdc6 a 3.28.0 regression. Per-task audits ran from gear S up from #809 (3.7.0) onward. #1191 (3.28.0) only made them visible as reviewer panes.

## Watch out
Before 3.7 the self-check was the executor's in-session fan-out tiered inline/single/deep (#312, 3.2.6). That is the model S to L went back to.

In the code: src/office/self_review_subagents.py AUDIT_GEARS=("XL",), AUDIT_GEARS_KEY, audit_gears(), funded(); config.resolve_gates pins review.self_review_audit_gears; config/config.default.yaml review.self_review_audit_gears: [XL]; briefs.in_session_fan_out.
