---
name: auto-self-improve
description: Issue-only Auto-Office bug-report workflow, run by the orchestrator. Use on a `bug?:` line, on any Auto-Office runtime/skill/adapter/hook defect you notice yourself, or when `office self-improve` is invoked. Decide it is an Office defect, dedupe against open and closed issues, sanitize, and file with `gh issue create` in Hikari9/auto-office, directly or through a cheap read-only subagent. Never fix bugs, edit source, commit, open a PR, or close issues.
---

# Auto Self Improve: issues only

**Hard boundary:** Self-improvement means evidence-backed **GitHub bug filing**, NOT self-repair. Never modify repository content, amend model catalogs, create worktrees or branches, commit, open or merge PRs, or close issues. Historical v3 proposal scripts are NOT part of this skill.

The runtime does not investigate or file. It prints a non-blocking `bug?:` line when an outcome looks like an Office defect, and `office self-improve` prints these steps plus the run's current `bug?:` signals. You decide what is a defect and you file it.

1. **Trigger.** Run the steps below on a `bug?:` line, or on any Office defect you notice yourself (for example a `next:` line that contradicts a refusal, or a wrong dispatch base). A `bug?:` line is a hint, not a verdict.
2. **Decide it is an Office defect.** Include Office runtime, CLI, skills, routing, adapters, hooks and integrations whose handling Office owns. Exclude target-application bugs, normal user or gate refusals, and third-party outages with no Office defect. Weak suspicions stay private until more evidence arrives.
3. **Gather evidence.** Collect it yourself, or delegate to a cheap read-only subagent (`model: "haiku"`) that only reads and summarizes. Raw evidence stays private.
4. **Dedupe.** `gh issue list --repo Hikari9/auto-office --state all --search "<keywords>"`. Reuse a match instead of filing a duplicate.
5. **Sanitize.** Write expected vs actual behavior, impact and safe reproduction steps. Leave out person, repository, path, account, credential and customer identifiers (`protocol/privacy-self-improvement.md`).
6. **File.** `gh issue create --repo Hikari9/auto-office`, directly or through the subagent. Report the issue URL; never claim a filing without one.

Filing never blocks delivery: landing and closeout continue whether or not an issue is filed. A failure to file is not a new incident to investigate. Read `protocol/privacy-self-improvement.md` for the privacy rules. The legacy `scripts/office_propose.py` and related proposal material are historical only.
