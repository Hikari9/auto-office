# Privacy and self-improvement: issue-only contract

Active `auto-self-improve` never proposes code, branches, catalog modifications or PRs. The orchestrator
(or a cheap read-only subagent it delegates to) may only gather evidence for Auto-Office-owned bugs and file
GitHub issues in `Hikari9/auto-office`. The runtime investigates and files nothing.

Private logs, user repositories, paths, org/person names, hosts, credentials, and project data stay private.
Public issue bodies contain only sanitized summaries, expected/actual behavior, reproduction when known, and
evidence confidence. Search open and closed issues first and reuse a match. Issue creation in
`Hikari9/auto-office` is the sole permitted remote write. Weak suspicions stay private. A failure to file
never blocks the primary run.

Legacy 3.0 dream/pattern and catalog proposal workflows (`scripts/office_propose.py`,
`references/self-improvement-graduation-bar.md`, `docs/v3-self-improvement-evidence.md`) are historical evidence,
NOT instructions for current runs or for this skill. They must never run from `auto-self-improve`.
