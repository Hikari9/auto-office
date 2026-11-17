"""What a reviewer may read outside its checkout: Office state and global guidance.

One definition feeds two consumers:
- `add_dirs()` is appended to reviewer and vision launches for harnesses that
  need a flag to read a directory (claude and agy `--add-dir`): the run's own
  state dir and the guidance dirs (#1239).
- `allow_rules()` are the `permissions.allow` entries `office install` writes to
  ~/.claude/settings.json. context-mode's ctx_execute_file refuses paths outside
  the project root unless a `Read(<glob>)` allow rule covers them, and no launch
  flag reaches it.

Rules are written in both absolute spellings because the two readers differ:
Claude Code takes `//abs` as absolute (`/abs` is project-relative), while
context-mode matches the glob literally against the absolute path.

Rules Office adds are recorded in a ledger so uninstall and re-install remove
exactly those, never a rule the user wrote.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from office import paths

# Relative to the home directory. Directories grant `<dir>/**`, files grant the
# path or glob as written. Credentials (~/.claude/.credentials.json, ~/.codex/auth.json)
# are deliberately outside these.
GUIDANCE_DIRS = (".claude/rules", ".claude/skills")
GUIDANCE_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", ".claude/*.md", ".codex/AGENTS.md")

# Never writable by a reviewer: harness config and global guidance.
READONLY_DIRS = (".claude", ".codex", ".agents")
READONLY_FILES = ("AGENTS.md", "CLAUDE.md")

# Profile kinds that read the operator's checkout and state but never change them.
READER_KINDS = ("reviewer", "vision")


def state_dirs() -> list[Path]:
    return [paths.state_home(), paths.data_home()]


def guidance_dirs() -> list[Path]:
    home = Path.home()
    return [home / d for d in GUIDANCE_DIRS]


def guidance_files() -> list[Path]:
    home = Path.home()
    return [home / f for f in GUIDANCE_FILES]


def run_state_dir(output: Path | None) -> Path | None:
    """The run's own state dir (`<state>/runs/<run>`) a reply file sits under, else None."""
    if not output:
        return None
    try:
        runs = paths.runs_dir().resolve()
        where = Path(output).resolve()
    except OSError:
        return None
    if runs not in where.parents:
        return None
    return runs / where.relative_to(runs).parts[0]


def add_dirs(output: Path | None = None) -> list[Path]:
    """Existing directories a reviewer's harness must be told it may read: its own run's state dir and the
    guidance dirs. Never the whole Office state or data home (#1239): Claude Code asks to trust every added
    directory, and trusting one covers everything below it, so a reviewer is shown only its run."""
    seen: list[Path] = []
    run_dir = run_state_dir(output)
    for d in ([run_dir] if run_dir else []) + guidance_dirs():
        if d.is_dir() and d not in seen:
            seen.append(d)
    return seen


def with_read_dirs(kind: str, include_dirs: list[Path] | None, output: Path | None = None) -> list[Path]:
    """`include_dirs` plus the shared read roots, for reviewer kinds only. A
    worker's argv is never widened."""
    base = list(include_dirs or [])
    if kind not in READER_KINDS:
        return base
    return base + [d for d in add_dirs(output) if d not in base]


def dispatch_dir(output: Path | None) -> Path | None:
    return Path(output).parent if output else None


def write_allows(output: Path | None) -> list[str]:
    """Claude `Edit(<abs>/**)` allow rules for a reviewer: its dispatch dir and
    nothing else. (Claude Code consults only `Edit(path)` rules for file writes;
    a `Write(path)` rule is accepted but never checked.) No reply path means no
    write access at all. Reviewer launches
    run with `--permission-mode dontAsk`, so a write no rule allows is denied
    (including another run's dispatch dir, which a deny rule could not name
    without also covering this one: deny beats allow). The profiles also pass
    `--restricted`, which ignores the user, project and local settings files, so
    an allow rule the operator already has (a broad `Write` or `Edit(<state>/**)`)
    cannot widen this fence."""
    out_dir = dispatch_dir(output)
    return [f"Edit(/{out_dir}/**)"] if out_dir else []


def _spellings(path: Path) -> list[str]:
    """`path` as given and, when it differs, with symlinks resolved (/tmp vs
    /private/tmp): opencode matches the spelling the agent typed."""
    forms = [str(path)]
    try:
        real = str(path.resolve())
    except OSError:
        return forms
    return forms + ([real] if real != forms[0] else [])


def opencode_permissions(cwd: Path, output: Path | None, include_dirs: list[Path] | None = None) -> list[dict]:
    """opencode `permissions` for a reviewer agent; the last matching rule wins.

    Everything is denied except reading (and glob/grep) and ONE write: the exact
    reply file. opencode gates a path outside its working directory twice: first
    `external_directory` on `<dir>/*` (`*` crosses `/`, so it covers subdirectories),
    then the `read` or `edit` action on the absolute path. A path inside the working
    directory is checked as `edit`/`read` on its cwd-relative path only. So the
    checkout, `include_dirs` and the shared read roots get an `external_directory`
    grant (a launch whose PWD is not the checkout would otherwise be denied reading
    it), and the reply file gets `external_directory` on its dispatch dir plus an
    exact-path `edit` allow in each spelling it can be checked under."""
    def rule(action: str, resource: str, effect: str = "allow") -> dict:
        return {"action": action, "resource": resource, "effect": effect}

    rules = [rule("*", "*", "deny")] + [rule(a, "*") for a in ("read", "glob", "grep")]
    rules += [rule("read", "*.env", "deny"), rule("read", "*.env.*", "deny")]
    readable = list(dict.fromkeys(with_read_dirs("reviewer", [Path(cwd), *(include_dirs or [])], output)))
    out_dir = dispatch_dir(output)
    for directory in readable + ([out_dir] if out_dir else []):
        rules += [rule("external_directory", f"{form}/*") for form in _spellings(directory)]
    if output:
        resources = _spellings(Path(output))
        try:
            relative = Path(output).relative_to(cwd)
            resources.append(str(relative))
        except ValueError:
            pass
        rules += [rule("edit", resource) for resource in resources]
    return rules


def opencode_agent_text(cwd: Path, output: Path | None, include_dirs: list[Path] | None = None) -> str:
    """The opencode agent file (markdown with YAML frontmatter) for a reviewer.
    Values are JSON strings, which are valid YAML scalars."""
    lines = ["---", "description: Auto Office reviewer. Reads the checkout and writes its reply file only.",
             "mode: primary", "permissions:"]
    for r in opencode_permissions(cwd, output, include_dirs):
        lines += [f"  - action: {json.dumps(r['action'])}", f"    resource: {json.dumps(r['resource'])}",
                  f"    effect: {r['effect']}"]
    lines += ["---", "",
              "You are an independent reviewer for Auto Office. Follow the review brief you are given exactly. "
              "You can read, glob and grep. The only file you may write is the reply file the brief names; "
              "you cannot edit anything else or run commands. Put your complete review in that file."]
    return "\n".join(lines) + "\n"


def write_opencode_agent(name: str, cwd: Path, output: Path, include_dirs: list[Path] | None = None) -> Path:
    """Write `<dispatch dir>/opencode/agents/<name>.md` and return the config
    directory to point `OPENCODE_CONFIG_DIR` at. It is per dispatch, so the write
    allowance names this dispatch's reply file and no other run's."""
    config_dir = Path(output).parent / "opencode"
    agent = config_dir / "agents" / f"{name}.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(opencode_agent_text(cwd, output, include_dirs), encoding="utf-8")
    return config_dir


def readonly_roots() -> list[str]:
    """Absolute paths and globs a reviewer never writes: global guidance and
    harness config. Directories grant `<dir>/**`."""
    home = Path.home()
    roots = [f"{home / d}/**" for d in READONLY_DIRS] + [str(home / f) for f in READONLY_FILES]
    return roots


def write_denials(cwd: Path, output: Path | None) -> list[str]:
    """Claude `Edit(<abs>)` deny rules (they also block the Write tool) for what
    a reviewer must not change: the checkout it runs in, Office's worktrees and
    data directory, and the read-only guidance roots. A target that contains
    the dispatch dir is skipped, since deny beats allow."""
    out_dir = dispatch_dir(output)
    targets = [str(Path(cwd)), str(paths.data_home()), str(paths.worktrees_dir())]
    rules = [f"Edit(/{t}/**)" for t in targets
             if out_dir is None or not (out_dir == Path(t) or Path(t) in out_dir.parents)]
    rules += [f"Edit(/{r})" for r in readonly_roots()]
    return list(dict.fromkeys(rules))


def claude_sandbox_available() -> bool:
    """Claude Code's OS sandbox runs here: macOS Seatbelt, or Linux/WSL2 with bubblewrap and socat. Where it
    cannot run, a Claude reviewer launched with `failIfUnavailable` would exit at startup, so it keeps the
    submit-only shell instead (#1186)."""
    if sys.platform == "darwin":
        return True
    return sys.platform.startswith("linux") and bool(shutil.which("bwrap")) and bool(shutil.which("socat"))


def _deny_beside(root: Path, keep: Path) -> list[Path]:
    """Every existing entry under `root` except the chain down to `keep` (which `root` contains): the sandbox
    lets nothing under a writable root be re-allowed once denied, so a root that holds the dispatch dir is
    fenced by denying its other children, level by level."""
    denied: list[Path] = []
    here = root
    for part in keep.relative_to(root).parts:
        try:
            children = sorted(here.iterdir())
        except OSError:
            return denied
        denied += [c for c in children if c.name != part]
        here = here / part
    return denied


def sandbox_write_denials(cwd: Path, output: Path | None, include_dirs: list[Path] | None = None) -> list[str]:
    """Absolute paths a sandboxed Claude reviewer's commands may not write. The sandbox lets Bash write its
    cwd, the temp dir and every `--add-dir`; the reviewer may write only its dispatch dir and temp. So the
    checkout it runs in, every include dir, Office's data home (runs.db) and worktrees, and the read-only
    guidance roots are denied. A root holding the dispatch dir (the state home, or cwd for a vision reviewer)
    is denied around it instead; an entry created there after launch is not covered (#1186)."""
    out_dir = dispatch_dir(output)
    home = Path.home()
    roots = [Path(cwd), paths.data_home(), paths.worktrees_dir(), *(include_dirs or [])]
    roots += [home / d for d in READONLY_DIRS] + [home / f for f in READONLY_FILES]
    denied: list[Path] = []
    for root in roots:
        if out_dir is not None and (out_dir == root or root in out_dir.parents):
            denied += _deny_beside(root, out_dir)
        else:
            denied.append(root)
    return list(dict.fromkeys(s for p in denied for s in _spellings(p)))


def claude_sandbox_settings(cwd: Path, output: Path | None, include_dirs: list[Path] | None = None) -> str:
    """The `--settings` JSON for a Claude reviewer's shell (#1186): every Bash call runs in the OS sandbox with
    no network and the write fence above, a sandbox failure never falls back to unsandboxed, and only the exact
    `office review submit` command (which writes runs.db) runs outside it. `--settings` still applies under
    `--restricted`, and a `false` there makes the sandbox admin-required, so repository settings cannot loosen it."""
    from office import review_submit
    sandbox: dict = {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": False,
        "network": {"allowedDomains": []},
        "filesystem": {"denyWrite": sandbox_write_denials(cwd, output, include_dirs)},
    }
    if output:
        sandbox["excludedCommands"] = [review_submit.submit_command(output)]
    return json.dumps({"sandbox": sandbox}, separators=(",", ":"))


def allow_rules() -> list[str]:
    rules: list[str] = []
    targets = [f"{d}/**" for d in state_dirs() + guidance_dirs()] + [str(f) for f in guidance_files()]
    for target in targets:
        for rule in (f"Read({target})", f"Read(/{target})"):
            if rule not in rules:
                rules.append(rule)
    return rules


# ---------------------------------------------------------------- settings file

def ledger_path() -> Path:
    return paths.data_home() / "read-rules.json"


def load_ledger() -> list[str]:
    try:
        data = json.loads(ledger_path().read_text())
    except (OSError, ValueError):
        return []
    rules = data.get("claude") if isinstance(data, dict) else None
    return [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []


def save_ledger(rules: list[str]) -> None:
    path = ledger_path()
    if rules:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claude": rules}, indent=2) + "\n")
    elif path.exists():
        path.unlink()


def _allow_list(data: dict, create: bool) -> list | None:
    perms = data.get("permissions")
    if perms is None and create:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        return None
    allow = perms.get("allow")
    if allow is None and create:
        allow = perms["allow"] = []
    return allow if isinstance(allow, list) else None


def sync(data: dict, owned: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Bring `data`'s permissions.allow to the wanted rules, in place. Returns
    (added, removed, owned): `owned` is the new ledger. A wanted rule the user
    already has stays theirs; an owned rule no longer wanted is removed. Settings
    whose permissions are not a dict/list are left untouched."""
    allow = _allow_list(data, create=True)
    if allow is None:
        return [], [], owned
    want = allow_rules()
    removed = [r for r in owned if r not in want and r in allow]
    allow[:] = [r for r in allow if r not in removed]
    added = [r for r in want if r not in allow]
    allow.extend(added)
    return added, removed, [r for r in want if r in owned or r in added]


def strip(data: dict, owned: list[str]) -> int:
    """Remove the owned rules from `data`, dropping containers Office emptied."""
    allow = _allow_list(data, create=False)
    if allow is None:
        return 0
    kept = [r for r in allow if r not in owned]
    removed = len(allow) - len(kept)
    if removed:
        allow[:] = kept
        perms = data["permissions"]
        if not allow:
            del perms["allow"]
        if not perms:
            del data["permissions"]
    return removed


def missing(data: dict) -> list[str]:
    allow = _allow_list(data, create=False) or []
    return [r for r in allow_rules() if r not in allow]
