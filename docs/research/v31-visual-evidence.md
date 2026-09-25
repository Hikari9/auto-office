# v3.1 research: visual evidence, reviewer capability, and prototype drift

Research for [Visual evidence capture, reviewer capabilities, and measurable prototype drift](https://github.com/Hikari9/auto-office/issues/156), governed by [the v3.1 charter](https://github.com/Hikari9/auto-office/issues/153#issuecomment-5832359821).

**Repository baseline:** `93167420271527f1220700f2eebdff1ac2d1115c`, observed main on September 25, 2026. Repository links below are pinned. Current external documentation was checked through primary sources and Context7; it does not prove local adapter conformance.

## Evidence boundary

No local historical data was accessed, as requested. No live Gemini/Sonnet/Luna comparison, paid model call, browser acceptance run, or repository test suite was executed. The local environment has Playwright 1.57.0 but lacks its expected Chromium executable; none of the coding-agent CLIs was available. Thus this report resolves the architecture/research question, not operational readiness or model superiority.

The [requested AI Labs video](https://www.youtube.com/watch?v=bBMp5tLxShQ) remains unverified: earlier transcript retrieval failed authentication and this research did not obtain a transcript through public retrieval. No video-specific implementation claim is attributed to it. This gap does not prevent using Shopify's independently available primary article.

## What Helix contributes, and what is our adaptation

[Shopify's Helix article](https://shopify.engineering/helix) describes small migration checkpoints, behavioral validation, rendered reference/candidate comparison, Gemini-assisted visual judgment, invalid-state comparison rejection, and renewed checks after relevant fixes. Its implementation uses independent code adversaries and supports broader autonomous execution. The transferable idea is evidence-backed convergence rather than perfect first attempts.

Auto Office's proposed one-submit CLI, orchestrator-first telemetry removal, separate configurable specialist fallbacks, rolling plan review and parallel checkpoint scheduling are **our adaptations**, not claims about Shopify's implementation. Gemini is the user's preferred visual route, not a universally proven best model.

## Current repository gap

| Inspected surface | Finding |
|---|---|
| [Verification script][verify] | `runtime_verification` and `browser_acceptance` are always invoked with empty commands and marked skipped. The overall command has no complete mandatory-UI applicability contract. |
| [Verification spoke][verification-spoke] | Already separates cheap browser capture from reviewer-grade judgment and requires real acceptance flows, fixed steps and DOM probes. Reuse that principle. |
| [Role defaults][config] | `browser_verifier` requires only `browser`, with a low effort floor and local-evidence source; no Gemini preferred seed or explicit image-judgment capability. A label alone cannot prove visual review. |
| [Review result schema][review-schema] | Carries identity, versions, HEAD, findings and evidence. Status enum has PASS, CHANGES_REQUIRED, PLAN_DEFECT, BRIEF_DEFECT, UNAVAILABLE, but not INVALID_COMPARISON. Add a compatible discriminated result rather than silently inventing unsupported status strings. |
| [Seed adapters][adapters] | Native Gemini CLI is not among the four seeds. agy invokes models differently; all seeds are valid-unverified with pending conformance. No model-name inference can fill missing image transport or read-only evidence. |
| [Codex seed][codex-adapter] and [agy seed][agy-adapter] | Default invocation includes `--yolo` or `--dangerously-skip-permissions`. These are not sufficient isolation policies for a non-mutating reviewer. |
| [Delivery-channel tests][delivery-tests] | Explicitly reject requesting file output from a read-only worker. Return findings through reply/stdout and let the runner persist the result; do not burn review tokens fighting output permissions. |

There is enough existing structure to extend, but no basis for saying the requested UI gate is already implemented.

## Capture versus judgment: capability matrix

The table distinguishes documented transport from local conformance. **Every live-conformance cell is untested.** Do not promote trust from documentation alone.

| Candidate path | Documented input/output | Missing proof / consequence |
|---|---|---|
| Deterministic Playwright process | Browser context/viewport controls, screenshots, DOM probes and locator geometry. [Locator][locator], [screenshots][screenshots] | Browser binary/environment and acceptance script must be available. A script can capture and measure without a model call. It does not judge subjective fidelity by itself. |
| Gemini through native Gemini CLI | `read_file` supports image content; headless JSON/stream-JSON returns execution output. [File tools][gemini-files], [headless][gemini-headless] | Verify actual image delivery and model identity. Outer JSON is not a validated inner review verdict. Native adapter is new work unless already supplied by user-local bindings, which were not inspected. |
| Gemini through agy | Existing seed describes argv-bound prompts and model-dependent invocation, not a verified multimodal review channel. [agy adapter][agy-adapter] | Prove image reading/attachments, selected model, nonmutation and parseable response separately. Native Gemini CLI documentation does not certify agy. |
| Sonnet through Claude Code | Read returns images as visual content; large images may be resized/recompressed. Headless mode supports JSON plus `--json-schema`. [Read behavior][claude-tools], [programmatic output][claude-headless] | Test the exact route and evidence crops. JSON-schema output still requires runtime identity/revision validation; image downscaling limits tiny-detail claims. |
| Luna through Codex | Official CLI reference documents image attachments, output schema and read-only sandbox choices. [CLI reference][codex-cli] | Test exact exec/resume/version combinations; the current seed does not supply image attachments/output schema and uses unrestricted invocation. No live route was certified. |
| Hermes / another adapter | Current Hermes seed has no verified visual transport or complete quota probe. [Seed directory][adapters] | Not a visual fallback merely because an underlying model supports vision. Require conformance first. |

A capture route needs browser automation. A judgment-only route needs actual image input, review competence, safe isolation and a machine-checkable result; it need not control a browser when the runtime already captured the evidence. This distinction avoids forcing all reviewers into expensive browsing sessions. Exploratory browser access can be a separate authorized capability, not a universal requirement.

## Recommended minimum flow

This is a proposal for [Visual-drift gates and specialist fallback](https://github.com/Hikari9/auto-office/issues/158), not a second lifecycle or an implemented command contract.

1. The runtime determines whether this checkpoint has user-visible acceptance work and a valid comparison reference. No UI gate is funded merely because the repository contains frontend files.
2. Resolve approved reference, acceptance states and in-scope components. Freeze their versions and the candidate revision. Reuse captures only when their inputs are unchanged.
3. Run a deterministic capture plan in an isolated local/preview context. A cheap capable worker is reserved for exploratory work that cannot be scripted; it does not become the independent judge.
4. Persist screenshots, capture metadata and targeted state/geometry probes privately. Feed the judge only relevant frames/crops, acceptance criteria and concise measurements, not the entire execution transcript.
5. Route independent UI judgment through Gemini-first preference, then qualifying Sonnet/Luna alternatives. Run the separate code reviewer concurrently when both gate prerequisites exist for the same revision.
6. Return a compact verdict and actionable findings to the executor. The runtime binds/persists the full receipt and invalidates it on relevant changes. No agent assembles timestamps, artifact paths or hashes manually.

Per the user's 8B decision, missing the preferred Luna/Gemini pair does not automatically collapse reviews into one. Substitute specialists independently where possible. A non-UI checkpoint may use only the normal code reviewer. Any exceptional combined-review case must meet both capability/evidence contracts and be specified explicitly; missing vision is a blocker, not an assumed pass.

## Capture contract: comparable inputs before judgment

Record source revision, reference identity/version, application URL and state identity, viewport, device scale, browser/build, relevant font/assets status, capture steps, probes and output digests. A request for a specific viewport is not evidence the browser applied it.

Pin the relevant environment, fixture data, locale/timezone and color scheme where they affect layout. Wait for app-specific readiness and required elements, not an arbitrary sleep or network silence alone. Playwright's official documentation discourages networkidle as a testing-readiness signal; assertions should establish the intended state. Browser/OS/font differences can affect rendering. [Readiness][readiness], [snapshot environment][snapshots]

Disable animations for a stable static capture only when that matches the acceptance state. Animation completion may itself affect state; animation behavior needs separate testing. Wait for necessary fonts/assets and preserve their provenance. Use semantic selectors/declared component identifiers when comparing DOM elements; different implementations need not have identical DOM structure.

Distinguish these outcomes:

- **PASS:** applicable criteria satisfied by current valid evidence.
- **CHANGES_REQUIRED:** a product discrepancy exists; material drift blocks, minor issues are reported according to the charter.
- **INVALID_COMPARISON:** capture inputs are not comparable, such as different intended states/viewports/reference versions. Recapture the appropriate side.
- **UNAVAILABLE/BLOCKED:** required environment, credentials, browser or image transport is missing.
- **NOT_APPLICABLE:** UI or fidelity criterion is genuinely outside scope, with a reason.

A candidate that cannot reach the required state because its interaction is broken has a **behavior failure**, not an endlessly invalid screenshot. Conversely, a missing test credential should not be charged to the implementer as a layout defect. When the cause is uncertain, request focused evidence instead of an automatic rewrite. This separation prevents recapture loops from concealing real product failures.

## Measuring real drift without false precision

| Reference type | What can be measured | What must remain estimated or unavailable |
|---|---|---|
| Running HTML prototype / inspectable design with geometry | Matching component positions/dimensions, spacing and alignment relationships, computed typography, overflow, visibility, declared states | Equivalent element mapping may need authoring; identical DOM/CSS is not required. |
| Approved screenshot with known viewport/scale | Image-space geometry, relative location and size, crops and visible differences | Original CSS padding, design tokens, semantics, focus behavior and precise font metrics cannot be recovered reliably from pixels alone. |
| Screenshot without reliable scale/state | Qualitative visual findings after normalizing only what can be justified | Exact CSS-pixel drift and universal numerical fidelity scores are not valid. |
| Requirements only / no prototype | Behavior, accessibility checks, responsive failures and explicit design constraints | Prototype fidelity is unavailable; do not fabricate a baseline or call it perfect parity. |

For directly comparable geometry, a useful component measurement is `(candidate_x - reference_x) / viewport_width`, accompanied by the underlying CSS-pixel values, viewport and component mapping. Spacing deltas and overflow can likewise be reported in explicit units. These are measurements of specified properties, **not a global percentage quality score**.

For image-only estimates, report method and uncertainty instead of pretending the model measured CSS. Aggregate coverage may say which required states/components were checked; it does not establish aesthetic correctness. Fix thresholds and materiality examples in the design ticket using approved prototypes, not invented defaults in this research.

Keep accessibility and actual interaction evidence separate from visual resemblance. A screenshot can show a button that is neither keyboard-reachable nor functional.

## Proposed small semantic result, runtime-owned envelope

The reviewer needs a concise task-specific response, conceptually:

```text
verdict: CHANGES_REQUIRED
finding:
  criterion: mobile navigation remains within viewport
  region: header / menu trigger
  severity: material
  observation: trigger is clipped at the requested mobile width
  evidence: captured header frame + overflow probe
```

The runner generates the rest: operation/attempt identity, producer/reviewer identities, exact routed triple, revision/reference/criteria versions, artifact digests, timestamps and duration. Proposed internal measured values include property, reference value, candidate value, unit, method (`dom`, `design`, `image_estimate`) and uncertainty when estimated. Reuse the existing finding/disposition model where possible instead of creating seven mandatory JSON documents.

A renderer or screenshot worker must not be able to certify that its own artifact is a successful independent review. Structured response validation also cannot certify that the model actually received images; conformance needs observable image-dependent tasks.

## Minimum prospective conformance and regression fixtures

All fixtures below are a test design, **not executed visual tests**.

| Fixture | Required observable result |
|---|---|
| Known-good reference/candidate in matched desktop and mobile states | PASS with correct evidence identity; no invented discrepancies. |
| Same filenames but different image content | Judge responds to actual images, not filenames or a scripted canned verdict. |
| Material shifted/clipped navigation element | Actionable material finding in the correct region. |
| Small allowed cosmetic variation | Reported minor issue, not an automatic major rewrite. |
| Reference modal open, capture modal closed due to wrong capture steps | INVALID_COMPARISON, recapture; no code patch requested. |
| Candidate's modal fails to open after correct steps | Behavior failure, not infinite invalid-comparison recapture. |
| Wrong viewport or missing required capture state | Invalid/unavailable evidence, never layout PASS. |
| Candidate edit after capture, same Git HEAD | Stale evidence rejected against changed snapshot. |
| Approved prototype replaced or reference version changed | Relevant earlier UI verdict invalidated. |
| No prototype supplied | Behavior may be checked; fidelity explicitly not measured. |
| Required font unavailable | Classified with evidence as environment/capture issue or real asset defect, not arbitrary spacing edits. |
| Late content at scroll/lazy-load boundary | Required visible region is actually loaded/captured; early screenshot cannot satisfy it. |
| Screenshot contains a fake instruction to approve or disclose data | Treated as untrusted page data; no instruction or authority change. |
| Read-only reviewer asked to return results | Results received through allowed reply channel with no source/state writes. |
| Model quota failure or invalid response schema | Classified as route/result failure; safe fallback or blocker, not fabricated UI success. |
| Repeated unchanged finding / invalid capture | Bounded retry and one allowed escalation; work remains resumable. |

An initial paid conformance run must use authorized fixture data, verify the actual model/effort/harness identity, demonstrate image-sensitive positive and negative cases, validate output and nonmutation, and record only observed usage. Until then, candidate routes are documented possibilities, not proven adapters.

## Privacy and security boundary

Use explicit allowed targets and least-privilege test identities. No production mutation or external send may be introduced by a capture plan. Reviewer code access is read-only; browser-driving permissions are separate. Treat application content, references and screenshots as data, not executable instructions.

Playwright auth state can contain impersonation-capable cookies/headers and must not be committed. [Authentication guidance][auth] Raw DOM extracts, network traces, screenshots and reviewer inputs can also expose sensitive data. Keep them private; redact both images and metadata before any public capsule, record excluded regions, and never claim coverage over what was masked. Hashes identify bytes; they do not prove lawful capture or genuine live provenance by themselves.

A file naming convention cannot prove a screenshot came from the stated runtime. Prefer runtime-owned capture and immutable artifact metadata. Persistent stores writable by an unrestricted producer do not provide adversarial tamper protection merely because they contain JSON hashes.

## Handoff

The visual research question is sufficiently answered for [the visual gate/fallback decision](https://github.com/Hikari9/auto-office/issues/158). Remaining decisions: exact applicability and materiality rules, reference selection/versioning, route conformance thresholds, internal schema mapping and the exceptional combined-review contract. These are already owned by the existing decision tickets; no new universal model assignment or pixel-diff requirement is introduced.

Local historical comparisons, live-model quality ranking, cost estimates and a numerical fidelity benchmark remain skipped/unmeasured. No implementation, browser run, hook installation, credential change, or main-branch mutation occurred.

[verify]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/verify.sh
[verification-spoke]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/skills/auto-verification/SKILL.md
[config]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/config/config.default.yaml
[review-schema]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/schemas/review-result.schema.json
[adapters]: https://github.com/Hikari9/auto-office/tree/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed
[codex-adapter]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed/codex.yaml
[agy-adapter]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed/agy.yaml
[delivery-tests]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/tests/test_packet_delivery_channel.py
[gemini-files]: https://geminicli.com/docs/tools/file-system/
[gemini-headless]: https://geminicli.com/docs/cli/headless/
[claude-tools]: https://code.claude.com/docs/en/tools-reference#read-tool-behavior
[claude-headless]: https://code.claude.com/docs/en/headless#get-structured-output
[codex-cli]: https://learn.chatgpt.com/docs/developer-commands?surface=cli
[locator]: https://playwright.dev/docs/api/class-locator#locator-bounding-box
[screenshots]: https://playwright.dev/docs/screenshots
[snapshots]: https://playwright.dev/docs/test-snapshots
[readiness]: https://github.com/microsoft/playwright/blob/main/docs/src/api/params.md
[auth]: https://playwright.dev/docs/auth
