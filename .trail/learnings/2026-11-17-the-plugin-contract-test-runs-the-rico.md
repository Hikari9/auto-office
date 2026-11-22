---
title: "The plugin contract test runs the real Claude Code, so a host upgrade can turn every PR gate red"
author: Rico
date: 2026-11-17
branch: fix/1273-red-main
id: l-4a80b92a
scope: repo
status: active
src: agent
touches:
  - tests/contracts/test_plugin_distribution.py
---

## Decided
A newer Claude Code (2.1.326) sends a `system/ui_invalidate` event before `system/init` in `-p --output-format stream-json`. The plugin install contract test read only the first event, so it failed on main for anyone on that host version. It is in tests/contracts, which every `validate.sh --pr` runs, so every PR gate failed. The test now reads events until init, up to 20.

## Tried and ruled out
Treating it as part of the #1267/#1265 regressions: the code was the same, only the installed host changed.

## Watch out
If a contract test fails on a clean main with no matching source change, check the host CLI versions (`claude --version`) before bisecting commits. The same was true of a pre-existing-checks timeout test that failed once under load and passed alone.

In the code: tests/contracts/test_plugin_distribution.py `_assert_host_exposes_namespaced_skills`
