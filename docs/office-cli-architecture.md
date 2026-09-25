# Office CLI Architecture & Agent Hook Specification

## 1. Purpose and Scope

### 1.1 Goal
This document specifies the architecture for a single unified global CLI binary (`office`) and a portable agent-hook boundary across all supported AI agent harnesses (`claude`, `codex`, `gemini`, `agy`, `hermes`, and `herdr`). It defines run discovery, command semantics, hook lifecycle, packaging, and harness interoperability.

### 1.2 Settled Constraints (#140)
- **Hybrid Hook Boundary**: Agent hooks perform mechanical, durability, and safety work (automatic state checkpoints, touched-file recording, missing-receipt warnings, and fail-closed safety blocking on hard stops). Lifecycle transitions (starting runs, routing, task dispatch, signoff approval, run close) remain explicit operations executed via `office` commands.
- **Resume-Never-Start Rule**: Hooks never automatically start an Office run. An unbound harness session in a repository with an active run reports an advisory notice; it never auto-binds or initializes state.
- **No Mandatory Daemon**: All operations are process-based, direct-execution CLI invocations. State coordination relies on local atomic filesystem operations, lockfiles (`fcntl`), and structured storage. No persistent background daemon is required.
- **MCP Deferred in v1**: MCP integration is deferred per decision D143-1. CLI commands are architected over an importable core registry (`office.api`) to allow MCP adapter generation in a subsequent issue without restructuring.

### 1.3 Non-Goals
- Implementing the CLI binary, hook handlers, or migration scripts is not part of this document's delivery; this document is the specification.
- Userland daemon processes, long-running background pollers, or inter-process sockets.
- Native Windows OS runtime support (POSIX `fcntl` locks and bash helper scripts require macOS, Linux, or WSL).
- Pre-deciding tool sets, authority levels, or token budgets for future MCP adapters.

---

## 2. Command Surface and Run Discovery (D138-*)

### 2.1 Discovery Algorithm
When an `office` command requires a run context, it resolves the target run using the following algorithm:

1. **CLI Arguments**: Inspect `--run <id>` or `--state-dir <path>`. If either flag is provided, resolve directly to that run state directory.
2. **State Directory Environment**: Inspect `OFFICE_STATE_DIR`. If set and non-empty, use this path as the run state directory.
3. **Run ID Environment**: Inspect `OFFICE_RUN_ID`. If set and non-empty, resolve `<repo_runs_dir>/<OFFICE_RUN_ID>`.
4. **Session Binding**: Query the current harness session ID and check `.office/sessions/<harness-session-id>` (written by `office start` or an explicit `office resume`). If the binding file exists, read the bound run ID.
5. **Git Common Directory Resolution**: Execute `git rev-parse --git-common-dir`. For git worktrees, resolve to the primary repository checkout root `.office/runs/`. Outside a git repository, run-scoped commands fail unless `--run` or `--state-dir` is explicitly provided. Global commands (`office list`, `office doctor`, `office install`) execute without a git or run context.
6. **Sole Active Run Scan**: In `<repo_runs_dir>`, scan all run pointers. A run pointer is active unless its phase is `closed` or `abandoned`. If exactly one active run exists, select it as the target run.
7. **Ambiguity or Absence**: If multiple active runs exist without an explicit binding or flag, or if zero active runs exist, fail immediately with exit code 3. Never select runs using latest modification time (`ls -t | head -1`).

### 2.2 Precedence List (D138-2)
Resolution precedence is strictly ordered:
1. `--run <id>` / `--state-dir <path>` CLI flags
2. `OFFICE_STATE_DIR` environment variable
3. `OFFICE_RUN_ID` environment variable
4. Session binding file `.office/sessions/<harness-session-id>`
5. Sole active run pointer in repository `.office/runs/`
6. Ambiguity error (Exit Code 3)

### 2.3 Ambiguity Output Example (D138-3)
When multiple active runs exist in a repository and no flag, environment variable, or session binding disambiguates them, the CLI exits with code 3 and outputs candidate runs (short ID, 60-character goal, phase, age):

```
$ office status
Error: Multiple active runs found in repository. Specify --run <id> or bind session with `office resume <id>`.
Active runs:
  7178051e  [phase: executing]  Author office-cli architecture spec (age: 12m)
  a02865b1  [phase: planning]   Review hook portability matrix and harness gap (age: 1h)
```

This replaces legacy `ls -t | head -1` patterns in `office_shortcut.sh` and `session_end.sh`.

### 2.4 Active Run Lifecycle (D138-4)
- A run pointer remains active from creation until its phase transitions to `closed` or `abandoned`.
- `office list` displays all active runs, with historical/closed runs available via `--all`.
- `office gc` safely prunes state directories for runs in `closed` phase after verifying all linked worktrees are detached.

### 2.5 Output Formats and Exit Codes (D138-5)
The CLI supports three output formats:
- **Terse (default)**: Single-line output containing essential decision fields.
- **JSON (`--json`)**: Complete structured JSON payload matching the command schema.
- **Verbose (`--verbose`)**: Human-readable prose output with diagnostics.

| Exit Code | Name | Semantic Meaning |
|---|---|---|
| `0` | OK | Command completed successfully. |
| `1` | Fail | Execution failure, validation error, or unhandled runtime error. |
| `2` | Usage | Invalid CLI arguments, unknown flags, or syntax errors. |
| `3` | Ambiguous / No Run | Multiple active candidate runs without binding, or no active run found. |
| `4` | Gate Refused | Safety gate check blocked, verification failed, or lease lock conflict. |

### 2.6 Semantic Commands (D138-6)
Semantic commands replace direct calls to `office_runtime.py` subcommands:

| Command | Replaced `office_runtime.py` Subcommands | Terse Output Shape |
|---|---|---|
| `office start [goal]` | `start` + issue record creation + kickoff | `<run-id> <phase> <worktree>` |
| `office status` | `status` | ≤10 lines: phase, next receipt, open gates, live dispatches |
| `office spoke <name>`<br>`office spoke --mark <name>` | `spoke` check / mark / hash computation | `<name> ok <sha256:digest>` (computed by CLI; one spoke per call) |
| `office route <role> --task <id>` | `route` candidate evaluation | `<role> -> <candidate> (<score>)` (catalog + config seed + quota probes) |
| `office dispatch <role> --task <id>` | `dispatch` packet generation + pane spawn | `<dispatch-id> <pane-id> <worktree-path>` (validates packet, worktree, pane, ledger) |
| `office approve <gate-id>` | `approve` gate signoff | `<gate-id> approved <receipt-id>` |
| `office close` | `close` run finalization and archival | `<run-id> closed <archive-receipt>` |

**Accepted Spoke Weakening**: `office spoke --mark <name>` accepts a single spoke name per call and computes the file digest internally. It weakens the check by no longer proving the agent located the file manually, but strictly blocks batch-marking across multiple spokes.

### 2.7 Raw Escape Hatch (D138-6)
Existing `office_runtime.py` subcommands remain accessible via `office raw <sub> [args...]`. This ensures backward compatibility for legacy workflows while semantic commands stabilize.

### 2.8 Dispatch Environment Export (D138-7)
When `office dispatch` launches a child executor, it injects `OFFICE_STATE_DIR` and `OFFICE_RUN_ID` directly into the child process environment. Child processes never run run-discovery logic; they operate strictly on their injected state pointers.

---

## 3. Hook Activation and Installation (D139-*)

### 3.1 Installation Responsibilities (D139-1)
- `office install` registers one marker-tagged, idempotent shim entry per supported event into each harness's user-global configuration (e.g., `office hook <event> --harness <h>`).
- Hooks are installed user-globally; no per-repository hook files or wrapper copies are written into project checkouts.
- Timestamped configuration backups (`<config-path>.bak.<timestamp>`) are written before any harness configuration is modified.

### 3.2 Bound vs. Unbound Sessions (D139-2, D141-2)
- **Bound Session**: A harness session whose unique session ID is registered in `.office/sessions/<harness-session-id>`. Bound sessions execute full authority actions (checkpointing, file touch tracking, inbox injection, hard safety gate blocking).
- **Unbound Session**: A harness session without an entry in `.office/sessions/`. In an unbound session, every hook event is a complete no-op (no state modification, no stdout/stderr output), except for `session.start`.
- **Advisory Start Notice**: On `session.start` in an unbound session, if the local git repository contains active Office runs, the hook emits exactly one plain-text line:
  `Active Office run <id> (phase <p>). office resume to bind.`
  Hooks never auto-start or auto-bind a run.

### 3.3 Opt-Out Precedence (D139-3)
Hook execution checks configuration in strict precedence order:
1. `OFFICE_HOOKS=off` environment variable
2. Repository configuration `.office/config.yaml` (`hooks: off` or `hooks: false`)
3. User-global configuration (`~/.office/config.yaml`)
4. Default: Enabled (`on`)

If hooks are opted out at any level, the shim exits `0` immediately without executing further logic.

### 3.4 Fast Path Budget (D139-4)
For unbound sessions, disabled hooks, or non-Office repositories, the hook entrypoint must terminate within **50ms**. The fast path performs:
1. One environment variable lookup (`OFFICE_HOOKS`).
2. One filesystem stat check for session binding (`.office/sessions/<id>`).

The fast path is implemented in a lightweight bootstrap module without importing the full Python runtime, YAML parsers, or JSONSchema validators.

### 3.5 Doctor and Uninstallation (D139-5)
- `office doctor`:
  - Verifies all harness hook registrations point to the active `office` binary path.
  - Validates hook configurations against the schemas of currently installed harness versions.
  - Checks for known defects (Hermes string-shape syntax, Gemini config path divergence, unwired `PostCompact`).
  - Scans and reports errors from `<state_dir>/hook-errors.jsonl`.
- `office uninstall`:
  - Parses harness configuration files and removes only Office marker-tagged entries.
  - Removes package skill symlinks.
  - Creates a timestamped backup before modifying any configuration.
  - Retains run state directories and `runs.db` unless `--purge` is passed.

### 3.6 Legacy Migration (D139-6, D142-6)
- `office install` scans for and removes legacy `install_hooks.sh` entries and local `.office/hooks/` directories (backing them up first). Both are officially deprecated.
- `office_shortcut.sh` is retained for one deprecation release as a forwarding wrapper that executes the global `office` binary if present.
- `office install` warns if repository-local `.office/bin/office` copies exist, but never deletes consuming repository files.

---

## 4. Portable Event Contract (D141-*)

### 4.1 Portable Event Definitions (D141-1)
The portable event surface consists of seven core events and two optional subagent events:
- `session.start(source=startup|resume|compact)`: Fires when an agent session begins, resumes, or recovers post-compaction. Post-compaction is handled via `source=compact`; there is no standalone `compact.post` event.
- `prompt.submit`: Fires when a user prompt or turn is submitted to the harness loop.
- `tool.pre`: Fires immediately before tool execution.
- `tool.post`: Fires immediately after tool execution completes.
- `turn.stop`: Fires when the model completes an execution turn.
- `compact.pre`: Fires prior to context window compaction.
- `session.end`: Fires when an agent session terminates or exits.
- `subagent.start` / `subagent.stop` (Optional): Fires on child agent spawn and termination in harnesses supporting native subagent lifecycle events (Claude Code, Hermes).

### 4.2 Event Mapping, Authority, and Failure Modes
Authority levels are defined as:
- **Observe**: Read execution context, record metrics/touched files.
- **Checkpoint**: Atomically flush memory state to disk.
- **Warn**: Emit non-blocking advisory messages to agent or stderr.
- **Inject**: Provide bounded context into the prompt turn.
- **Block**: Prevent execution via exit code or denial directive.

| Portable Event | Claude Code 2.1.282 | Codex CLI 0.156.1 | Gemini CLI 0.46.0 | agy 1.2.10 | Hermes v0.21.5 | Authority (Bound) | Failure Mode |
|---|---|---|---|---|---|---|---|
| `session.start` | `SessionStart` (`startup\|resume\|compact`) | `SessionStart` (`startup\|resume\|compact`) | `SessionStart` (`source: startup\|resume`) | ❌ none | `on_session_start` (resume does not fire) | Checkpoint + resume capsule; unbound: 1-line notice | Fail-open, 5s timeout |
| `prompt.submit` | `UserPromptSubmit` | `UserPromptSubmit` | `BeforeAgent` | `PreInvocation` (before model) | `pre_llm_call` | Inject unread inbox items only | Fail-open, 5s timeout |
| `tool.pre` | `PreToolUse` | `PreToolUse` (block UNKNOWN) | `BeforeTool` | `PreToolUse` | `pre_tool_call` | **Block only**: hard stops (protected path, lease escape, main checkout mutation) | **Fail-closed** if bound + lease resolved; fail-open otherwise |
| `tool.post` | `PostToolUse` | `PostToolUse` | `AfterTool` | `PostToolUse` | `post_tool_call` | Observe + record touched files | Fail-open, 5s timeout |
| `turn.stop` | `Stop` | `Stop` | `AfterAgent` | `Stop` | `post_llm_call` / `pre_verify` | Checkpoint + warn on missing receipt (never force continuation) | Fail-open, 5s timeout |
| `compact.pre` | `PreCompact` | `PreCompact` | `PreCompress` (advisory only) | ❌ none | ⚠️ gateway-only `session:compress` | Checkpoint state | Fail-open, 5s timeout |
| `session.end` | `SessionEnd` | ⚠️ `session_end.rs` (no live config) | `SessionEnd` | ❌ none | `on_session_end` | Final checkpoint, mark lease `ended`, close finished panes | Best-effort, non-blocking |
| `subagent.start` | `SubagentStart` | ❌ none | ❌ none | ❌ none | `subagent_start` | Record child in ledger | Fail-open |
| `subagent.stop` | `SubagentStop` | ❌ none | ❌ none | ❌ none | `subagent_stop` | Record child in ledger | Fail-open |

### 4.3 Injection Budget (D141-4)
- Maximum payload size: **≤12 lines** and **≤1 KB**.
- Format: Plain text only (no ANSI codes, no markdown tables).
- Tail pointer: Every injection must conclude with:
  `office status --verbose`
- Injections occur only on `prompt.submit` when unread items exist in the run event inbox. No routine or per-turn context injection is permitted.

### 4.4 Run Event Inbox
- The run event inbox (`<state_dir>/inbox.jsonl`) stores asynchronous notifications, receipts, and external signals.
- On `prompt.submit`, the hook inspects `inbox.jsonl`. If unread items exist, it formats them within the 12-line / 1 KB budget, emits them to the harness context, and marks the items as read.
- If the inbox is empty, `prompt.submit` emits nothing.

### 4.5 Fallback Mechanisms per Harness (D141-6)
Explicit `office` commands are the binding contract; hooks only accelerate durability:
- **agy**: Lacks session and compaction events. The `auto-office` skill executes `office status` upon session startup and `office checkpoint` at phase transitions.
- **Codex CLI**: Lacks native subagent events and verified post-compaction hooks. Relies on `office dispatch` ledger updates and manual status checks.
- **Gemini CLI**: Lacks native subagent lifecycle events. Subagents are tracked via explicit `office dispatch` records.
- **Hermes**: Compaction is restricted to gateway mode. CLI-driven sessions rely on explicit `office checkpoint` instructions.
- **herdr**: Operates as a process wrapper without internal hook visibility. Pane state (`herdr agent wait`, `herdr pane report-agent`) tracks dispatch process liveness only; it is never treated as a native hook. Subagent tracking across all harnesses is recorded via `office dispatch` ledger writes.

### 4.6 Known Defects Detected by Office Doctor
`office doctor` must identify and report the following defects discovered during harness audits:
1. **Hermes Configuration Syntax**: Legacy installers wrote bare string hooks (`hooks.on_session_end: "<path>"`) into `~/.hermes/profiles/*/config.yaml`. Hermes requires list-of-dicts syntax (`hooks: pre_tool_call: [{matcher: ..., command: ...}]`).
2. **Gemini Configuration Path**: Legacy installers wrote standalone `~/.gemini/config/hooks.json`. Current Gemini CLI 0.46.0 expects hooks within `settings.json` under the `hooks` key.
3. **Unwired Post-Compaction**: Legacy manifests declared `post_compact` for `compact_advisor.sh`, but no harness installer wired `PostCompact`, leaving it inert.

---

## 5. Packaging, Upgrade, Compatibility (D142-*)

### 5.1 Package Layout Tree (D142-1, D142-2, D142-8)
The project packages as `office-skills` exposing the console script entrypoint `office`:

```
office-skills/
├── pyproject.toml
├── src/
│   └── office/
│       ├── __init__.py
│       ├── cli.py               # CLI entrypoint (console_scripts: office = office.cli:main)
│       ├── api.py               # Single command registry and schema declarations (D143-2)
│       ├── discovery.py         # Git common-dir and run resolution (D138-1..7)
│       ├── runtime/             # Core runtime migrated from scripts/office_runtime.py
│       ├── hooks/               # Fast-path shims and portable event handlers (D139-*, D141-*)
│       ├── helpers/             # Packaged bash helper scripts (D142-8)
│       ├── resources/           # Schemas, config defaults, catalog seeds, protocol docs (D142-2)
│       └── skills/              # Packaged skill definitions symlinked to harnesses (D142-3)
└── scripts/                     # Deprecation shims (retained for one release)
    └── office_runtime.py        # Thin wrapper: from office.runtime import main; main()
```

Runtime resources (schemas, catalog seeds, protocol documentation) and skills ship as package data accessed via `importlib.resources`.

### 5.2 Installation and Upgrade Sequences
- **Canonical Installation (D142-1)**:
  `uv tool install office-skills`
  Installs into an isolated virtual environment with pinned dependencies (`jsonschema`, `PyYAML`). Alternative: `pipx install office-skills`.
- **Developer / Editable Installation**:
  `uv tool install --editable <path/to/office-skills>`
- **Skill Directory Symlinking (D142-3)**:
  `office install` creates symlinks from each harness's global skill directory to the installed package's `skills/`:
  - `~/.claude/skills/office` -> `<package_data>/skills/office`
  - `~/.codex/skills/office` -> `<package_data>/skills/office`
  - `~/.gemini/skills/office` -> `<package_data>/skills/office`
  - `~/.hermes/skills/office` -> `<package_data>/skills/office`
  In editable mode, symlinks point directly to the git checkout.
- **Upgrade Workflow (D142-4)**:
  `office upgrade` executes:
  `uv tool upgrade office-skills && office install && office doctor`
- **Uninstallation (D142-5)**:
  `office uninstall` removes harness hook entries, deletes skill symlinks, and executes `uv tool uninstall office-skills`. Run directories and `runs.db` are preserved unless `--purge` is supplied.

### 5.3 Versioning and Drift Policy (D142-3, D142-4)
- A single unified version string governs the system: `VERSION` = Python package version = `plugin.json` version.
- **Pinned Run Hashes**: Active runs record their creating version and policy hash (`policy_hash`).
- **Drift Detection**: When `office status` runs, it compares the active runtime version with the run's pinned version.
- **Policy Enforcement**: If an approved run encounters a mismatch in `policy_hash` following a package upgrade, the run enters a hard-stop state. Execution resumes only after an explicit adoption command:
  `office amend --adopt-version`

### 5.4 Platform Support (D142-7)
- Supported platforms: **macOS** and **Linux** (including Windows Subsystem for Linux - WSL).
- Native Windows (PowerShell/cmd.exe without WSL) is **unsupported** due to strict dependencies on POSIX `fcntl` file locking and bash helper scripts.

---

## 6. MCP Boundary (D143-*)

### 6.1 Deferred Status (D143-1)
No Model Context Protocol (MCP) server or client adapter is implemented in v1. The `office` CLI contains no `office mcp` subcommand, and `office install` provides no `--mcp` flag.

### 6.2 Single Command Registry Requirement (D143-2)
To allow future MCP adapter generation without architectural rework, all CLI commands must be defined in a unified registry (`office.api`). Each registry entry defines:
- Command name and semantic purpose.
- Input argument types and validation rules.
- JSONSchema for structured outputs.
- Execution handler function.

The CLI layer in `office.cli` is a thin transport adapter over this core registry.

### 6.3 Future Proposal Requirements (D143-3, D142-8)
Any future MCP implementation must be proposed in a distinct issue addressing tool scopes, authority levels, and token overhead. The dependency extra `office-skills[mcp]` is reserved for this purpose.

---

## 7. Harness Compatibility Matrix

The compatibility matrix reflects verified capabilities from live harness probes and official documentation:

| Harness | Version | `session.start` | `prompt.submit` | `tool.pre` | `tool.post` | `turn.stop` | `compact.pre` | `session.end` | `subagent.*` | Fallback Mechanism |
|---|---|---|---|---|---|---|---|---|---|---|
| **Claude Code** | 2.1.282 | ✅ `SessionStart` | ✅ `UserPromptSubmit` | ✅ `PreToolUse` | ✅ `PostToolUse` | ✅ `Stop` | ✅ `PreCompact` | ✅ `SessionEnd` | ✅ `SubagentStart`<br>`SubagentStop` | Native support across all events. |
| **Codex CLI** | 0.156.1 | ✅ `SessionStart` | ✅ `UserPromptSubmit` | ⚠️ `PreToolUse` (block UNKNOWN) | ✅ `PostToolUse` | ✅ `Stop` | ✅ `PreCompact` | ⚠️ `session_end.rs` (no live config) | ❌ none | Explicit `office dispatch` ledger writes; status checks. |
| **Gemini CLI** | 0.46.0 | ✅ `SessionStart` | ✅ `BeforeAgent` | ✅ `BeforeTool` | ✅ `AfterTool` | ✅ `AfterAgent` | ✅ `PreCompress` (advisory) | ✅ `SessionEnd` | ❌ none | Explicit `office dispatch` ledger writes; status checks. |
| **agy** | 1.2.10 | ❌ none | ⚠️ `PreInvocation` (before model) | ✅ `PreToolUse` | ✅ `PostToolUse` | ✅ `Stop` | ❌ none | ❌ none | ❌ none | Skill invokes `office status` on start and `office checkpoint` at phases. |
| **Hermes** | v0.21.5 | ✅ `on_session_start` (no resume) | ✅ `pre_llm_call` | ✅ `pre_tool_call` | ✅ `post_tool_call` | ✅ `post_llm_call`<br>`pre_verify` | ⚠️ gateway-only `session:compress` | ✅ `on_session_end` | ✅ `subagent_start`<br>`subagent_stop` | Explicit `office checkpoint` instructions in skill. |
| **herdr** | 0.9.1 | ❌ none | ❌ none | ❌ none | ❌ none | ❌ none | ❌ none | ❌ none | ❌ none | Heuristic pane monitoring only; dispatch liveness via ledger. |

---

## 8. Open Unknowns Carried from #137

The following six unknowns were identified in research #137 and are carried forward with their holding defaults:

### 1. Codex Tool Blocking Mechanism
- **Research Finding**: Whether Codex CLI's `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreCompact`, and `Stop` hooks can block execution, and whether blocking requires an exit code (e.g., code 2) or a structured JSON response (`decision: "deny"`), is uncorroborated by source modules.
- **Affected Decisions**: D141-3, D141-5.
- **Holding Default**: Assume fail-open advisory status for Codex hooks, except where standard non-zero exit codes block execution. Hard security stops rely on explicit pre-command verification in `office`.

### 2. Codex Live Event Coverage
- **Research Finding**: Whether `SubagentStart`, `SubagentStop`, `PostCompact`, and `SessionEnd` exist as live, firing events in Codex CLI 0.156.1 remains unverified (not present in live configuration or `codex-rs/hooks/src/events/`).
- **Affected Decisions**: D141-1, D141-3.
- **Holding Default**: Do not register hooks for these events on Codex. Rely on `office dispatch` ledger tracking and explicit session management.

### 3. Gemini CLI Hook Configuration Path
- **Research Finding**: Whether Gemini CLI 0.46.0 reads standalone `~/.gemini/config/hooks.json` or strictly requires hook definitions inside `~/.gemini/settings.json` (as described in official docs) remains unverified without mutating user files.
- **Affected Decisions**: D139-1, D139-5.
- **Holding Default**: `office install` targets the documented `settings.json` format; `office doctor` flags standalone `hooks.json` as a potential legacy defect.

### 4. agy Subprocess Hierarchy
- **Research Finding**: Whether agy wraps an underlying `gemini` binary (which would allow inheriting Gemini's native hooks) or implements an independent model execution loop is unconfirmed.
- **Affected Decisions**: D141-6, D139-1.
- **Holding Default**: Treat agy as an independent harness exposing only its five documented hooks. All missing lifecycle boundaries fall back to explicit skill commands.

### 5. Hermes Profile Configuration Schema
- **Research Finding**: Legacy installers wrote bare string hooks into `~/.hermes/profiles/*/config.yaml`, whereas Hermes documentation specifies list-of-dicts syntax. The runtime handling of bare strings (ignored vs. error) is unverified.
- **Affected Decisions**: D139-5, D139-6.
- **Holding Default**: Treat bare-string configuration as invalid. `office doctor` reports it as an error; `office install` replaces it with list-of-dicts entries.

### 6. Subagent Process Environment Variable Propagation
- **Research Finding**: Whether environment variables exported prior to harness startup propagate into child subagent processes across all harnesses is documented only for Hermes (`HERMES_HOME`). Claude Code, Codex, Gemini, and agy are UNKNOWN.
- **Affected Decisions**: D138-7, D141-3.
- **Holding Default**: Do not rely on harness ambient environment forwarding. Child agents launched via `office dispatch` have `OFFICE_STATE_DIR` and `OFFICE_RUN_ID` explicitly injected into their process invocation.

---

## 9. Decision Index

Every decision code from `decisions-all.md` appears exactly once in this index:

| Decision Code | Summary | Specification Section Anchor |
|---|---|---|
| **D138-1** | Git common-dir repository discovery and worktree resolution | [§2.1 Discovery Algorithm](#21-discovery-algorithm) |
| **D138-2** | Run context resolution precedence order | [§2.2 Precedence List (D138-2)](#22-precedence-list-d138-2) |
| **D138-3** | Ambiguity exit code 3 and candidate list formatting | [§2.3 Ambiguity Output Example (D138-3)](#23-ambiguity-output-example-d138-3) |
| **D138-4** | Active run pointer lifecycle and garbage collection | [§2.4 Active Run Lifecycle (D138-4)](#24-active-run-lifecycle-d138-4) |
| **D138-5** | Terse, JSON, verbose output formats and exit-code table | [§2.5 Output Formats and Exit Codes (D138-5)](#25-output-formats-and-exit-codes-d138-5) |
| **D138-6** | High-level semantic command surface and raw escape hatch | [§2.6 Semantic Commands (D138-6)](#26-semantic-commands-d138-6) |
| **D138-7** | Child dispatch environment variable export | [§2.8 Dispatch Environment Export (D138-7)](#28-dispatch-environment-export-d138-7) |
| **D139-1** | User-global idempotent hook installation | [§3.1 Installation Responsibilities (D139-1)](#31-installation-responsibilities-d139-1) |
| **D139-2** | Bound vs unbound sessions and resume-never-start rule | [§3.2 Bound vs. Unbound Sessions (D139-2, D141-2)](#32-bound-vs-unbound-sessions-d139-2-d141-2) |
| **D139-3** | Opt-out configuration precedence hierarchy | [§3.3 Opt-Out Precedence (D139-3)](#33-opt-out-precedence-d139-3) |
| **D139-4** | Fast path latency budget (<50ms) for unbound sessions | [§3.4 Fast Path Budget (D139-4)](#34-fast-path-budget-d139-4) |
| **D139-5** | `office doctor` verification and `office uninstall` behavior | [§3.5 Doctor and Uninstallation (D139-5)](#35-doctor-and-uninstallation-d139-5) |
| **D139-6** | Removal and migration of legacy hooks | [§3.6 Legacy Migration (D139-6, D142-6)](#36-legacy-migration-d139-6-d142-6) |
| **D141-1** | Portable hook event definitions and post-compact handling | [§4.1 Portable Event Definitions (D141-1)](#41-portable-event-definitions-d141-1) |
| **D141-2** | Unbound session no-op behavior and single start notice | [§3.2 Bound vs. Unbound Sessions (D139-2, D141-2)](#32-bound-vs-unbound-sessions-d139-2-d141-2) |
| **D141-3** | Bound session hook authority boundaries | [§4.2 Event Mapping, Authority, and Failure Modes](#42-event-mapping-authority-and-failure-modes) |
| **D141-4** | Context injection budget (≤12 lines, ≤1 KB) and pointer | [§4.3 Injection Budget (D141-4)](#43-injection-budget-d141-4) |
| **D141-5** | Fail-open default, tool.pre fail-closed, and 5s timeout | [§4.2 Event Mapping, Authority, and Failure Modes](#42-event-mapping-authority-and-failure-modes) |
| **D141-6** | Explicit CLI contract and per-harness fallbacks | [§4.5 Fallback Mechanisms per Harness (D141-6)](#45-fallback-mechanisms-per-harness-d141-6) |
| **D142-1** | Package layout and `uv tool install` distribution | [§5.1 Package Layout Tree (D142-1, D142-2, D142-8)](#51-package-layout-tree-d142-1-d142-2-d142-8) |
| **D142-2** | Modular runtime layout and package data resources | [§5.1 Package Layout Tree (D142-1, D142-2, D142-8)](#51-package-layout-tree-d142-1-d142-2-d142-8) |
| **D142-3** | Single version identifier and harness skill symlinks | [§5.2 Installation and Upgrade Sequences](#52-installation-and-upgrade-sequences) |
| **D142-4** | Upgrade sequence, hash pinning, and version drift handling | [§5.3 Versioning and Drift Policy (D142-3, D142-4)](#53-versioning-and-drift-policy-d142-3-d142-4) |
| **D142-5** | Uninstallation sequence and state persistence | [§5.2 Installation and Upgrade Sequences](#52-installation-and-upgrade-sequences) |
| **D142-6** | `office_shortcut.sh` deprecation and binary coexistence | [§3.6 Legacy Migration (D139-6, D142-6)](#36-legacy-migration-d139-6-d142-6) |
| **D142-7** | Supported operating systems and Windows limitation | [§5.4 Platform Support (D142-7)](#54-platform-support-d142-7) |
| **D142-8** | Native core hooks, bash helpers, and reserved MCP extra | [§5.1 Package Layout Tree (D142-1, D142-2, D142-8)](#51-package-layout-tree-d142-1-d142-2-d142-8) |
| **D143-1** | Deferral of MCP server and client implementation in v1 | [§6.1 Deferred Status (D143-1)](#61-deferred-status-d143-1) |
| **D143-2** | Unified command registry requirement for future MCP | [§6.2 Single Command Registry Requirement (D143-2)](#62-single-command-registry-requirement-d143-2) |
| **D143-3** | Scoping requirement for future MCP proposals | [§6.3 Future Proposal Requirements (D143-3, D142-8)](#63-future-proposal-requirements-d143-3-d142-8) |
