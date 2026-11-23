---
title: "agy quota must come from the daily CloudCode host agy uses; the prod host reports idle 100% windows and hides Hydrogen"
author: Rico
date: 2026-11-23
branch: main
cost: { minutes: 41, tokens: 337128 }
id: l-6c9a8e03
scope: repo
status: active
src: agent
touches:
  - SKILL.md
  - docs/orchestrator-reference.md
  - plugin-dist/docs/orchestrator-reference.md
  - plugin-dist/skills/auto-office/SKILL.md
  - src/office/context_snapshot.py
  - tests/v31/test_issue_922_919_wake_default.py
  - CHANGELOG.md
  - docs/root-wake.md
  - plugin-dist/docs/root-wake.md
  - src/office/_version.py
---

## Decided
On 2026-11-23 (#1339) Office launched Hydrogen while agy showed the Gemini weekly window at 0.4% left. agy 1.5.14 talks to daily-cloudcode-pa.googleapis.com (11k hits in its own log). For the same account, cloudcode-pa.googleapis.com answers retrieveUserQuota and the summary endpoint with idle windows (reset = now + window, 100%) and lists no gemini-4 model. The daily host returns gemini-4-hydrogen-sonar and every Gemini bucket on one shared weekly allowance (0.04%). The probe now queries the daily host first and treats prod-host windows as unknown.

## Tried and ruled out
Adding a project or metadata body to retrieveUserQuotaSummary: the project changes nothing and metadata is a 400. fetchAvailableModels on the prod host shows no gemini-4 either. agy has no quota CLI command. Its status line JSON on stdin carries quota.gemini-weekly and gemini-5h, but that only reaches a user's own statusline script.

## Watch out
The daily summary returns only the Claude/GPT group, so families.gemini comes from per-model buckets there. That summary said 99% weekly while its per-model Claude buckets said 37%, and the probe prefers the summary.

In the code: scripts/agy-usage.py API_HOSTS, DAILY_TIMEOUT_S, GEMINI_4_FAMILY, FALLBACK_HOST_CAUSE; src/office/candidates.py quota_for_candidate.
