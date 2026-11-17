---
title: "Tests must put user routing policy in the live config, not the run's pinned copy"
author: Rico
date: 2026-11-17
branch: fix/c5-routing-pins
id: l-58254765
scope: repo
status: active
src: agent
touches:
  - tests/test_h4_agy_hydrogen_reviewer.py
  - tests/test_route_probe.py
  - src/office/candidates.py
  - src/office/inspect_cmd.py
---

## Decided
Since #1120, routing uses only the live user/repo policy whenever it can be read. A deny, allow or role rule set only in a run's pinned config (`run["policy"]["routing"]["user_policy"]`, or the `config=` passed to declared_decision/route_role) no longer applies. Tests write the live policy instead: the user config file (`OFFICE_USER_CONFIG`) or a monkeypatched `candidates.required_user_policies`.

## Tried and ruled out
Keeping the pinned policy as a fallback merged under live: that is the #1120 bug (a cleared rule kept applying to running runs).

## Watch out
Some fixtures stub `candidates.required_user_policies` to `[]` (tests/test_h4_agy_hydrogen_reviewer.py env). There a live allow cannot reach routing until the test stubs it to return the allow. Unquoted reviewer pins are now `by: orchestrator`, and `office inspect task` prints the real actor.

In the code: tests/test_route_probe.py `_live_deny`; tests/test_h4_agy_hydrogen_reviewer.py `_live`; src/office/candidates.py live_user_policies/required_user_policies; src/office/inspect_cmd.py review pin line
