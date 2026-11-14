---
title: "Codex reviewer falls back to pi Luna at 5% quota; you cannot pre-pin a lane reviewer mid-run"
author: Rico
date: 2026-11-14
branch: main
cost: { minutes: 1, tokens: 5707 }
id: l-a8cbaa67
scope: repo
status: active
replaces: l-54ab8fda
src: agent
---

## Decided
Rico's rule (2026-11-14): when Codex quota hits 5% left, reviewers fall back to pi/gpt-6-luna@high. Reviewer seed order is codex luna@high > pi gpt-6-luna@high > deepseek > opus low > agy, with codex Luna denies lifted for code_reviewer. Note there is no gpt-6.1-luna on pi.

## Tried and ruled out
Pinning a convergence lane reviewer before it runs: `office review T1 --as` is refused (tasks have no own review under convergence-v1) and `office review L-T1 --as` only re-pins a review that returned no verdict. Temporarily raising the reviewer reserve to force the fallback was blocked by the permission classifier. To prove pi Luna as reviewer, the rico-mode deputy (not Rico) chose a direct `pi -p --model gpt-6-luna --thinking high --no-tools` review with the diff on stdin.

## Watch out
Shipped denies on codex luna/gpt-6-luna @xhigh/@max stay; they are not Rico's. To force a pi review in Office, pass `--review-as pi/gpt-6-luna@high` at dispatch time.

In the code: ~/.config/auto-office/config.yaml quota.reserve_percent_by_role.reviewer: 5.0; backups config.yaml.bak-20261114-pi-luna-reviewer and -codex-luna-lift.
