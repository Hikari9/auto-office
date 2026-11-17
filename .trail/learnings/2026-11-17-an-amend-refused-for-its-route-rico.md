---
title: An amend refused for its route stays atomic; fix only the remedy it prints
author: Rico
date: 2026-11-17
branch: fix/c5-routing-pins
id: l-d9ccdd96
scope: repo
status: active
src: agent
touches:
  - tests/v31/test_issue_721_accepted_route.py
  - docs/review-convergence.md
  - scripts/validate.sh
  - scripts/build_plugin_distribution.py
  - src/office/amend.py
  - tests/v31/test_issue_814_amend_records_on_route_refusal.py
---

## Decided
For #814, an `office amend T --add-accept/--add-check` whose executor relaunch is refused for quota or route-unavailable still records nothing, as #721 requires. Only the remedy changed: it names `office amend route T --as <route> --quote`, not `office dispatch T --as`. For #752 the quota reserve rule stays: a first quoted `--as` skips the reserve, an unquoted reuse on rerun is checked. The RECHECK `next:` now requalifies the route before offering `--resume`.

## Tried and ruled out
Recording the amendment and holding the task when the relaunch is refused. It contradicts tests/v31/test_issue_721_accepted_route.py (quota case), which pins atomic refusal deliberately.

## Watch out
Editing docs/review-convergence.md or skills/auto-office/SKILL.md makes `scripts/validate.sh --pr` fail at the ecosystem check until `scripts/build_plugin_distribution.py --sync` refreshes plugin-dist/.

In the code: src/office/amend.py relaunch refusal remedy; convergence.py / guide.py RECHECK next; tests/v31/test_issue_814_amend_records_on_route_refusal.py
