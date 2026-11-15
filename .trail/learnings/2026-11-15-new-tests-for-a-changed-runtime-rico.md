---
title: "New tests for a changed runtime module go in a test file named after the module, or the PR gate blocks"
author: Rico
date: 2026-11-15
branch: feat/quoted-route-override
id: l-6232ac0d
scope: repo
status: active
src: agent
touches:
  - scripts/select_pr_tests.py
  - tests/v31/test_route_override_quoted.py
---

## Decided
Tests for the quoted route override live in their own file named after the module they cover, so the scoped PR gate can map them.

## Tried and ruled out
Appending the new tests to the existing #837 override test file. The gate then blocked with "No tests mapped to changed runtime module" because that file's name does not contain the module name, and its multi-name `from office import ...` line is not matched by the import pattern.

## Watch out
The selector is digest-pinned, so fix the gap with a correctly named test file, not a selector change. The first `validate.sh --pr` run in a fresh worktree also builds a new `.venv`.

In the code: scripts/select_pr_tests.py (direct match is `test_<stem>.py` or `test_<stem>_*.py`), tests/v31/test_route_override_quoted.py.
