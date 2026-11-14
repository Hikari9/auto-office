---
title: "Hydrogen xhigh conformance probe fails because the agy adapter maps xhigh to nothing, not because agy refuses it"
author: Rico
date: 2026-11-14
branch: main
cost: { minutes: 5, tokens: 110834 }
id: l-6a94cb8e
scope: repo
status: active
src: agent
touches:
  - adapters/seed/agy.yaml
  - src/office/route_probe.py
---

## Decided
On 2026-11-15, probes for agy/gemini-4-hydrogen passed at low, medium and high. At xhigh the probe failed with conformance-failed ("no harness-reported invocation metadata"). It was reported on #1143 as a follow-up and not fixed in the #1142/#1144 landing work.

## Tried and ruled out
- That agy lacks xhigh: `agy models` lists gemini-4-hydrogen-xhigh, and a direct `agy -p ... --output-format stream-json --model gemini-4-hydrogen-xhigh --effort xhigh` emits init.model=gemini-4-hydrogen-xhigh.
- That the identity reader is wrong: `_agy_init` handles the xhigh tier suffix.

## Watch out
- The fix is the adapter effort mapping. Changing it alters adapter_hash, so every cached agy probe pass (low/medium/high) is invalidated and must be re-probed.
- `office doctor --probe-route` returns a cached pass without launching ("cached record, no new launch"). Read probed_at before calling it a fresh receipt.

In the code: adapters/seed/agy.yaml effort_mapping `xhigh: null` (and `max: null`); src/office/route_probe.py `_agy_init`, `_PROBE_ARGV`; runs.db table route_probes.
