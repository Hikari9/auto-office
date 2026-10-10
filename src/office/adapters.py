"""Harness adapters: how to launch a routed role on an installed harness.

A seed adapter's `office_profiles` describe three launch forms:

- `worker`: planner/executor. May edit its own worktree and run commands.
- `reviewer`: read-only review whose final message is the verdict.
- `vision`: read-only review with image evidence attached.

A profile existing is not proof the route works. Visual capability counts only
after an image-sensitive conformance probe of the exact harness/model/effort
path passes (office.conformance).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from office import paths, read_scope
from office.util import atomic_write_json, sha256_obj

PROFILE_KINDS = ("worker", "reviewer", "vision")
_VERSION_TTL_SECONDS = 6 * 3600
_RESOLVED_EXE: dict[tuple[str, str], tuple[str | None, float]] = {}
_VERSION_MEMO: dict[tuple, tuple[str | None, float]] = {}


class AdapterError(RuntimeError):
    pass


def adapter_dir() -> Path:
    return paths.resources_root() / "adapters" / "seed"


def user_adapter_dir() -> Path:
    """User-level adapters (`office harness scaffold` writes here): a harness
    registered without a release. OFFICE_USER_ADAPTERS overrides the location."""
    raw = os.environ.get("OFFICE_USER_ADAPTERS")
    return Path(raw).expanduser() if raw else paths.user_config_path().parent / "adapters"


def user_adapter_files() -> list[Path]:
    d = user_adapter_dir()
    return sorted(d.glob("*.yaml")) if d.is_dir() else []


def _read(p: Path) -> dict | None:
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def load_sources() -> dict[str, tuple[dict, str, Path]]:
    """id -> (adapter, origin, path). Origin is `seed`, `user`, or
    `user-override`. A user adapter whose id names a seed adapter is ignored
    unless it sets `override_seed: true`; an unreadable user file is skipped so
    one bad draft never stops every dispatch (`office harness validate` reports it)."""
    out: dict[str, tuple[dict, str, Path]] = {}
    for p in sorted(adapter_dir().glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out[data.get("id", p.stem)] = (data, "seed", p)
    for p in user_adapter_files():
        data = _read(p)
        if data is None:
            continue
        aid = str(data.get("id") or p.stem)
        if aid in out and out[aid][1] == "seed":
            if data.get("override_seed") is not True:
                continue
            out[aid] = (data, "user-override", p)
        else:
            out[aid] = (data, "user", p)
    return out


def load_all() -> dict[str, dict]:
    return {aid: entry[0] for aid, entry in load_sources().items()}


def harness_ids(*, routed: bool = False) -> list[str]:
    """Adapters that can launch an Office role (they declare `office_profiles`),
    sorted. With `routed`, only those the catalog has a row for."""
    ids = sorted(aid for aid, a in load_all().items() if a.get("office_profiles"))
    if not routed:
        return ids
    from office import candidates
    used = {r.get("invocation_harness") for r in candidates.catalog_rows()}
    return [aid for aid in ids if aid in used]


def executables() -> set[str]:
    """Every adapter id and executable name, for recognising a harness process."""
    out = set()
    for aid, a in load_all().items():
        out.add(aid)
        exe = executable(a)
        if exe:
            out.add(os.path.basename(exe))
    return out


def adapter_hash(adapter: dict) -> str:
    return sha256_obj(adapter)


def profile(adapter: dict, kind: str) -> dict | None:
    return (adapter.get("office_profiles") or {}).get(kind)


def session_spec(adapter: dict | None) -> dict:
    """How the adapter's harness yields a session id: `assigned` (Office passes
    one at launch via `assign_arg`), `detected` (read from hook payloads and
    herdr), or `none` (the harness exposes nothing). Undeclared reads as none."""
    spec = (adapter or {}).get("session") or {}
    return {**spec, "id": spec.get("id") or "none"}


def assigns_session(adapter: dict | None) -> bool:
    spec = session_spec(adapter)
    return spec["id"] == "assigned" and bool(spec.get("assign_arg"))


def session_output_pattern(adapter: dict | None) -> "re.Pattern[str] | None":
    """The adapter's `session.output_pattern` (one capture group, matched per
    line) when `output` is among its session sources: how a headless run's
    stream names its session id. A pattern that does not compile reads as none."""
    spec = session_spec(adapter)
    if spec["id"] != "detected" or "output" not in (spec.get("sources") or []) or not spec.get("output_pattern"):
        return None
    try:
        pattern = re.compile(str(spec["output_pattern"]))
    except re.error:
        return None
    return pattern if pattern.groups == 1 else None


def executable(adapter: dict) -> str | None:
    if not isinstance(adapter, dict):
        return None
    return (adapter.get("invocation") or {}).get("executable")


def installed(adapter: dict) -> bool:
    exe = executable(adapter)
    return bool(exe and shutil.which(exe))


def harness_version(adapter: dict) -> str | None:
    """`<harness> --version`, cached briefly so routing does not fork per call."""
    if not isinstance(adapter, dict):
        return None
    exe = executable(adapter)
    if not exe:
        return None
    path_env = os.environ.get("PATH", "")
    now = time.time()

    cached_res = _RESOLVED_EXE.get((exe, path_env))
    if cached_res and now - cached_res[1] < _VERSION_TTL_SECONDS:
        raw_path = cached_res[0]
    else:
        raw_path = shutil.which(exe)
        _RESOLVED_EXE[(exe, path_env)] = (raw_path, now)

    if not raw_path:
        return None

    try:
        resolved_path = Path(raw_path).resolve()
        st = resolved_path.stat()
        mtime = st.st_mtime_ns
    except OSError:
        _RESOLVED_EXE.pop((exe, path_env), None)
        return None

    cmd_list = (adapter.get("version_fingerprint") or {}).get("command") or [exe, "--version"]
    cmd_key = tuple(cmd_list)
    memo_key = (str(resolved_path), mtime, cmd_key)

    cached_entry = _VERSION_MEMO.get(memo_key)
    if cached_entry and now - cached_entry[1] < _VERSION_TTL_SECONDS:
        return cached_entry[0]

    cache_path = paths.data_home() / "harness-versions.json"
    try:
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache = raw_cache if isinstance(raw_cache, dict) else {}
    except (OSError, ValueError):
        cache = {}

    entry = cache.get(exe) if isinstance(cache, dict) else None
    entry_at = entry.get("at") if isinstance(entry, dict) else None
    if (isinstance(entry, dict)
            and isinstance(entry_at, (int, float))
            and now - entry_at < _VERSION_TTL_SECONDS
            and entry.get("mtime_ns") == mtime
            and entry.get("path") == str(resolved_path)
            and entry.get("command") == cmd_list):
        version = entry.get("version")
        _VERSION_MEMO[memo_key] = (version, entry_at)
        return version

    try:
        proc = subprocess.run(cmd_list, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = (out or err).splitlines()
        raw = text[0] if text else ""
    except (OSError, subprocess.SubprocessError):
        _VERSION_MEMO[memo_key] = (None, now)
        return None

    match = re.search(r"\d+\.\d+(\.\d+)?", raw)
    version = match.group(0) if match else (raw or None)
    cache[exe] = {
        "version": version,
        "at": now,
        "mtime_ns": mtime,
        "path": str(resolved_path),
        "command": cmd_list,
    }
    try:
        atomic_write_json(cache_path, cache)
    except OSError:
        pass

    _VERSION_MEMO[memo_key] = (version, now)
    return version


def effort_value(adapter: dict, effort: str) -> str | None:
    mapping = adapter.get("effort_mapping") or {}
    return mapping.get(effort, effort)


PREFLIGHT_TIMEOUT_S = 20


def preflight_checks(adapter: dict, model: str) -> list[tuple[str, list[str]]]:
    """Named (check, argv) preflight commands for one launch of `model`, in
    declaration order, with `{model}` expanded to the pinned invocation slug."""
    spec = (adapter or {}).get("preflight") or {}
    return [(name, [str(a).replace("{model}", str(model)) for a in raw])
            for name, raw in spec.items() if isinstance(raw, (list, tuple)) and raw]


def _model_listed(text: str, slug: str) -> bool:
    """Whether a harness model listing offers `slug`. A `provider/id` slug must
    match a listed provider and model (adjacent columns or the joined form); a
    bare id must be a listed model. Unverifiable is not listed: a launch that
    cannot name the model it will run fails clearly instead of routing silently."""
    listed = [line.split() for line in (text or "").splitlines()]
    if "/" not in slug:
        return any(slug in tokens for tokens in listed)
    provider, _, name = slug.partition("/")
    return any((slug in tokens)
               or (len(tokens) >= 2 and tokens[0] == provider and tokens[1] == name)
               for tokens in listed)


def run_preflight(adapter: dict, model: str) -> tuple[bool, str]:
    """Run the adapter's preflight checks for one launch: (ok, detail). A
    `model_check` additionally requires the pinned slug in the harness's model
    list; every check must exit 0. This is the candidate's provider/model and
    credential validation on the dispatching host — a local probe recorded at
    registration time proves nothing about another host."""
    for name, argv in preflight_checks(adapter, model):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"preflight {name} timed out after {PREFLIGHT_TIMEOUT_S}s ({' '.join(argv)})"
        except (OSError, subprocess.SubprocessError):
            return False, f"preflight {name} could not run ({' '.join(argv)})"
        if proc.returncode != 0:
            detail = next((ln.strip() for ln in reversed((proc.stderr or proc.stdout or "").splitlines()) if ln.strip()), "")
            return False, (f"preflight {name} exited {proc.returncode}: {detail}" if detail
                           else f"preflight {name} exited {proc.returncode} ({' '.join(argv)})")
        if name == "model_check" and not _model_listed(proc.stdout, model):
            return False, (f"preflight model_check: {model} is not in the harness's model list on this host; "
                           "pin a listed provider/model (provider/id) or install it here")
    return True, ""


def toml_path(path: Path) -> str:
    """`path`, resolved, as a TOML basic string. Codex keys `projects` trust on
    the canonical path (macOS `/tmp` is `/private/tmp`), and a JSON string is a
    valid TOML basic string."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return json.dumps(str(resolved))


def build_argv(adapter: dict, kind: str, *, model: str, effort: str, cwd: Path,
               output: Path | None = None, images: list[Path] | None = None,
               include_dirs: list[Path] | None = None, session_id: str | None = None,
               resume_args: list[str] | None = None) -> tuple[list[str], dict]:
    """Return (argv, profile). Placeholders are substituted element-wise; the
    prompt never passes through a shell. `session_id` is appended through the
    adapter's `session.assign_arg` when the harness accepts an assigned id."""
    prof = profile(adapter, kind)
    if not prof:
        raise AdapterError(f"adapter {adapter.get('id')} has no {kind} profile")
    exe = executable(adapter)
    mapped_effort = effort_value(adapter, effort)
    include_dirs = read_scope.with_read_dirs(kind, include_dirs)
    # The one directory a sandboxed reviewer may write: its dispatch dir, else cwd.
    sandbox_dir = Path(output).parent if output else Path(cwd)
    argv = [exe]
    for raw in prof.get("argv") or []:
        arg = str(raw)
        if arg == "{images}":
            for image in images or []:
                for piece in prof.get("image_arg") or []:
                    argv.append(str(piece).replace("{image}", str(image)))
            continue
        if arg == "{include_dirs}":
            for d in include_dirs or []:
                for piece in prof.get("include_arg") or []:
                    argv.append(str(piece).replace("{dir}", str(d)))
            continue
        if "{effort}" in arg and mapped_effort is None:
            # This harness has no knob for this effort: drop the flag and the
            # value that follows it, rather than passing a literal 'None'.
            if argv and argv[-1].startswith("-"):
                argv.pop()
            continue
        if "{output_dir}" in arg and not output:
            # No reply file (a worker): drop the flag that grants its directory.
            if argv and argv[-1].startswith("-"):
                argv.pop()
            continue
        if "{write_allow}" in arg:
            allow = read_scope.write_allows(output) if kind in read_scope.READER_KINDS else []
            arg = arg.replace("{write_allow}", "".join("," + r for r in allow))
        if "{write_deny}" in arg:
            deny = read_scope.write_denials(cwd, output) if kind in read_scope.READER_KINDS else []
            arg = arg.replace("{write_deny}", "".join("," + r for r in deny))
        arg = (arg.replace("{model}", model).replace("{effort}", mapped_effort or "")
               .replace("{last_message}", str(Path(output).with_name("last-message.txt") if output else Path(cwd) / "last-message.txt"))
               .replace("{cwd_toml}", toml_path(cwd))
               .replace("{sandbox_dir_toml}", toml_path(sandbox_dir))
               .replace("{sandbox_dir}", str(sandbox_dir))
               .replace("{cwd}", str(cwd)).replace("{output_dir}", str(Path(output).parent) if output else "")
               .replace("{output}", str(output or "")))
        argv.append(arg)
    if session_id and assigns_session(adapter):
        argv.extend(str(a).replace("{session_id}", session_id) for a in session_spec(adapter)["assign_arg"])
    argv.extend(str(a) for a in resume_args or [])
    return argv, prof


def headless_resume_args(adapter: dict, kind: str, session_id: str) -> list[str] | None:
    """Args appended to the headless argv to continue `session_id`, from the
    profile's `headless_resume_argv`; None when the harness declares no such form."""
    form = (profile(adapter, kind) or {}).get("headless_resume_argv")
    if not form or not session_id:
        return None
    return [str(a).replace("{session_id}", session_id) for a in form]


def resume_argv(adapter: dict, kind: str, *, session_id: str, model: str, effort: str, cwd: Path,
                include_dirs: list[Path] | None = None, output: Path | None = None) -> tuple[list[str], str] | None:
    """(agent args, herdr kind) that reopen `session_id` in a pane, or None when
    the profile declares no `interactive.resume_argv` (the harness cannot resume).
    The resume args follow the profile's interactive args, `{session_id}` filled in."""
    prof = profile(adapter, kind) or {}
    form = (prof.get("interactive") or {}).get("resume_argv")
    if not form or not session_id:
        return None
    base = interactive_argv(adapter, kind, model=model, effort=effort, cwd=cwd, include_dirs=include_dirs, output=output)
    if base is None:
        return None
    args, herdr_kind = base
    return args + [str(a).replace("{session_id}", session_id) for a in form], herdr_kind


def interactive_argv(adapter: dict, kind: str, *, model: str, effort: str, cwd: Path,
                     include_dirs: list[Path] | None = None, output: Path | None = None,
                     session_id: str | None = None) -> tuple[list[str], str] | None:
    """(agent args, herdr kind) for a pane-hosted interactive session, or None
    when the profile has no interactive form. The args follow `herdr agent
    start ... --`, so the executable itself is not included."""
    prof = profile(adapter, kind) or {}
    inter = prof.get("interactive") or {}
    if not prof.get("herdr_kind") or not inter.get("argv"):
        return None
    form = {"argv": inter["argv"], "include_arg": prof.get("include_arg")}
    argv, _ = build_argv({**adapter, "office_profiles": {kind: form}}, kind,
                         model=model, effort=effort, cwd=cwd, include_dirs=include_dirs, output=output,
                         session_id=session_id)
    return argv[1:], prof["herdr_kind"]
