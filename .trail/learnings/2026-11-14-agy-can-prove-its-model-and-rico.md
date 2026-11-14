---
title: agy can prove its model and effort through stream-json output and its strict effort check
author: Rico
date: 2026-11-14
branch: main
cost: { minutes: 7, tokens: 39877 }
id: l-b137269a
scope: repo
status: active
src: agent
touches:
  - src/office/routecmd.py
  - tests/test_routecmd_hydrogen.py
  - ".office/plans/37ccb7f7/PLAN.md"
  - src/office/route_probe.py
  - adapters/seed/agy.yaml
---

## Decided
For exact conformance probes, read agy's identity from its own stream-json `init` event, not from the model's reply. agy refuses an `--effort` that contradicts the tier in the model name. So passing both `--model <tiered slug>` and `--effort <e>`, then reading `init.model`, gives a model and effort the harness itself reports. Plan this probe-only. Ordinary agy executor and reviewer launches keep their argv and text output.

## Tried and ruled out
- The default text output of `agy -p` carries no model or effort, which is why probes refused with no-identity-readback.
- agy's log line "Model ID ... not in local config, defaulting / resolved via default" is not a silent fallback. An unknown model still exits 1 with "invalid model selection".

## Watch out
- `init.model` names the model agy resolved. The adapter seed still lists an older silent-fallback defect, so a reviewer should weigh how far to trust `init.model`.
- On agy, the Gemini effort lives in the model name (gemini-4-hydrogen-medium). Normal launches must not add a separate `--effort` (the adapter forbids it for Gemini slugs). Only the probe adds it, as a consistency check.
- A mismatch error looks like: `--model gemini-4-hydrogen-low conflicts with --effort=high` (exit 1). Classify it as unsupported-model-effort.

In the code: src/office/route_probe.py `_IDENTITY_READBACK` (codex, claude readers), `_evaluate`, `_run_probe`; adapters/seed/agy.yaml worker profile. Live check: `agy -p "ok" --output-format stream-json --model gemini-4-hydrogen-medium --effort medium` -> first NDJSON event `init` with `model`. `agy models` lists the tiered slugs.
