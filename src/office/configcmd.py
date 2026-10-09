"""`office config` (git-config style editing) and `office setup` (its interactive form).

Both edit the user file (~/.config/auto-office/config.yaml) or, with --repo, the
repo file (.auto-office/config.yaml). They write only what the person set, never
the shipped defaults, so a later release can still change a default. Every edit
is resolved and validated before it is written, and a running run keeps the
policy it pinned at start.
"""
from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from office import candidates, db, paths, state
from office import config as cfg
from office.result import Result
from office.state import OfficeError, Refused

SEED_KEY = re.compile(r"^roles\.([^.]+)\.preferred_seed$")
ROUTE_SPEC = re.compile(r"^(?:(?P<harness>[A-Za-z0-9_.-]+)/)?(?P<model>[A-Za-z0-9_.-]+)(?:@(?P<effort>[a-z]+))?$")
# The roles setup asks about, in the order a run uses them.
SETUP_ROLES = (
    ("planner", "Planner (writes the plan)"),
    ("plan_reviewer", "Plan reviewer"),
    ("executor", "Executor (soft preference; floors and quota still apply)"),
    ("code_reviewer", "Code reviewer"),
    ("visual_reviewer", "Visual reviewer"),
)
HARNESSES = ("claude", "codex", "agy")


def _usage(message: str, next_step: str | None = None) -> OfficeError:
    return OfficeError("usage", message, next_step=next_step, exit_code=2)


# ------------------------------------------------------------------ routes

def parse_route(spec: str) -> dict:
    """`[harness/]model[@effort]`, the same form `office dispatch --as` takes."""
    m = ROUTE_SPEC.match(spec.strip())
    if not m:
        raise _usage(f"not a route: {spec!r} (expected [harness/]model[@effort], e.g. claude/sonnet@high)")
    out = {"model_id": m["model"]}
    if m["harness"]:
        out["harness"] = m["harness"]
    if m["effort"]:
        out["effort"] = m["effort"]
    return out


def format_route(entry: dict) -> str:
    return (f"{entry['harness']}/" if entry.get("harness") else "") + str(entry["model_id"]) \
        + (f"@{entry['effort']}" if entry.get("effort") else "")


def parse_seed(text: str) -> list[dict]:
    return [parse_route(part) for part in text.split(",") if part.strip()]


def _simple_seed(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(e, dict) and "model_id" in e
                                           and set(e) <= {"model_id", "harness", "effort"} for e in value)


def validate_seed(entries: list[dict]) -> list[str]:
    """Problems with seed entries against the shipped catalog (empty when valid)."""
    rows = candidates.catalog_rows()
    models = sorted({r["model_id"] for r in rows})
    problems = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("model_id"):
            problems.append(f"{e!r}: each entry needs a model_id")
            continue
        label = format_route(e)
        same_model = [r for r in rows if r["model_id"] == e["model_id"]]
        if not same_model:
            near = difflib.get_close_matches(e["model_id"], models, n=3)
            problems.append(f"{label}: unknown model {e['model_id']!r}" + (f" (did you mean {', '.join(near)}?)" if near else ""))
            continue
        if e.get("harness") and not any(r.get("invocation_harness") == e["harness"] for r in same_model):
            have = sorted({r.get("invocation_harness") for r in same_model if r.get("invocation_harness")})
            problems.append(f"{label}: {e['model_id']} runs on {', '.join(have)}, not {e['harness']}")
            continue
        if not e.get("effort"):
            continue
        # An alias (opus, sonnet) follows the newest concrete model, so any effort that harness offers is valid.
        pool = same_model
        if any(r.get("alias_family") for r in same_model):
            pool = [r for r in rows if r.get("invocation_harness") in {x.get("invocation_harness") for x in same_model}]
        if not any(r.get("effort") == e["effort"] and e.get("harness") in (None, r.get("invocation_harness")) for r in pool):
            have = sorted({r.get("effort") for r in pool if r.get("effort")})
            problems.append(f"{label}: no {e['effort']} effort for {e['model_id']} (known: {', '.join(have)})")
    return problems


# ------------------------------------------------------------------ files

def _tier_path(tier: str, cwd: Path | None) -> Path:
    if tier == "user":
        return paths.user_config_path()
    ident = paths.repo_identity(cwd)
    if ident is None:
        raise _usage("--repo needs a git repository", next_step="cd into the repository, or edit the user file (--user)")
    return cfg.config_paths(ident[0])["repo"]


def _repo_root(cwd: Path | None) -> Path | None:
    ident = paths.repo_identity(cwd)
    return ident[0] if ident else None


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OfficeError("config-unreadable", f"{path} is not valid YAML ({str(exc).splitlines()[0]})",
                          next_step="fix it by hand: office config --edit")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise OfficeError("config-unreadable", f"{path} must be a YAML mapping",
                          next_step="fix it by hand: office config --edit")
    return data


def _write(path: Path, data: dict) -> str | None:
    """Write atomically. YAML round-tripping drops comments, so a file that has
    any is copied to <name>.bak first. Returns the backup path, if one was made."""
    backup = None
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if any(line.lstrip().startswith("#") for line in text.splitlines()):
            backup = f"{path}.bak"
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False) if data else "{}\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _get_path(data: Any, parts: list[str]) -> tuple[bool, Any]:
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


def _set_path(data: dict, parts: list[str], value: Any) -> None:
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _unset_path(data: dict, parts: list[str]) -> bool:
    stack: list[tuple[dict, str]] = []
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return False
        stack.append((node, part))
        node = nxt
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in reversed(stack):  # prune parents the unset emptied
        if parent[part]:
            break
        del parent[part]
    return True


def _leaves(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(data, dict) and data:
        return [leaf for k, v in data.items() for leaf in _leaves(v, f"{prefix}.{k}" if prefix else str(k))]
    return [(prefix, data)]


# ------------------------------------------------------------------ validation

def _check_key(key: str, tier: str, force: bool) -> None:
    parts = key.split(".")
    default = cfg.load_yaml(cfg.default_config_path()) or {}
    if parts[0] in cfg.NON_CONFIGURABLE_KEYS:
        raise _usage(f"{parts[0]} is not configurable")
    if parts[0] not in default:
        near = difflib.get_close_matches(parts[0], [k for k in default if k not in cfg.NON_CONFIGURABLE_KEYS], n=3)
        raise _usage(f"unknown config key {parts[0]!r}" + (f" (did you mean {', '.join(near)}?)" if near else ""),
                     next_step="office config --list --all")
    if tier == "repo" and key == "paths.runs_db":
        raise _usage("paths.runs_db is machine-level; a repo may not move it", next_step="office config --user paths.runs_db <path>")
    if force:
        return
    node: Any = default
    for i, part in enumerate(parts):
        if not isinstance(node, dict):
            break  # below a scalar or list: the value check decides
        if part not in node:
            if i == 2 and parts[0] == "roles" and part in ("preferred_seed", "preferred_seed_by_size"):
                break  # any role may carry a seed, though only some ship one
            near = difflib.get_close_matches(part, list(node), n=3)
            where = ".".join(parts[:i]) or "the top level"
            raise _usage(f"unknown config key {key!r}: {where} has no {part!r}"
                         + (f" (did you mean {', '.join(near)}?)" if near else ""),
                         next_step="office config --list --all, or --force to set it anyway")
        node = node[part]


def _validate(new: dict, tier: str, cwd: Path | None) -> list[str]:
    """Resolve the files as they would be after the edit; return warnings the
    edited tier produced. Raises OfficeError when the result is invalid."""
    root = _repo_root(cwd)
    files = cfg.read_files(root)
    files[tier] = yaml.safe_dump(new, sort_keys=False)
    try:
        effective, warnings = cfg.resolve(root, files=files)
    except ValueError as exc:
        raise _usage(f"that value is invalid: {exc}")
    bad = [w for w in warnings if w.get("tier") == tier and w.get("reason") == "type-mismatch-ignored"]
    if bad:
        w = bad[0]
        raise _usage(f"{w['key']} must be a {w['expected']}, not a {w['got']}")
    policy = effective.get("cost_policy") or {}
    if policy.get("default") not in (policy.get("available") or []):
        raise _usage(f"cost_policy.default must be one of {', '.join(policy.get('available') or [])}")
    # Only what this file sets: a shipped default is not the edit's to reject.
    for role, conf in (new.get("roles") or {}).items():
        problems = validate_seed((conf or {}).get("preferred_seed") or []) if isinstance(conf, dict) else []
        if problems:
            raise _usage(f"roles.{role}.preferred_seed: " + "; ".join(problems),
                         next_step="office setup lists the known routes (type ? at a prompt)")
    return [f"{w['key']}: {w['reason']}" for w in warnings if w.get("tier") == tier]


def _coerce(key: str, raw: str) -> Any:
    if SEED_KEY.match(key) and not raw.lstrip().startswith(("[", "{")):
        return parse_seed(raw)
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _show(key: str, value: Any) -> str:
    if SEED_KEY.match(key) and _simple_seed(value):
        return ",".join(format_route(e) for e in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return yaml.safe_dump(value, default_flow_style=True, width=10**6).strip()
    return str(value)


# ------------------------------------------------------------------ office config

def config(*, key: str | None, value: str | None, tier: str | None, unset: bool = False, list_: bool = False,
           all_: bool = False, edit: bool = False, path: bool = False, origin: bool = False, force: bool = False,
           cwd: Path | None = None) -> Result:
    cwd = cwd or Path.cwd()
    if path:
        tiers = [tier] if tier else ["user", "repo"]
        lines = []
        for t in tiers:
            try:
                p = _tier_path(t, cwd)
            except OfficeError:
                if tier:
                    raise
                continue
            lines.append(f"{t}\t{p}" if len(tiers) > 1 else str(p))
        return Result(lines=lines, data={"paths": lines})
    if edit:
        target = _tier_path(tier or "user", cwd)
        if not (editor := os.environ.get("VISUAL") or os.environ.get("EDITOR")):
            raise _usage("set $EDITOR to use --edit", next_step=f"edit {target} by hand")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*shlex.split(editor), str(target)], check=False)
        _validate(_load(target), tier or "user", cwd)
        return Result(lines=[f"edited {target}"], next="office config --list")
    if list_:
        return _list(tier, all_, origin, cwd)
    if unset:
        if key is None or value is not None:
            raise _usage("usage: office config --unset <key>")
        return _unset(key, tier or "user", cwd)
    if key is None:
        raise _usage("usage: office config <key> [<value>] | --list | --unset <key> | --edit | --path",
                     next_step="office config --list")
    if value is None:
        return _get(key, tier, origin, cwd)
    return _set(key, value, tier or "user", force, cwd)


def _effective_with_origin(key: str, tier: str | None, cwd: Path) -> tuple[bool, Any, str]:
    parts = key.split(".")
    root = _repo_root(cwd)
    if tier:
        ok, v = _get_path(_load(_tier_path(tier, cwd)), parts)
        return ok, v, tier
    files = cfg.read_files(root)
    effective, _ = cfg.resolve(root, files=files)
    ok, v = _get_path(effective, parts)
    where = "default"
    for t in ("repo", "user"):  # the repo file wins over the user file
        if files.get(t) is not None and _get_path(yaml.safe_load(files[t]) or {}, parts)[0]:
            where = t
            break
    return ok, v, where


def _get(key: str, tier: str | None, origin: bool, cwd: Path) -> Result:
    ok, value, where = _effective_with_origin(key, tier, cwd)
    if not ok:
        raise OfficeError("not-set", f"{key} is not set" + (f" in the {tier} file" if tier else ""), exit_code=1,
                          next_step="office config --list --all")
    text = _show(key, value)
    return Result(lines=[f"{where}\t{text}" if origin else text], data={"key": key, "value": value, "origin": where})


def _list(tier: str | None, all_: bool, origin: bool, cwd: Path) -> Result:
    root = _repo_root(cwd)
    rows: list[tuple[str, str, Any]] = []
    if all_ and not tier:
        effective, _ = cfg.resolve(root)
        files = {t: _load_quiet(p) for t, p in cfg.config_paths(root).items()}
        for k, v in _leaves({k: v for k, v in effective.items() if k not in cfg.NON_CONFIGURABLE_KEYS}):
            where = next((t for t in ("repo", "user") if t in files and _get_path(files[t], k.split("."))[0]), "default")
            rows.append((where, k, v))
    else:
        for t in ([tier] if tier else ["user", "repo"]):
            try:
                data = _load(_tier_path(t, cwd))
            except OfficeError:
                if tier:
                    raise
                continue
            rows += [(t, k, v) for k, v in _leaves(data) if k]
    lines = [f"{w}\t{k}={_show(k, v)}" if origin else f"{k}={_show(k, v)}" for w, k, v in rows]
    return Result(lines=lines or ["(nothing set)"], next=None if lines else "office config <key> <value>, or office setup",
                  data={"settings": [{"origin": w, "key": k, "value": v} for w, k, v in rows]})


def _load_quiet(path: Path) -> dict:
    try:
        return _load(path)
    except OfficeError:
        return {}


def _set(key: str, raw: str, tier: str, force: bool, cwd: Path) -> Result:
    _check_key(key, tier, force)
    target = _tier_path(tier, cwd)
    data = _load(target)
    value = _coerce(key, raw)
    _set_path(data, key.split("."), value)
    warnings = _validate(data, tier, cwd)
    backup = _write(target, data)
    res = Result(lines=[f"set {key}={_show(key, value)} ({tier})"], next=_applies_next(),
                 data={"key": key, "value": value, "tier": tier, "path": str(target)})
    res.notices += [f"warning: {w}" for w in warnings]
    if backup:
        res.notices.append(f"note: comments in {target} are not kept; the original is at {backup}")
    return res


def _unset(key: str, tier: str, cwd: Path) -> Result:
    target = _tier_path(tier, cwd)
    data = _load(target)
    if not _unset_path(data, key.split(".")):
        raise OfficeError("not-set", f"{key} is not set in the {tier} file", exit_code=1,
                          next_step="office config --list")
    _validate(data, tier, cwd)
    backup = _write(target, data)
    res = Result(lines=[f"unset {key} ({tier}); the default applies again unless another file sets it"],
                 next=_applies_next(), data={"key": key, "tier": tier, "path": str(target)})
    if backup:
        res.notices.append(f"note: comments in {target} are not kept; the original is at {backup}")
    return res


def _applies_next() -> str:
    return "new runs pick this up; a running run keeps the policy it pinned (office start)"


# ------------------------------------------------------------------ a run's routing (#308)

def apply_run_routing(con, run: dict, quote: str | None) -> Result:
    """Re-pin a run's `roles` and `routing` from the current config files: the one
    opt-in that moves a running run off the values it pinned at start. Later
    dispatches route from the re-pinned policy; the drift notice then has nothing
    left to report for those blocks. `quote` is the user's words authorizing it."""
    if not (quote or "").strip():
        raise _usage('--apply-routing records the user\'s words (--quote "<words>")',
                     next_step=f"office config --run {run['id'][:8]} --apply-routing --quote \"<words>\"")
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}; its routing is no longer used")
    root = Path(run["repo_root"]) if run.get("repo_root") else None
    if root is not None and not root.is_dir():
        raise OfficeError("repo-missing", f"run {run['id'][:8]}'s repository {root} is gone; its repo config cannot be read",
                          next_step="restore the repository, then retry")
    repo = root
    files = cfg.read_files(repo)
    try:
        live, _ = cfg.resolve(repo, files=files)
    except ValueError as exc:
        raise OfficeError("bad-config", f"config is invalid: {exc}", next_step="fix the config files, then retry")
    policy = dict(run.get("policy") or {})
    blocks = ("roles", "routing")
    changed = [b for b in blocks if policy.get(b) != live.get(b)]
    short = run["id"][:8]
    if not changed:
        return Result(lines=[f"run {short} already routes from the current config files; nothing changed"])
    policy.update({b: live.get(b) for b in blocks})
    recorded = policy.get(cfg.FILE_BLOCKS_KEY)
    if recorded is not None:
        # The drift baseline for roles follows the re-pin, so a later edit still reads as drift.
        now = cfg.file_blocks(repo, files)
        for tier in ("user", "repo"):
            base = {k: v for k, v in (recorded.get(tier) or {}).items() if k != "roles"}
            if "roles" in now.get(tier, {}):
                base["roles"] = now[tier]["roles"]
            recorded[tier] = base
    with db.transaction(con):
        state.update_run(con, run["id"], policy=policy)
        state.emit(con, run, "run.routing_applied",
                   f"run {short} re-pinned {' and '.join(changed)} from the config files",
                   payload={"quote": quote.strip(), "changed": changed})
    return Result(lines=[f"run {short} re-pinned {' and '.join(changed)} from the config files (\"{quote.strip()}\")"],
                  next="later dispatches of this run route from the re-pinned policy",
                  data={"run": run["id"], "changed": changed})


# ------------------------------------------------------------------ office setup

def _installed() -> dict[str, bool]:
    return {h: shutil.which(h) is not None for h in HARNESSES}


def known_routes() -> list[str]:
    """One line per harness/model with its efforts, for the `?` prompt."""
    by: dict[tuple[str, str], set[str]] = {}
    for r in candidates.catalog_rows():
        if r.get("invocation_harness") and r.get("effort"):
            by.setdefault((r["invocation_harness"], r["model_id"]), set()).add(r["effort"])
    order = ["none", "low", "medium", "high", "xhigh", "max"]
    return [f"{h}/{m}@{{{','.join(sorted(e, key=lambda x: order.index(x) if x in order else 99))}}}"
            for (h, m), e in sorted(by.items())]


def setup(*, tier: str, yes: bool = False, cwd: Path | None = None,
          input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
          interactive: bool | None = None) -> Result:
    """Walk through the settings people usually change and write the answers."""
    cwd = cwd or Path.cwd()
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        raise _usage("office setup is interactive and needs a terminal",
                     next_step="office config <key> <value> sets one value without prompts")
    target = _tier_path(tier, cwd)
    data = _load(target)
    effective, _ = cfg.resolve(_repo_root(cwd))
    installed = _installed()
    out(f"Auto Office setup: editing the {tier} config ({target})")
    out("Installed harnesses: " + ", ".join(f"{h} {'yes' if ok else 'NO'}" for h, ok in installed.items()))
    out("Enter keeps the current value. `-` resets a role to the shipped default. `?` lists known routes.")
    out("Routes are [harness/]model[@effort], comma-separated, most preferred first.\n")

    changes: list[tuple[str, Any]] = []  # (key, new value; None unsets)

    def ask(prompt: str) -> str:
        try:
            return input_fn(prompt).strip()
        except EOFError:
            raise OfficeError("cancelled", "setup cancelled; nothing was written", exit_code=1)

    for role, label in SETUP_ROLES:
        key = f"roles.{role}.preferred_seed"
        current = (((effective.get("roles") or {}).get(role) or {}).get("preferred_seed")) or []
        mine = _get_path(data, key.split("."))[0]
        out(f"{label}\n  now: {','.join(format_route(e) for e in current) or '(no preference: automatic routing)'}"
            + ("  [set in this file]" if mine else ""))
        while True:
            answer = ask("  routes> ")
            if answer == "?":
                out("\n".join(f"    {r}" for r in known_routes()))
                continue
            if not answer:
                break
            if answer == "-":
                if mine:
                    changes.append((key, None))
                break
            try:
                seed = parse_seed(answer)
                problems = validate_seed(seed)
            except OfficeError as exc:
                seed, problems = [], [exc.message]
            if problems or not seed:
                out("  " + "; ".join(problems or ["name at least one route"]))
                continue
            missing = sorted(h for h in {e.get("harness") for e in seed} - {None} if installed.get(h) is False)
            if missing:
                out(f"  note: {', '.join(missing)} is not installed here; routes on it are skipped until it is")
            changes.append((key, seed))
            break

    policy = effective.get("cost_policy") or {}
    options = policy.get("available") or []
    out(f"\nCost policy ({'/'.join(options)})\n  now: {policy.get('default')}")
    while True:
        answer = ask("  policy> ")
        if not answer:
            break
        if answer in options:
            if answer != policy.get("default"):
                changes.append(("cost_policy.default", answer))
            break
        out(f"  choose one of: {', '.join(options)}")

    if not changes:
        return Result(lines=["no changes"], data={"changed": []})
    out("\nChanges:")
    for key, value in changes:
        out(f"  {key} = {'(reset to default)' if value is None else _show(key, value)}")
    if not yes and ask(f"Write to {target}? [Y/n] ").lower() not in ("", "y", "yes"):
        return Result(lines=["nothing written"], data={"changed": []})

    for key, value in changes:
        if value is None:
            _unset_path(data, key.split("."))
        else:
            _set_path(data, key.split("."), value)
    _validate(data, tier, cwd)
    backup = _write(target, data)
    res = Result(lines=[f"wrote {len(changes)} setting(s) to {target}"], next=_applies_next(),
                 data={"changed": [k for k, _ in changes], "tier": tier, "path": str(target)})
    if backup:
        res.notices.append(f"note: comments in {target} are not kept; the original is at {backup}")
    return res
