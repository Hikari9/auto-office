---
title: "An autouse fixture that sets an OFFICE_ variable in v31 tests must build env first, or env deletes it"
author: Rico
date: 2026-11-17
branch: fix/1273-red-main
id: l-a92b73b8
scope: repo
status: active
src: agent
touches:
  - tests/v31/conftest.py
  - tests/v31/test_preexisting_checks.py
  - tests/v31/test_issue_1175_unrunnable_check.py
  - src/office/gates.py
---

## Decided
The v31 `env` fixture deletes every OFFICE_*, HERDR_* and AUTO_OFFICE_* variable when it is built. An autouse fixture runs before `env`, so any OFFICE_* it sets is silently gone by the time the test runs. Such a fixture now calls `request.getfixturevalue("env")` first (when the test requests env), then sets the variable.

## Tried and ruled out
Treating the runner-timeout check test as a plain load flake. It failed every time under load because the quiet-host factor (OFFICE_CHECK_LOAD_FACTOR=100000) never reached the check job; the default factor 2 x CPUs applied and the gate went UNAVAILABLE. It only passed on a quiet host.

## Watch out
A test that passes alone and fails in the full or PR suite, with a gate UNAVAILABLE summary naming "host load N exceeds M", is this. Setting the variable inside the test body (after env) also works.

In the code: tests/v31/conftest.py Env setup env_drop; tests/v31/test_preexisting_checks.py and tests/v31/test_issue_1175_unrunnable_check.py `_quiet_host`; src/office/gates.py host_overloaded
