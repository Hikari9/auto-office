---
name: auto-adapter
description: Register or change an Auto Office harness adapter (one per CLI) and its catalog model rows with `office harness scaffold|validate|smoke|list` and `office model add|list|disable`. Covers the 3.1+ checklist, what each permission/trust flag grants, the Herdr interactive-form requirement, and why nothing in this flow promotes trust.
---

# Auto Adapter

Adapters are data, never separate lifecycles. A new harness needs no Python edit.

## Register a harness (3.1+)

1. `office harness scaffold <id> --binary <path>` probes `--version`/`--help` and drafts
   `~/.config/auto-office/adapters/<id>.yaml` (next to the user config) with every mandatory
   field marked TODO, plus a fake-binary test stub in `adapters/tests/`. A seed id is refused
   unless `--override-seed` (the file then sets `override_seed: true`); otherwise seed wins.
2. Fill every TODO from the harness docs and `--help` of the pinned version: worker (and only
   if proven read-only, reviewer/vision) `argv`, `prompt` (stdin | argv | argv-bound, never a
   shell), `effort_mapping` for every effort a row uses (null drops the flag), `preflight`
   (`model_check` lists the pinned slug, `auth_check` exits 0), `quota_probe` (null is unknown,
   never unlimited), `failure_signatures`, `source_notes` with version and date.
3. `office harness validate <id>`: schema, no TODO left, argv renders at every effort with no
   unknown `{placeholder}`, safe prompt transport, effort map covers catalog efforts, every
   permission/trust flag justified, `herdr_kind` has an `interactive.argv`, every
   preflight/quota/version/model command resolves on this host.
4. `office model add <id>/<provider/slug> --effort medium[,high]` appends rows to the user
   catalog overlay (`catalog.yaml` beside the user config), never the packaged seed. Scores
   come later through `office benchmarks brief|submit`; there is no second path.
5. `office harness smoke <id> --model <m>`: one launch in a throwaway git repo, no run
   identity, no pty. Recorded in `harness-smoke.jsonl` as launch evidence only. It refuses
   a form with a permission/trust flag unless `--allow-unsafe-flags` is passed.
6. `office harness list` shows origin, install, version, sign-in, rows, trust and last smoke.
   `office model list [<id>]`; `office model disable <id>/<model>[@effort] --reason ...`
   sets `dispatchable: false` through the overlay.

## Trust posture

- Trust stays `valid-unverified` until a recorded trust act (`office approve trust <route>
  --quote "<user's words>"`). Validate, model add and smoke never record trust or conformance.
  Never write `verified_state: proven`.
- Never auto-approve a login, trust or permission prompt. A smoke that stops on one fails.
- For each permission flag, answer before using it: what it lets the agent do unprompted
  (edits, commands, network, project config/MCP/extensions), in which directories, and
  whether a reviewer form can avoid it. Record the answer under `trust_justifications.<flag>`.
  Prefer declining project-resource trust (pi uses `--no-approve`) over granting it.
- A reviewer/vision profile needs verified read-only isolation; without it declare only a
  worker (`capabilities: [builder]`).

## Herdr

Setting `herdr_kind` promises a pane-hosted form: add `interactive.argv` with the same trust
flags and pinned model/effort, or Herdr launches silently degrade (#479).

3.0 runs keep `office raw scaffold-adapter`/`validate-adapter`; never use them for 3.1+.
