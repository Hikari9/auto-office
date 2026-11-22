---
title: "The PR gate fails a new module test that imports it in a list, and fails any version number written into docs"
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 72, tokens: 215358 }
id: l-05774c15
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - src/office/_version.py
  - src/office/root_wake.py
  - tests/v31/test_root_wake_723.py
  - config/config.default.yaml
  - docs/xs-xl-run-gear.md
  - plugin-dist/config/config.default.yaml
  - plugin-dist/docs/xs-xl-run-gear.md
  - src/office/briefs.py
  - src/office/config.py
---

## Decided
On 2026-11-17 (#1233) the scoped PR gate failed twice before passing. Neither failure was in the change itself.
1. The gate said "No tests mapped to changed runtime module self_review_subagents.py" even though a new test imported it. The import was `from office import briefs, config as cfg, contract, self_review_subagents as srs`, and the selector only maps a module when it is the first name after `from office import`. Fix: import the changed module on its own line.
2. test_version_single_source failed because the docs and a module docstring said "a run started from 3.28.5". Any literal of the current version outside _version.py and CHANGELOG fails. Fix: say "after #1233" instead of the version.

## Watch out
The gate output names only the first problem. Fix it and rerun; the next one shows only then.

In the code: scripts/select_pr_tests.py line ~131 regex `\b(?:from\s+office\s+import\s+|office\.|import\s+office\.|from\s+office\.)<stem>\b`; tests/test_version_single_source.py::test_version_string_is_not_hardcoded_elsewhere; scripts/validate.sh --pr.
