---
title: "Dispatches record task size as an explicit unsized literal, and readers treat it as empty"
author: Rico
date: 2026-11-28
branch: main
cost: { minutes: 1, tokens: 341302 }
id: l-5e1b3a4b
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - README.md
  - SKILL.md
  - catalog/executor-policy.yaml
  - catalog/provenance.yaml
  - catalog/role-policy.yaml
  - catalog/routes.tsv
  - docs/route-discovery.md
  - plugin-dist/README.md
  - plugin-dist/catalog/executor-policy.yaml
---

## Decided
Every dispatch packet records task_size (XS-XL or the literal "unsized"), task_sizes, run_gear, run_size_class and fastlane (#1363, PR #1364). dispatches.size_class is written for every role with "unsized" as the fallback. office status shows size on its own "sizes:" line.

## Tried and ruled out
- Storing NULL for unsized in dispatches.size_class: the reviewer required the explicit literal. Writing "unsized" broke route learning's fallback (it read the string as a size), so readers convert it back with state.recorded_size.
- Putting the size inside the "routes:" line (T1 [S] ...): existing tests and tools parse that line, so the size went on a separate line.
- Using the run's intake size_class when a task has none (review F5): waived at the round cap. A run size is not a task size, so it is recorded as run_size_class and never substituted.

## Watch out
Any new reader of dispatches.size_class must treat "unsized" as empty (state.recorded_size), or size-based learning and rollout change.

In the code: src/office/state.py task_size/largest_size/size_record/recorded_size; gates.gate_task_sizes; route_learning.py size_class fallback; tests/v31/test_issue_1363_dispatch_size.py.
