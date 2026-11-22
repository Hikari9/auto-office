# Artificial Analysis benchmarks: Meta Muse Spark 1.3 via opencode (added 2026-10-14)

Snapshot taken 2026-10-14 from artificialanalysis.ai per-model pages
(`/models/muse-spark-1-3-xhigh`, `/models/releases/muse-spark-1-3`). Intelligence scores are
**Artificial Analysis Intelligence Index v4.3.2** (the version both pages publish: "v4.3.2
incorporates 10 evaluations: AA-Briefcase v1.1, GDPval-AA v2.1, AutomationBench-AA,
Terminal-Bench 4.0, SciCode, Humanity's Last Exam, GDP.pdf, CritPt, AA-Omniscience, AA-LCR
v1.1") and are not comparable with other index versions. See `catalog/seed.yaml`
`benchmark_index_notes`.

AA publishes two effort entries for this release — **Max** and **Xhigh** — and no
`(low|medium|high)` variant. There is deliberately no seed row for any effort without a
same-effort AA page: the benchmark rules map each row to the page for the same model and
the same reasoning effort, and an unclear effort mapping is left out, never interpolated.

| Model | Intel. | $ in /1M | $ out /1M | $/task | Tok/s | TTFT | Released |
|---|---|---|---|---|---|---|---|
| Muse Spark 1.3 (Max) | 48 | $1.25 | $4.25 | $1.60 | 124.2 | n/a | 2026-09 |
| Muse Spark 1.3 (Xhigh) | 45 | $1.25 | $4.25 | $1.37 | 220.9 | 28.99s | 2026-09-02 |

Prices are Meta's first-party API list prices as published on the AA pages (input / output
per 1M tokens). Speed and TTFT are AA's first-party API measurements, not local CLI
performance. The catalog row records this in `metrics_context` ("AA first-party API
performance; not local CLI conformance"). The model is proprietary (Meta, 1M-token
context, text/image/video in, text out); weights are not available.

Only the **Xhigh** entry becomes a seed row. The Max entry has no seed row on purpose:
`#max` is not a usable variant for this slug locally (see below), and a row for it would
be a benchmark-only claim over an undispatched effort.

## Local harness evidence (the invocation side, not the benchmark side)

The Xhigh row is routable because `opencode` 2.0.29 reaches this slug locally, not because
AA lists it. Recorded 2026-10-14 on this machine:

- `opencode models` lists `opencode/muse-spark-1.3-contributor-free`, so the row pins that
  provider-qualified slug in `invocation_model_id` and never resolves an ambiguous default.
  Preflight (`opencode models` plus `opencode auth list`, read from output rather than the
  exit code) requires the pinned slug there before every headless launch.
- `office harness smoke opencode --model opencode/muse-spark-1.3-contributor-free
  --effort xhigh` exited 0 (launch evidence recorded in `harness-smoke.jsonl`,
  2026-10-14T12:44:06Z, opencode 2.0.29). A medium smoke of the same slug also exited 0
  earlier the same day (2026-10-14T12:35:36Z); medium stays on the user overlay because AA
  publishes no medium page for this model (`/models/muse-spark-1-3-medium` is 404), so a
  scored seed row for it cannot be written without inventing a mapping.
- Variant behavior, re-verified against opencode 2.0.29 at registration (see
  `adapters/seed/opencode.yaml` `source_notes`): the `#variant` suffix on `--model` is the
  per-run effort selector; `#none` and `#max` fail fast with `Variant unavailable`, and an
  unknown model fails fast with `Model unavailable`. Exit 0 is therefore launch evidence,
  **not** model conformance: Office reads no model identity back from opencode yet, and the
  row's `invocation_source` says exactly that.

Only `xhigh` is a dispatchable seed effort. No `--thinking`-style level is proven to change
model behavior beyond launching, and no other effort row is added until its exact
model/effort conformance is proven against the local harness.
