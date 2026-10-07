"""office doctor: verify the installation and name every known defect."""
from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

from office import adapters, config_repairs, db, frontdoor, install, legacy, paths, read_scope, runtime_default, state, version
from office.result import Result


def doctor(fix: bool = False, probe_vision: bool = False) -> Result:
    res = Result()
    problems = 0
    ver = version.current()
    res.add(f"office {ver} (exact identity: {'yes' if version.is_exact(ver) else 'NO'})")
    drift = version.install_drift()
    if drift is not None:
        at = f"{drift['source']} @ {drift['head']}" if drift["head"] else drift["source"]
        if drift["differ"]:
            problems += 1
            shown = ", ".join(drift["differ"][:3]) + (f" (+{len(drift['differ']) - 3} more)" if len(drift["differ"]) > 3 else "")
            res.add(f"install: STALE — {len(drift['differ'])} runtime file(s) differ from {at}: {shown}. "
                    f"The version string cannot show this. Reinstall: uv tool install --force --reinstall {drift['source']} "
                    "&& office install (runs pinned to this version pick up the new code)")
        else:
            res.add(f"install: matches its source {at}")
    if fix:
        res.lines.extend(install.install().lines)
    reg = frontdoor.registered(ver)
    if not reg:
        problems += 1
        res.add("runtime: this version is not registered for pinned runs (office install)")
    con = db.connect()
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        schema = con.execute("SELECT value FROM schema_meta WHERE key='office_schema'").fetchone()
        res.add(f"runs.db: {paths.runs_db()} ({mode}, schema {schema[0] if schema else '?'})")
        missing = db.missing_columns(con)
        if missing:
            problems += 1
            res.add("schema: MISSING COLUMNS " + ", ".join(missing))
        else:
            res.add("schema: all columns present")
        rows = con.execute("SELECT id, office_version, phase FROM runs WHERE office_version IS NOT NULL "
                           "AND phase NOT IN ('closed','abandoned')").fetchall()
        pinned = {}
        for r in rows:
            pinned.setdefault(version.release_line(r["office_version"]), []).append(r["id"][:8])
        from office import config as cfg
        for r in rows:
            drift = cfg.config_drift(state.get_run(con, r["id"]))
            if drift:
                res.add(drift)
        newest = frontdoor.installed_lines()[0]
        for line, ids in sorted(pinned.items(), key=lambda kv: version.release_key(kv[0])):
            ok = version.same_line(line, ver) or frontdoor.newest_on_line(line)
            if not ok:
                problems += 1
            res.add(f"on {line}: {len(ids)} active run(s) {'ok' if ok else 'RUNTIME MISSING — install and register a ' + line + '.x runtime'}")
            if version.release_key(newest) > version.release_key(line):
                res.add(f"  {newest} is installed; upgrade with: " + "; ".join(f"office upgrade {i}" for i in ids))
        compat = con.execute("SELECT command, COUNT(*) AS n FROM compat_calls GROUP BY command ORDER BY n DESC LIMIT 5").fetchall()
        if compat:
            res.add("compat (removed in 3.2.0): " + ", ".join(f"{c['command']}×{c['n']}" for c in compat))
    finally:
        con.close()
    retained = legacy.retained_runtime(runtime_default.LEGACY_V3_FINAL, materialize=fix)
    res.add(f"new runs: Auto Office {runtime_default.new_runs_setting()} | retained 3.0 runtime: "
            + ("ok" if retained else "not materialized (office doctor --fix)"))
    ident = paths.repo_identity()
    if ident:
        from office import config as cfg
        top = ident[0]
        try:
            effective, _ = cfg.resolve(top)
        except ValueError as exc:
            res.add(f"config: invalid: {exc}")
            effective = {}
        setup = str((effective.get("worktree") or {}).get("setup") or "").strip()
        if (top / "pnpm-lock.yaml").is_file() and not setup:
            res.add("worktree setup: pnpm-lock.yaml found but worktree.setup is not set; new worktrees start without "
                    "node_modules. Add to .auto-office/config.yaml: worktree: {setup: \"pnpm install --offline "
                    "--frozen-lockfile || pnpm install --frozen-lockfile\"}"
                    + ("" if shutil.which("pnpm") else "; pnpm is not on PATH, install it first (e.g. "
                       "corepack enable pnpm, or brew install pnpm)"))
        missing = setup_tools_missing(setup)
        if missing:
            problems += 1
            res.add(f"worktree setup: `{setup}` runs under /bin/sh, but {', '.join(missing)} "
                    f"{'is' if len(missing) == 1 else 'are'} not on PATH; every new worktree's setup would exit 127 "
                    "(command not found). Install it or fix worktree.setup")
        for leg in legacy.legacy_runs(paths.primary_checkout(ident[1])):
            if leg.active:
                have = legacy.retained_runtime(leg.plugin_commit, materialize=fix)
                if not have:
                    problems += 1
                res.add(f"legacy run {leg.run_id[:8]} ({leg.phase}) pinned to {leg.plugin_commit[:12]}: "
                        + ("runtime ok" if have else "RUNTIME MISSING"))
    for harness, path in install.CONFIG.items():
        path = path.expanduser()
        # Claude settings are checked even when the file is absent (the read rules are then
        # missing), but not when Claude itself is absent: office install skips it then too.
        if not path.exists() and (harness != "claude" or not path.parent.exists()):
            continue
        try:
            # An absent claude settings file is empty settings: the rules are missing.
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            problems += 1
            res.add(f"{harness}: {path.name} is not valid JSON")
            continue
        if harness == "claude":
            lacking = read_scope.missing(data)
            if lacking:
                problems += 1
            total = len(read_scope.allow_rules())
            res.add(f"claude: reviewer read rules {total - len(lacking)}/{total} in permissions.allow"
                    + (" (office install; reviewers' context-mode cannot read Office state or global guidance without them)"
                       if lacking else " ok"))
            if not path.exists():
                continue
        managed = [h.get("command") for entries in (data.get("hooks") or {}).values() for e in entries
                   for h in (e.get("hooks") or []) if install.MARKER in (h.get("command") or "")]
        if not managed:
            res.add(f"{harness}: office hooks not installed (office install)")
            continue
        exe = managed[0].split(" hook ")[0]
        good = Path(exe).exists() or bool(shutil.which(exe))
        if not good:
            problems += 1
        res.add(f"{harness}: {len(managed)} office hook(s) -> {'ok' if good else 'binary missing: ' + exe}")
    for check in (config_repairs.gemini_legacy, config_repairs.hermes_hooks):
        lines, count = check(fix)
        res.lines.extend(lines)
        problems += count
    res.add("known gap: Codex tool.pre denial is unverified; Codex hooks stay warn-only and are not installed")
    res.lines.extend(codex_hook_warnings(ident[0] if ident else None))
    res.add("known gap: compact_advisor.sh was never wired to PostCompact; 3.1 keeps state durable in runs.db instead")
    all_adapters = adapters.load_all()
    for aid, a in sorted(all_adapters.items()):
        if a.get("office_profiles"):
            res.add(f"adapter {aid}: " + (f"installed {adapters.harness_version(a)}" if adapters.installed(a) else "not installed"))
    try:
        import playwright  # noqa: F401
        res.add("visual capture: playwright available")
    except ImportError:
        res.add("visual capture: playwright missing (uv tool install 'auto-office[visual]'); visual gates report CAPTURE_BLOCKED")
    con = db.connect()
    try:
        proofs = con.execute("SELECT harness, model, effort, result, proved_at FROM capability_proofs WHERE capability='vision' "
                             "ORDER BY proved_at DESC LIMIT 6").fetchall()
        if proofs:
            for p in proofs:
                res.add(f"vision proof {p['harness']}/{p['model']}@{p['effort']}: {p['result']} ({p['proved_at'][:10]})")
        else:
            res.add("vision proofs: none yet (the first visual gate probes; or office doctor --probe-vision)")
        if probe_vision:
            res.lines.extend(_probe_all(con))
    finally:
        con.close()
    errors = paths.state_home() / "hook-errors.jsonl"
    if errors.exists():
        n = sum(1 for _ in errors.open())
        res.add(f"hook errors logged: {n} ({errors})")
    res.data = {"problems": problems}
    res.next = "office doctor --fix" if problems and not fix else None
    res.exit_code = 1 if problems else 0
    return res


def codex_hook_warnings(cwd: Path | None = None) -> list[str]:
    """Conservative preflight: hook hashes belong to Codex, not Office.

    Presence of trust entries does not prove the current handlers are trusted:
    moving a handler changes its positional key. Never approve or rewrite them.
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    cfg_path = home / "config.toml"
    try:
        cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    except (OSError, ValueError):
        return [f"codex: cannot read hook configuration in {cfg_path}; check Codex startup manually"]
    if (cfg.get("features") or {}).get("hooks") is False:
        return []
    files = [home / "hooks.json"]
    if cwd:
        files.append(cwd / ".codex" / "hooks.json")
    # Enabled plugins can contribute hooks even with no user hooks.json.
    plugins = cfg.get("plugins") or {}
    for key, settings in plugins.items():
        if not isinstance(settings, dict) or settings.get("enabled") is not True:
            continue
        name, sep, market = key.partition("@")
        if sep and all(part and part not in (".", "..") and "/" not in part for part in (name, market)):
            files.extend((home / "plugins" / "cache" / market / name).glob("*/hooks/hooks.json"))
    configured = []
    for path in files:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            configured.append(f"{path} (unreadable)")
            continue
        if isinstance(data, dict) and data.get("hooks"):
            configured.append(str(path))
    if not configured:
        return []
    return ["codex: hook trust UNVERIFIED for " + ", ".join(configured) +
            "; new, changed or reordered hooks may block Herdr startup at 'Hooks need review'. "
            "Office cannot validate Codex's trusted_hash values. Open Codex in the dispatch directory and "
            "review or skip pending hooks before launching; office doctor --fix does not trust hooks"]


def _probe_all(con) -> list[str]:
    from office import candidates, conformance, routing, util
    from office.config import resolve
    config, _ = resolve(None)
    run_id = "doctor-" + util.new_run_id()
    sdir = paths.run_dir(run_id)
    sdir.mkdir(parents=True, exist_ok=True)
    now = util.now_iso()
    with db.transaction(con):
        con.execute("INSERT INTO runs(id, family_id, created_at, status, office_version, goal, phase, state_dir, policy_json, "
                    "gates_json, requirements_version, plan_version, routing_version, terminal_at, terminal_reason, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, run_id, now, "closed", version.current(), "office doctor --probe-vision", "closed", str(sdir),
                     util.dumps(config), "{}", 1, 0, 1, now, "maintenance probe", now))
    run = state.get_run(con, run_id)
    out = []
    all_adapters = adapters.load_all()
    cands, _ = candidates.build_candidates(con, "visual_reviewer", probe=False,
                                           family_floors=config.get("model_family_floors"))
    # Preferred routes first; a route with a current proof is not re-probed.
    seed = candidates.role_policy(config, "visual_reviewer").get("preferred_seed")
    rank = {id(c): routing.preferred_rank(c, seed) for c in cands}
    cands.sort(key=lambda c: rank[id(c)] if rank[id(c)] is not None else len(seed or []))
    for cand in cands[:6]:
        adapter = all_adapters[cand["adapter_id"]]
        cached = conformance.proof_status(con, cand, adapter, "vision")
        if cached:
            out.append(f"probe {routing.candidate_id(cand)}: {cached} (cached proof)")
            continue
        r = conformance.probe_vision(con, run, cand, adapter)
        out.append(f"probe {r['triple']}: {r['result']} ({r['detail']})")
    return out or ["no visual-capable routes installed"]


_SH_BUILTINS = {"cd", "export", "set", "unset", "test", "[", "true", "false", "echo", "printf", ":", ".", "source",
                "command", "exec", "eval", "if", "then", "else", "fi", "for", "do", "done", "while", "case", "esac",
                "exit", "return", "umask", "type", "hash", "env", "{", "}", "(", ")"}


def setup_tools_missing(setup: str) -> list[str]:
    """The programs a `worktree.setup` command starts that are not on PATH, where
    no `||` alternative covers them (#479: `pnpm install` with no pnpm exits 127
    in every new worktree, and the dispatch continues without dependencies)."""
    import re
    import shlex
    missing: list[str] = []
    for step in re.split(r"&&|;|\n", setup or ""):
        tools = []
        for alt in step.split("||"):
            try:
                words = shlex.split(alt.split("|")[0])
            except ValueError:
                return []
            words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
            tools.append(words[0] if words else "")
        if any(not t or t in _SH_BUILTINS or "/" in t or "$" in t or shutil.which(t) for t in tools):
            continue
        missing += [t for t in dict.fromkeys(tools) if t not in missing]
    return missing
