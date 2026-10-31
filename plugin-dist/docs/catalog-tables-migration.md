# Shipped model catalog: hybrid TSV + YAML (#852)

## Authority and intent

The **one shipped model registry** is four Git-tracked files in `catalog/`:

| File | Owns | Edit rule |
| --- | --- | --- |
| `routes.tsv` | Ordered model/harness/effort identities, dispatch slugs, scalar provenance, eligibility, and explicit field presence | One row per unique `harness/model@effort` |
| `benchmarks.tsv` | Version-pinned benchmark index observations, including explicit null observations | One row per `route_key, index_version`; never infer scores |
| `manifest.yaml` | Schema version, seed kind, index version and narrative index notes | No model rows |
| `provenance.yaml` | Nested price/speed/source/snapshot/supersession metadata and any future non-tabular fields | Exactly one mapping per `route_key` |

`catalog/lineages.yaml`, archived nonrouting YAML snapshots, adapter definitions and `config/config.default.yaml` are **unchanged**. Policy remains where it was: user role allow/deny and preferences retain precedence; no second `policy/defaults.tsv` is added. The existing `~/.config/auto-office/catalog.yaml` overlay and `office model add|list|disable` retain their format and authority. No SQLite or service is needed.

`src/office/shipped_catalog.py` joins the files into the **identical legacy-shaped in-memory object** that `catalog/seed.yaml` used to provide. All runtime readers, hash/caching paths and CLI listing use this one strict loader; there is no runtime fallback to a second editable YAML model source.

## Representation and round-trip rules

The order of `routes.tsv` is semantically significant and is preserved in normalized model lists. Keys are `<harness>/<model_id>@<effort>`, using the original exact model and harness IDs; `invocation_model_id` remains separate from `model_id`. Alias regexes stay literal strings and continue resolving via the existing lineages code.

All scalar seed fields reside in `routes.tsv`: `model_id`, `invocation_harness`, `effort`, `invocation_model_id`, `invocation_source`, `source_identifier`, `source_name`, `source_effort`, `effort_confidence`, `alias_family`, `dispatchable`, `discovery`, `discovery_reason`, `free_model`, `release_date` and `content_hash`. The loader preserves the distinction between absent, null, false, zero and empty string.

- `\M` means **missing** (key absent in the original row); `\N` means **YAML null**. They are never equivalent to blank, zero or false.
- Empty TSV cell means an **empty string**; booleans are exactly `true` or `false`.
- Strings beginning with a literal backslash use an additional leading backslash. Python's `csv` TSV dialect quotes fields containing tabs, quotes or line breaks.
- `benchmark_shape=map` means a benchmark map (which may be empty); `null` and `missing` preserve the other two states. A benchmark observation score can be `\N`, but a missing row never gets an invented score.
- Benchmark scores retain their numeric type and original exact index key; nested YAML preserves source notes, citations and unknown future per-row fields. Each benchmark row joins to the provenance mapping through `route_key`; do not duplicate the same citation as an independently editable field.

For example, a single benchmark correction edits **one TSV score cell**, with source evidence edited only in the corresponding provenance mapping. Adding a model takes one TSV route row, zero or more benchmark rows, and one provenance entry rather than a repeated 15–25-field YAML model block. The layout reduces change-locality noise; it does not promise faster routing.

## Legacy parity evidence

The migration baseline was `main` commit `9e5d1212a3419a0a471047361e8f37a1d66cdb49` on 2026-10-28, with **71** model/harness/effort rows. Its canonical normalized hash (using `office.util.sha256_obj`) was:

```text
sha256:18bc86983f9daa9b777c39e21947db533be04c8ca6e97bb2286b5f5d8fc70f26
```

After conversion, all 71 assembled rows are structurally equal to the old YAML model rows (including per-field absence/null, index values, prices, source snapshots, supersession and row ordering), with the **same canonical hash**. Because `config.snapshot_hashes()` and `scripts/office_runtime.py` hash reconstructed normalized data instead of serialized TSV bytes, benign formatting edits do not change a run's catalog identity, and pre-migration seed-shaped pinned records retain hash compatibility. Older runs and user overlay data are not rewritten.

To reproduce a conversion comparison from an existing seed file without introducing a second runtime authority:

```python
import tempfile, yaml
from pathlib import Path
from office import shipped_catalog
from office.util import sha256_obj

old = yaml.safe_load(Path("/path/to/legacy-seed.yaml").read_text())
with tempfile.TemporaryDirectory() as tmp:
    shipped_catalog.export_legacy(old, Path(tmp) / "catalog")
    new = shipped_catalog.load(Path(tmp))
    assert new == old
    assert sha256_obj(new) == sha256_obj(old)
```

The converter is explicit and refuses duplicates/malformed data. Runtime never reads an old seed. For new catalog additions or score updates, review and record any intentional changes to the normalized hash. The legacy migration hash above documents the one-time parity comparison; future catalog edits are not blocked by a frozen test expectation.

## Guardrails, rollback and coordination

Malformed TSV, duplicate model or score keys, invalid effort/boolean/number, unknown benchmark routes, missing provenance entries, incompatible schema version and invalid joins fail closed with file/line diagnostics. Benchmark source authenticity and confidence are still governed by the preexisting benchmark refresh validation, not inferred by this format. Explicit #809 L-only unscored Argon-medium policy remains a narrowly scoped policy decision, **not** a general catalog waiver. Issue #633's three-column CLI runtime manifest is separate and unaffected.

Rollout requires the targeted catalog/route/overlay/CLI/lineage regressions, snapshot-hash parity, a packaged-wheel smoke test, and independent final-SHA review. Any collision with concurrent catalog PRs (for example #851 adding Argon rows) must be rebased by translating those added rows into the new shipped files, without dropping their proven provenance. Do not change effective routing policy while reconciling.

To roll back, revert this entire migration commit/PR: Git history retains the old `seed.yaml`, while user config, overlays, stored runs, archive and lineages have not been rewritten. **Do not** manually edit `seed.yaml` beside the TSVs or automatically migrate home-directory data.
