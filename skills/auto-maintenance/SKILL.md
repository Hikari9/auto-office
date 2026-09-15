---
name: auto-maintenance
description: Internal Auto Office v3 maintenance spoke. Use at the beginning or closeout of an Auto Office invocation to label eligible historical outcomes lazily, refresh stale model catalogs outside route-time, update local harness bindings, maintain hot/warm/dreamt evidence tiers, compute maturity/decay, and prepare replay inputs without requiring an always-running daemon.
---

# Auto Maintenance

Maintenance is event-driven by future Auto Office invocations; no daemon is required.

- Mark an observation eligible at the earlier of 14 days after merge or the next three relevant repo/surface runs, then label on the next maintenance pass.
- Preserve negative-outcome asymmetry: recurrence/revert/material post-merge defects are stronger ground truth than no-observed-failure is positive.
- Maintain hot/warm/dreamt authority by lineage. Dreamt rows keep raw local storage but lose numeric routing authority.
- Trigger catalog refresh when local harness fingerprint changes or catalog snapshot is older than 72 hours. Refresh may use network; current routing does not. If refresh fails, keep the last valid snapshot and record staleness.
- Apply ecosystem decay only to affected evidence lineage.
- Compute maturity deterministically from policy + run database; failures move age backward.
- Do not refit routing policy before the replay/calibration evidence bars are met.
