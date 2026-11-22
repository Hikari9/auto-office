# Gemini 4 Hydrogen: gated executor registration and owner seeds (2026-11-14)

**Status:** [Draft PR #1142](https://github.com/Hikari9/auto-office/pull/1142) is registration and advisory intent **only**, not qualified dispatch or an activated canary. [Issue #1143](https://github.com/Hikari9/auto-office/issues/1143) is the authoritative approved implementation and release gate. [Issue #862](https://github.com/Hikari9/auto-office/issues/862) separately owns benchmark-version research. Older XS/S-first seed drafts and the “decision pending” language in earlier revisions of this note are superseded by the owner's final 2026-11-14 decision.

## Registered routes — documentation, not authenticated local proof

| Office route | Candidate `agy --model` ID | Antigravity proof |
| --- | --- | --- |
| `agy/gemini-4-hydrogen@low` | `gemini-4-hydrogen-low` | Pending exact model/effort readback |
| `agy/gemini-4-hydrogen@medium` | `gemini-4-hydrogen-medium` | Pending exact model/effort readback |
| `agy/gemini-4-hydrogen@high` | `gemini-4-hydrogen-high` | Pending exact model/effort readback |
| `agy/gemini-4-hydrogen@xhigh` | `gemini-4-hydrogen-xhigh` | Pending exact model/effort readback |

The IDs are **external/third-party evidence** and must not be labeled locally proven. All rows remain `dispatchable: false`, `discovery: eligible`; no shipped role allow, missing-score floor waiver, global trust change or policy exception enables them. The agy adapter encodes thinking effort in the combined model ID, not a separate `--effort` argument. Confirm installed `agy models`, selected identity, effort, builder tool execution, workspace write isolation, subprocess cleanup, permissions, quota and fallback on the authenticated account before claiming qualification. `office doctor --probe-route agy/gemini-4-hydrogen@<effort>` can record exact conformance receipts when the platform's required OS-enforced sandbox and model identity readback are available.

**Benchmark isolation:** the shipped routing index is **Artificial Analysis Intelligence Index v4.3.2**, and every Hydrogen row's score for that index remains **null**. Published **v5.0.1** headline index values (Low 27, Medium 41, High 51, Xhigh 54) are *reference-only evidence*: they are not an Office coding composite, cannot be compared to v4.3.2 floors and cannot create a v4 score. Google's published API list prices of **$1.50 input / $7.50 output per million tokens** (November 12, 2026) are not actual Antigravity subscription or Office quota consumption.

Sources: [Google release](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-hydrogen/), [Google DeepMind model card](https://deepmind.google/models/model-cards/gemini-4-hydrogen/), [Artificial Analysis Hydrogen v5.0.1](https://artificialanalysis.ai/models/releases/gemini-4-hydrogen), and [third-party Antigravity listing](https://github.com/AGI-is-going-to-arrive/Antigravity-Context-Window-Monitor/releases) (**not** local conformance).

## Final owner-approved XS–XL advisory preferences

The arrow means soft escalation and `|` means qualified same-rung peers, selected by task-fit, cost, quota, reliability and seed bias. The **staged YAML is flat** and does not implement equal-rung semantics; #1143 owns that runtime change.

| Size | Preferred and subsequent peers | Hydrogen authorization after exact conformance |
| --- | --- | --- |
| XS | Haiku 5.5 Medium → Terra 6.1 Low \| Hydrogen Low | **Manual-only**, no automatic XS trial |
| S | Terra 6.1 Medium \| Hydrogen Medium → Sonnet 5.5 Medium | Only gated low-risk S automatic canary |
| M | Sonnet 5.5 Medium \| Hydrogen High → Terra 6.1 High | Only gated low-risk M automatic canary |
| L | Sonnet 5.5 High → Hydrogen High | **Manual-only**, no Opus Low L fallback |
| XL | Opus 5.5 Medium → Sol 6.1 Medium \| Argon High | **Hydrogen excluded** |

Unknown task size is not L and does not automatically qualify Hydrogen. Terra High at M remains role-denied pending a narrowly scoped, tested policy change. Fable 5.2 remains planner-only in separate [PR #1133](https://github.com/Hikari9/auto-office/pull/1133); Hydrogen gains no reviewer privileges. **Keep Gemini 3.8 Flash registered for visual review** until a separate successful *image-sensitive* Hydrogen conformance and replacement review.

## Activation gate — remains open

The owner authorized *after conformance* automatic trials **only for low-risk, bounded, reversible local/repo S and M executor tasks**, with known qualified same-size fallback and independent review: **at most 3 Hydrogen automatic attempts in any rolling 20 eligible S/M opportunities and 1 per run**, with conservative cold-start and atomic cross-process accounting. This path is **not yet implemented or live**. Count launched trials that fail; prohibit bypass via role allows, generic discovery/exploration, benchmark substitutions or alternate efforts. Exclude security, credentials, financial/payment, privileged, production data, infrastructure and irreversible tasks. Protect independent reviewer quota and historical pinned runs.

All code/test gates, real local conformance receipts, the separate #1143 capped canary implementation, independent review and rollback/smoke proof must pass before merge or deployment. The earlier owner-provided report recorded **zero Hydrogen executor attempts**; its proxy cost estimates are not Hydrogen operating results.
