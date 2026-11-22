---
title: "Hydrogen reviews code only as the user-allowed high-effort route, and the reviewer order needed a fix to list it"
author: Rico
date: 2026-11-15
branch: main
cost: { minutes: 15, tokens: 85726 }
id: l-f2673113
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - src/office/_version.py
  - src/office/configcmd.py
  - src/office/routecmd.py
  - tests/test_h3_hydrogen_reviewer_seed.py
  - docs/route-discovery.md
  - plugin-dist/docs/route-discovery.md
  - src/office/hydrogen_trials.py
  - tests/test_issue_1163_hydrogen_user_allow.py
  - tests/test_routecmd_hydrogen.py
---

## Decided
On 2026-11-15 Rico promoted agy/gemini-4-hydrogen to a voting code reviewer ("Promote now") after the planted-bug set: Hydrogen caught 5/5, Luna 4/5, and neither had a false positive. It reviews code only, only at @high (the effort that ran the set), only through the user's exact allow, and still needs a fresh exact probe pass. Office has one reviewer per gate, so it sits second in roles.code_reviewer.preferred_seed after codex/luna@high. It votes when Luna is unavailable or excluded. The shadow lane keeps scoring 10 live PRs as a check.

## Tried and ruled out
- Listing it in the seed alone. A preferred_seed entry is not a user allow, so routing never admits it.
- `office config --force` to seed it. Seed validation rejected the catalog-disabled row even with --force. #1176 fixed this: a seed may list a catalog-disabled route when the user's exact allow covers that route for that role.

## Watch out
- Plan, visual and other reviewers, planners and workers still veto Hydrogen.
- No producer-family exclusion applies: Hydrogen may review a task Hydrogen executed, as Luna already does for Luna-executed tasks.
- #1169 and #1170 landed without the AGENTS.md version bump. Luna caught it on the next PR, which then carried it (3.21.0).

In the code: hydrogen_trials.REVIEW_ROLES/REVIEW_EFFORT and route_refusal, configcmd._user_allowed_seed, routecmd._dispatch_view. Config: `office route allow agy/gemini-4-hydrogen@high --role code_reviewer`, then the seed.
