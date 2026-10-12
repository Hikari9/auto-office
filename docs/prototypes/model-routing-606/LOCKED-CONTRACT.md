# Locked UI contract — Auto-Office Model Registry & Routing Settings

**Status:** APPROVED by maintainer, 2026-10-13 (Philippines)  
**Decision ticket:** [Prototype model catalog and user-routing settings without YAML edits](https://github.com/Hikari9/auto-office/issues/606)  
**Wayfinder:** [Deterministic model catalog lifecycle and user-governed routing](https://github.com/Hikari9/auto-office/issues/601)  
**Interactive reference:** [index.html](index.html) — opens with the approved hybrid selected; historical A/B/C concept controls remain only for comparison.  
**Static reference:** [locked-hybrid.svg](locked-hybrid.svg)

## Decision

**The approved direction is A + C on desktop, with B-style model cards on mobile.**

- **A: Registry Grid** is the desktop **primary information surface**: searchable, filterable, dense, compact, and compareable across model generations, harnesses, effort variants, benchmarks and independent availability/policy states.
- **C: Split Inspector** is **part of that same desktop surface**, not a second alternative page: selecting a model in the grid updates a persistent inspector showing the complete factual and user-configured explanation, effort-specific provenance and controls.
- **B: Model Cards** is the **mobile/narrow-screen presentation**: the same model selection, benchmark provenance, factual states and user controls in expandable cards.
- Continue using the **already approved Auto-Office Workstation shell** (product rail, repository rail, canvas; blue/green/purple accents; yellow/red only for semantic warnings/errors). Do not implement three parallel production registry UIs.

Other directions A-only, B-only desktop and C-only as a substitute for the dense table are **comparison references**, not the target.

## Desktop interaction contract

1. **Settings → Models & Routing → Registry**. The model grid is visible immediately. Search by model/lineage/provider/harness, filter by discovered harness and factual/user policy status. Preserve search/filter/scroll and selected row when inspecting, editing or navigating back from detail.
2. Grid columns: canonical **model lineage + concrete generation**, supported harness and invocation, available effort variants, pinned benchmark index/specific effort score and provenance, **factual runtime availability**, **effective user policy**, and **Inspect**. Do not reduce a multi-effort generation to an unexplained single score; the headline score is a cited sample, full variant matrix belongs in detail.
3. **Sticky model identity** at the left, horizontally scroll remaining columns; vertical scrolling stays within grid/inspector where appropriate. At intermediate desktop widths the inspector can become narrower or an explicit overlay; do not compress data into unreadable cells. Keep the table and inspector visible together at normal desktop width.
4. Clicking an identity/row's Inspect control keeps the selection in the grid and updates the **persistent right-hand inspector** without route changes. Inspector sections: identity and release lineage; supported harness invocation + effort matrix; benchmark source/index/version, scoring confidence and date; exact probe/conformance, trust and capability evidence; account/quota freshness separately; user allow/manual-only/deny and source tier; version-ceiling effect; run-pinned versus future config; reason codes for unavailable/unverified routes.
5. Allow a model to show *user-permitted* **and** *technically unverified/unavailable* simultaneously. Show truthful unknown or pending, not fake 0% or an Auto-Office-created disabled switch. The inspector's edit uses the shared Office config command path, with optimistic controls forbidden until the service returns a committed receipt + re-read.

## Narrow screen / mobile contract

1. At mobile widths, replace the comparison grid with **one-column expandable model cards**; cards show name/generation, harness, effort badges, factual availability, effective personal policy and benchmark headline with provenance affordance.
2. Tapping **Expand evidence & policy** expands that card inline for effort matrix, factual reasons, details and preference controls. Second tap collapses; preserve filter, selection and vertical scroll. No required horizontal scrolling.
3. Search/filter remain above the cards, responsive without overflowing; dialogs become screen-contained sheets/stacked dialogs with explicit close, keyboard Escape, focus management and semantic statuses. The persistent desktop inspector is **not** squeezed into a narrow second column.
4. On tablet/mid-width desktop, retain grid + inspector where readable or allow inspector as an accessible panel; the data model and action semantics do not diverge.

## Config and availability authority

- The **global user model policy** is the first release; repository overrides are tracked separately in [Repo-scoped policy follow-up](https://github.com/Hikari9/auto-office/issues/607). Preserve the existing Machine → Repository → Run (pinned) settings provenance and current Office CLI command authority. Run-pinned policy is inspectable but not mutated retroactively.
- The **model registry** is objective. The active registry retains the **two latest published generations per explicit named model lineage**; effort rows and harness variants do not consume extra generation slots. Older generations remain accessible in a separate historical/archive view and for safe pinned-run audit.
- The user's default is **allow all supported models/harnesses**, with factual machine installations, authentication and exact effort conformance checked separately. New verified model releases above the user's denied version ceiling remain allowed.
- **User-only** filters: harness enable/deny for auto routing, explicit model deny and lineage version ceilings (e.g. Sol ≤5.6 denied does not deny Sol 6.1), and **manual-only / overkill** scoped policy. Price, quota appetite or benchmark quality never author a disable. A user-denied route stays blocked until user explicitly updates that policy; explicit manual routing may override economics/manual-only but not actual impossibility.
- If a user explicitly requests a **technically impossible route**, stop, explain factual blocker and ask **Change selection now** or **Review policy/settings**. Do not silently fallback.
- Qualified but expensive Sol stays allowed and selectable; route economics influence the planner's whole-wave primary+fallback budgeting, not user policy. Shared reservations are soft passive-rebalance signals, not system disablement.
- Detailed cost/token/cache telemetry remains opt-in/off by default. Unknown quota and cost are labeled unknown; never synthesize an exact usage percentage.

## Catalog updates and audit

- **Hybrid update publishing**, only within a user-initiated/opted-in update: routine complete and validated catalog changes publish automatically; uncertain identity/conformance, new capability/safety/trust surface, conflicting config or material changes require human review; invalid changes cannot be approved past factual validation.
- Update view shows stage/validate/preview with the exact diff, generation archival, provenance, pinned catalog hash, reason why approval is or is not needed and rollback. An update never rewrites user-owned preferences; pinned/in-flight run routes remain attached to their original snapshot.
- **Rollback** previews before/after state, requires confirmation and produces an audit receipt. It neither removes history nor silently rebinds active running tasks.
- Expose both supported model list and currently installed/qualified model set; add a history/archive view separately; an account's absent model access is runtime state, not a reason to delete a registry entry.

## First Run and CLI parity

- **First press Run:** non-blocking, minimal popup: **Continue with defaults** or **Review my model policy**. The user can start without answering filters. Do not auto-opt-out a hungry model.
- Same central configuration and runtime data drives CLI and Web Settings. CLI route names and convenience commands illustrated in the prototype are *proposals*, not asserted existing commands. Do not build a parallel YAML manager, web-only policy store, or prompt-only routing authority.
- Show source of effective value, whether a change applies immediately or to future runs, and explicit **read-only pinned** treatment for already started work.
- Browser is a viewer/controller only; runtime persists and validates all changes, issues idempotent receipts, and re-reads confirmed results. Offline/stale writes cannot fake success.

## Prototype states and implementation acceptance

- **Examine:** sample Sol 6.1 has **Allowed by you** alongside **Probe needed**; user never sees system-chosen disabled.
- **Filter:** deny Sol ≤5.6 and verify Sol 6.1 remains user-permitted; disable Codex harness independently; make Sol manual-only without forbidding explicit manual selection.
- **Desktop:** selected grid row drives inspector; sticky identity, filter/search retention; independent fields show source, confidence and reason.
- **Mobile:** B-style cards expand/collapse and expose the same controls/status without overflow.
- **Update:** routine Luna release publishes only when fully validated, with oldest current-generation archived and preferences untouched; Sol unclear effort status remains pending; validated new adapter capability requests approval; invalid model stays rejected; audit and rollback preview are inspectable.
- **Run:** first-run prompt skips cleanly, and manual impossible route requires the two user choices.
- **Stale/error/empty:** load, no installed harness, missing sign-in, quota unknown, probe failed, empty search, offline service, save-in-progress and save-failed are clear, truthful and non-destructive.
- **Accessibility:** keyboard navigation, labeled actions, visible focus, focus trap/restoration in dialogs, semantic status announcements, high contrast, reduced-motion compliance; production should test screen-reader and mobile behavior.
- **No real side effects in prototype:** HTML uses sanitized sample content and mock interactions. It is an implementation reference, not a deployed model setting system.

## Ownership and handoff

- This contract **locks design intent**, not the implementation. Build on `src/office/web/static/settings.js`, `settings.css`, `src/office/web/settings.py`, and the existing CLI. Preserve the selected Workstation shell `docs/prototypes/auto-office-web-v2/LOCKED-CONTRACT.md`.
- Core contracts are already decided in [Lineage retention](https://github.com/Hikari9/auto-office/issues/602), [User authority](https://github.com/Hikari9/auto-office/issues/605), [Catalog publication](https://github.com/Hikari9/auto-office/issues/603), [Planner–runtime handoff](https://github.com/Hikari9/auto-office/issues/604) and [Adaptive routing](https://github.com/Hikari9/auto-office/issues/300). Don't reopen these in the UI implementation.
- Engineering should treat **this locked contract + the decision ticket + the interactive HTML** as the canonical reference unless explicitly superseded by a later maintainer decision. All branches and artifacts are review-only; no runtime, production or default catalog flags changed here.
