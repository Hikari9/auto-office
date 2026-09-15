---
name: hermes-cli
description: Directly invocable Auto Office v3 Hermes CLI mechanics primitive. Use when a selected/explicit Hermes harness dispatch needs prompt transport, model selection, working directory targeting, liveness detection, completion readback, or Hermes-specific failure attribution. Do not treat this primitive as a separate lifecycle office or proof of adapter promotion.
---

# Hermes CLI primitive

Load `../../adapters/seed/hermes.yaml` plus current local override. The shipped seed is `valid-unverified`.

Hermes uses Gemini models via Google AI. Prompt transport is via stdin pipe. Model selection uses `--model` flag. Working directory uses `--cwd` flag.

Hermes records sessions in `~/.hermes/state.db` (SQLite). The adapter reads this directly for telemetry rather than parsing output. Skill detection uses `skill-md-read` inference since Hermes has no first-class Skill tool.

Known failure signatures: "quota exceeded", "model not available", "rate limit", "context length exceeded", "invalid API key".

Completion detection: exit code 0 plus output containing completion markers. Liveness uses PID check plus output file growth monitoring.

Attribute invocation failures to adapter, quota failures to quota/account, and logical implementation defects to the producer model/role only when evidence supports it.
