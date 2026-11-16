---
title: "Route overrides can be issued on Rico's quoted words by any non-worker, never by a worker"
author: Rico
date: 2026-11-15
branch: feat/quoted-route-override
id: l-5611d848
scope: repo
status: active
src: agent
touches:
  - SKILL.md
  - scripts/build_plugin_distribution.py
  - src/office/route_override.py
  - src/office/routecmd.py
  - tests/v31/test_role_override_837.py
---

## Decided
On 2026-11-15 Rico chose "Any non-worker, quoted": orchestrators may run `office route override` themselves when they relay his verbatim words with the Manila time he said them. Workers never can. The receipt records who issued it (terminal or quoted), the quote, the quote time and the herdr pane, and all of it is covered by the HMAC. Receipts made before this change have no issuer and keep the original MAC body, so they still verify. Every quoted override is reported to Rico (SO32a in rico-mode).

## Tried and ruled out
Reusing the dispatch `--quote` flag as the override authority. That text is something an orchestrator can write, so it stays unable to lift a deny. Only the new `--rico-quote` plus `--quoted-at` pair counts, and giving just one of them is refused rather than falling back to the terminal path.

## Watch out
Stripping the new columns from a quoted receipt to reach the legacy MAC body does not verify, because the stored MAC was computed over the new body. A test pins this. SKILL.md has a 250-line budget, so edits to it must replace text in place. After editing skills, run `scripts/build_plugin_distribution.py --sync` so `plugin-dist` matches.

In the code: src/office/route_override.py (`issue`, `validate_quote`, `_mac`, `ensure_schema`), src/office/routecmd.py (`override`, `_issuer_note`), tests/v31/test_role_override_837.py.
