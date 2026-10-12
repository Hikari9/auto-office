# Model Registry & Routing Policy — Wayfinder Prototype (Issue #606)

**Status: three interactive exploratory UI options; direction NOT yet selected.**  
**Parent:** [Auto-Office deterministic catalog lifecycle Wayfinder](https://github.com/Hikari9/auto-office/issues/601).  
**Decision ticket:** [Prototype model catalog and user-routing settings without YAML edits](https://github.com/Hikari9/auto-office/issues/606).

[Open the self-contained interactive HTML](index.html).

## Scope, precedence and authenticity

This is a **sanitized sample-state design artifact** for the already-selected Auto-Office **Workstation** shell (desktop rail + repository rail + canvas). It contains NO live source data, credentials, backend calls, API bindings, GitHub mutations, real provider quota values, or runnable Office commands. Modeled benchmark sample numbers are demonstrative; the UI explicitly marks them as such. Every Save / Approve / Rollback / Scan action is simulated in-page only. The proposed `office model ...` commands are **not implemented**.

The current production Settings surface already has a real Machine → Repository → Run cascade with provenance. The current `office onboard` uses native harness question UI and validated CLI writes. The new model-policy experience must **extend** rather than silently replace those authorities. Global user-owned model policy ships first; repo-scoped exceptions are later work [#607](https://github.com/Hikari9/auto-office/issues/607); run-pinned values are read-only. The Workstation layout is already the chosen shell in `docs/prototypes/auto-office-web-v2/LOCKED-CONTRACT.md`.

## Compare three information architectures

| Option | Concept | Best use | Cost / trade-off |
|---|---|---|---|
| **A — Registry Grid** | Dense, searchable, horizontally scrollable table with sticky model identity and factual status versus user policy in separate columns | Frequent routing/benchmark inspection; many rows and effort variants | Tighter scan, less context until Inspect is opened |
| **B — Model Cards** | Responsive card gallery showing route variants, policy and status at a glance | First-run overview, exploration and touch/mobile | More space per route; comparatively slow for dozens of models |
| **C — Split Inspector** | Compact model list beside persistent detail inspector with evidence, provenance, each effort and editable policy | Deep inspection and source-traceable decisions | Less table-wide comparison; inspector moves under list on mobile |

A/B/C are **alternatives for the Model Registry surface only**. All share the same policy, update and CLI contract. They do **not** reopen the earlier selected Workstation shell.

### Prototype interaction checklist

1. Switch A/B/C from the **Display concept** selector. Try search, harness filter and "Needs probe"/"Filtered" states.
2. Inspect a route such as Sol 6.1: **Allowed by you** and **effort probe pending** are independent.
3. Open **My routing policy**: toggle Codex or Claude, set Sol `Deny ≤ 5.6`, or set a model to **Manual only** / **User-denied**. New Sol 6.1 remains permitted under `Deny ≤ 5.6`; a low benchmark or high cost does not create a disable.
4. Preview the effective policy and use **Undo demo changes** or **Save demo policy**. Both are page-only, no real write.
5. Click **Simulate first Run**: choose to continue with all-allowed defaults or to review filters, non-blocking.
6. **Updates & rollback**: see six-stage pipeline, routine verified auto-publish classification, approval-required *validated* capability change (demo approval), withheld unverified effort mapping and invalid model rejection. Preview a before/after diff, inspect snapshot history and preview rollback.
7. **CLI parity**: copy proposed future command shapes clearly labeled **not yet implemented**.

### Recommended next design decision (requires maintainer feedback)

Select A, B, C, or a hybrid. Suggested hybrid: **A for desktop registry comparisons + C as its inspector + B-like cards for narrow/mobile widths**. This avoids three parallel production registry UIs while preserving inspectability and mobile readability.

## UX implementation invariants

- **Facts, filters and economics are separate:** registry model/harness/effort identities and benchmark provenance are factual; user allow/deny and version ceilings are explicit intent; installed/sign-in/quota/trust/probe status are runtime observations.
- **Release lineage:** active registry retains newest two known stable generations of each *named lineage* globally, independent of effort variant and machine. Older generations accessible in archive/audit, never silently restored into routing.
- **State provenance:** distinguish source shipped default, machine/user, repository (follow-up), run-pinned and runtime. Default user policy permits every technically supported model; absence of exact effort conformance must be labeled unverified rather than disabled.
- **First Run:** quick-start ask, not a setup blocker. No implicit policy mutation on skipping.
- **Updates:** user-invoked/opted-in update scans; validated routine changes may auto-publish within that transaction; meaningful valid but material changes request approval; invalid evidence cannot be approved into technical truth.
- **Rollback/replay:** transactional snapshot restore never rewrites user preferences or live pinned runs. Preview diff shows effect and provenance before action.
- **Runtime ownership:** Web invokes the same Office CLI/runtime mutations and re-reads confirmed values; browser state is never the authority. Respect source freshness and idempotency receipts.
- **Quota and per-task allocation:** economically expensive routes remain user-permitted, planner gets a route economics sheet, runtime obeys binding primary/fallback envelopes and passive headroom reassessment. Detailed cost telemetry remains opt-in.
- **Presentation:** desktop Workstation blue/green/purple, semantic yellow/red. Grid sticky first column + horizontal overflow; cards responsive 3→2→1; split inspector collapses to vertical flow. Mobile rail becomes horizontal header. No credentials or private process details embedded in this artifact.
- **Accessibility:** controls have labels, dialog semantics, keyboard Escape close and focus target. Production must additionally implement full focus trapping/restoration and server-reconciled pending/error states.

## Review and next steps

The human-in-loop prototype ticket must remain OPEN until the maintainer chooses a direction. After selection, record the locked desktop/mobile interaction contract, replace the exploratory branch with a durable canonical reference (plus visual screenshot), and feed implementation from the issue; **do not mistake creating this prototype for shipping the model policy/runtime changes**.

Grounding: [Model lineage](https://github.com/Hikari9/auto-office/issues/602), [User policy](https://github.com/Hikari9/auto-office/issues/605), [Catalog publish](https://github.com/Hikari9/auto-office/issues/603), [Planner/router handoff](https://github.com/Hikari9/auto-office/issues/604), [Repo follow-up](https://github.com/Hikari9/auto-office/issues/607), `src/office/web/static/settings.js`, `src/office/web/static/settings.css`, `src/office/web/settings.py`, `docs/web-ui.md`, and `docs/onboarding.md`.
