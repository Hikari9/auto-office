---
title: Task checks that run tests/v31 files need pytest --all or they silently run nothing
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 29, tokens: 19853 }
id: l-88656eee
scope: repo
status: active
src: agent
touches:
  - pyproject.toml
  - tests/conftest.py
---

## Decided
Every plan task check that names tests/v31 test files passes `--all`. An example is `.venv/bin/python -m pytest -n 2 -q --all tests/v31/test_x.py`. Without it, the default marker filter in pyproject.toml deselects the integration, legacy and slow tests, and many tests/v31 modules carry those markers.

## Tried and ruled out
Checks without `--all` in run 74f66e5d. On T2's revision the check printed "no tests ran" (pytest exit 5, 19 deselected). Base gave the same result, so Office classed it as pre-existing and accepted the task with no test run (the #1175 shape). A hand run with `--all` showed 19 passed, and the new tests failed with the fix reverted.

## Watch out
A check log that says only "bringing up nodes... no tests ran" means deselection, not a broken check. Fix the contract with `office amend <T> --drop-check '<old>' --add-check '<new>'`. Accepted tasks rerun the new check and reopen only on failure.

In the code: pyproject.toml `[tool.pytest.ini_options]` markers and addopts deselect integration, legacy and slow. tests/conftest.py adds the `--all` option.
