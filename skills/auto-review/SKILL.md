---
name: auto-review
description: Explicit standalone independent review of a specific commit, PR, diff or plan. Dispatch a distinct read-only reviewer in a fresh Herdr pane or fresh CLI session, return SHA-bound APPROVED, RECHECK or INTAKE_GAP, and let the invoking agent coordinate up to three substantive rounds. Never auto-activate or start an Office run.
---

# Auto Review — standalone, explicit only

This skill is a review primitive, NOT another Office lifecycle, hidden gate or repair orchestrator. Invoke only when the caller explicitly requests /auto-review. It works without the office CLI. Require: repository + immutable revision/base, acceptance criteria, relevant AGENTS.md, prior findings if rechecking, and an identified invoking/coordinating agent. For a plan, use an immutable plan digest instead of a code SHA. Missing decisive requirements produce INTAKE_GAP; do not invent a spec.

## Select an independent reviewer

Read the current effective reviewer-role policy if available (office route policy --effective --role code_reviewer), including source and user pins; a missing Office CLI is NOT a reason to invoke or install the Office lifecycle. Determine eligibility from current harness/model/effort capability, factual invocation contract, explicit allow/neutral/deny and override receipts, free-model opt-in, quota, availability, and independence from the producer/fixer. A user pin controls within remaining hard gates. Never bypass a deny or technical incompatibility to obtain the preferred model.

Preferred eligible chain, re-evaluated at each launch:
1. Codex GPT-6 Luna High.
2. OpenCode DeepSeek V4.1 Flash Max (only if actually available and opted in when classified free).
3. Claude Code Opus 5.5 Low.
4. AGY Opus 5.5 Low.

Disclose actual harness, exact invocation-model identifier, canonical model, effort, current role policy result and why earlier candidates were skipped. Do not assert launch capability on the basis of a catalog row alone.

## Launch and inspect

- If Herdr is healthy and a new pane can be safely registered, start a FRESH reviewer pane in the correct checkout/cwd, with the pinned immutable revision and read-only scope. Verify pane identity and launch/readback; do not reuse a producer pane.
- Otherwise start a FRESH independent reviewer CLI session using the verified harness adapter invocation form and enforced read-only permissions. Use only verified review-capable profiles, e.g. OpenCode's restricted office-reviewer agent. Do not use unrestricted worker flags or a non-isolated subagent of the requester as "independent".
- The reviewer may read files, diff, issue, tests and supplied evidence, and may write its isolated review report if needed. It must NEVER modify producer source files, push commits, approve its own changes, or invoke a repair worker. If no supported read-only reviewer can actually launch, report UNAVAILABLE with the concrete failure and preserve the review requirement.
- Compare the REAL base-to-revision diff, relevant standards and acceptance criteria. Check correctness, architecture, safety, integration, tests, versioning, risk, regressions, and evidence rather than trusting the producer summary. Detect changes to the reviewed SHA during execution and rebind review to an immutable revision before any verdict.

## Result contract

Output exactly one of APPROVED / RECHECK / INTAKE_GAP for a completed substantive review. Include reviewed immutable SHA or plan digest, reviewer identity/effort/harness, independence evidence, test evidence versus missing checks, and each finding as:
FINDING ID | severity (critical/high/medium/low) | blocking yes/no | file:line or concrete evidence | smallest actionable correction | suggested owner.

APPROVED is permitted only when no blocking finding remains on that same revision. RECHECK means the coordinator must resolve blockers and request another independent look. INTAKE_GAP means requirements/authority are genuinely missing; escalate that exact decision to the owner and do not infer consent. A reviewer that cannot launch, lacks source access, fails readback or produces an invalid reply gives UNAVAILABLE, not APPROVED, and consumes no substantive round.

## Invoker-controlled remediation; at most three substantive rounds

The agent that invoked /auto-review COORDINATES remediation; it need NOT personally fix anything. It can fix inline (executor invoker), delegate one or more fixes to separate executors (orchestrator invoker), or coordinate a specifically assigned other owner. Preserve explicit assignments. The reviewer remains independent, read-only and separate from the actual producer/fixer in every case.

Count only completed, valid substantive reviewer verdicts. The INITIAL review is round 1; repairs may earn round 2 and round 3, never round 4. Failed launches, unavailable routes, bad transcripts and invalid reviews do not consume rounds. Prefer the same independent reviewer context on a corrected SHA when safe, but never compromise independence. Maintain an invoker-owned ledger with round, reviewer identity, immutable SHA, verdict, findings, fix owner, and resolution. Changing fixer, caller or review surface never resets the cap.

After round 3, if blockers remain, STOP with RECHECK and outstanding evidence; no hidden waiver, self-approval, reset or manufactured APPROVED. INTAKE_GAP pauses for the owner's real decision and is not an authorization to continue. Report an unresolved review as blocked, not accepted.
