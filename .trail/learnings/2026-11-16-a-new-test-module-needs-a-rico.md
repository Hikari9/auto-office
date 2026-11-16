---
title: A new test module needs a row in both the retirement inventory and the contract-suite legacy table
author: Rico
date: 2026-11-16
branch: fix/1213-prefail-tests
cost: { minutes: 6, tokens: 77535 }
id: l-a7be5a59
scope: repo
status: active
src: agent
touches:
  - docs/test-retirement.md
  - docs/contract-suite.md
  - tests/test_test_retirement_inventory.py
  - tests/test_test_retirement_record.py
  - src/office/rerun.py
  - tests/v31/test_issue_580_rebase_bases.py
---

## Decided
Every new test module under tests/ (except tests/contracts/) gets a `keep` row in two tables, in path-sorted order. The full suite fails on main whenever the rows are missing. By 3.28.0, 39 modules had drifted (#1213).

## Tried and ruled out
Adding rows only to the retirement inventory. The record test then fails because the legacy table must hold exactly the same (path, disposition) set.

## Watch out
Both tests run only under --all, so the PR gate misses them unless a test module is added in the same diff. Bump the Totals `keep` count with the rows. A synthetic label in a combine() parent list (for example "rebased default branch") is not a task, so look up `shared:` paths only for real dependencies.

In the code:
- docs/test-retirement.md "## Module inventory": `| \`tests/...\` | keep |  |` rows plus the `Totals: {...}` line
- docs/contract-suite.md "## Legacy test-module disposition": `| \`tests/...\` | keep |` rows
- checks: tests/test_test_retirement_inventory.py, tests/test_test_retirement_record.py
