# AGENTS.md

Rules for every agent and model working in this repo. `CLAUDE.md` has the commands and architecture.

## Versioning (mandatory)

Every PR that lands on `main` bumps the release version in that same PR, following semver:

- **MAJOR:** breaking CLI, config or policy changes.
- **MINOR:** new features or behaviour, such as routing or catalog additions.
- **PATCH:** fixes, docs, tests and chores.

- The version is written in exactly one file: `src/office/_version.py`. Bump it there and nowhere else. `pyproject.toml` reads it dynamically, the plugin manifests carry none, and everything else imports `office._version` or `office.version`. Never hardcode a version string; `tests/test_version_single_source.py` fails if one appears.
- Add one line for the new version at the top of `CHANGELOG.md`.
- Parallel PRs that bump the same version conflict. Rebase, take the next free version, and update the `CHANGELOG.md` line.
- The bump is part of the change, not a separate release step. Do not skip it, and do not stop on a classifier block: report the exact command that was blocked.
