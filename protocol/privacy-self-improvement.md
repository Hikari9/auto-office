# Privacy and self-improvement — issue-only contract

Active `auto-self-improve` never proposes code, branches, catalog modifications or PRs. It may only
investigate Auto-Office-owned bugs read-only in isolated environments and file evidence-backed GitHub issues.
The runtime's durable observer monitors the entire run, backfills on every land/close attempt including
failures, and persists retryable incidents independently of prunable run details.

Private logs, user repositories, paths, org/person names, hosts, credentials, and project data stay private.
Public issue bodies contain only deterministic sanitized summaries, an opaque identity marker, expected/actual
behavior, reproduction when known, and evidence confidence. A runtime-owned publisher, not the investigator,
performs the sole permitted remote write: GitHub issue creation in `Hikari9/auto-office`. Weak suspicions stay
private. GitHub and model failures never block the primary run and must be retried and reported visibly.

Legacy 3.0 dream/pattern and catalog proposal workflows (`scripts/office_propose.py`,
`references/self-improvement-graduation-bar.md`, `docs/v3-self-improvement-evidence.md`) are historical evidence,
NOT instructions for current runs or for this skill. They must never run from `auto-self-improve`.
