"""`office harness` and `office model`: register a harness and its catalog rows
without editing Python (#499).

    office harness scaffold <id> --binary <path>   draft a user-level adapter with TODOs
    office harness validate <id>                   schema plus semantic checks
    office harness smoke <id> --model <m>          one throwaway-repo launch, recorded as launch evidence
    office harness list                            installed, version, sign-in, trust per harness
    office model add <harness>/<slug> --effort E   append rows to the user catalog overlay
    office model list [<harness>]
    office model disable <harness>/<model>[@effort]

Nothing here promotes trust. A harness and every route on it stay
`valid-unverified` until a recorded trust act (`office approve trust`).
`smoke` writes launch evidence only: never trust, never conformance.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from office import adapters, paths, user_catalog
from office.result import Result
from office.state import OfficeError
from office.util import now_iso

ID_RE = re.compile(r"^[a-z0-9-]+$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_PROMPT_TRANSPORTS = ("stdin", "argv", "argv-bound")
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "cmd", "cmd.exe", "powershell", "pwsh"}
# Permission or trust flags that widen what a launched agent may do unprompted.
# Key (the `trust_justifications` key that justifies it) -> matcher over one argv token.
UNSAFE_TRUST_FLAGS: dict[str, re.Pattern[str]] = {
    "bypassPermissions": re.compile(r"bypassPermissions"),
    "--dangerously-skip-permissions": re.compile(r"^--dangerously-skip-permissions\b"),
    "--dangerously-bypass-approvals-and-sandbox": re.compile(r"^--dangerously-bypass-approvals-and-sandbox\b"),
    "--yolo": re.compile(r"^--yolo\b"),
    "yolo": re.compile(r"^yolo$"),
    "--full-auto": re.compile(r"^--full-auto\b"),
    "danger-full-access": re.compile(r"danger-full-access"),
    "--approve": re.compile(r"^--approve\b"),
    "--trust": re.compile(r"^--trust\b"),
    "--allow-all": re.compile(r"^--allow-all"),
    "--skip-permissions": re.compile(r"^--skip-permissions\b"),
    "trust_level=trusted": re.compile(r"trust_level\s*=\s*\\?\"?trusted"),
}
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
SMOKE_PROMPT = ("This is an Auto Office launch smoke test in a throwaway repository. "
                "Reply with the single word READY. Do not edit, create or delete any files.")
SMOKE_TIMEOUT_S = 300


def _usage(message: str, next_step: str | None = None) -> OfficeError:
    return OfficeError("usage", message, next_step=next_step, exit_code=2)


def affected_runs_line() -> str | None:
    """Which active runs now differ from their pinned catalog/adapter hashes."""
    from office import config, db, state
    try:
        con = db.connect()
    except Exception:  # noqa: BLE001 - a missing store means no runs to warn about
        return None
    try:
        now = config.snapshot_hashes()
        rows = con.execute("SELECT id FROM runs WHERE phase NOT IN ('closed','abandoned')").fetchall()
        ids = [r["id"][:8] for r in rows if config.routing_inputs_drift(state.get_run(con, r["id"]), now)]
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()
    if not ids:
        return None
    return (f"active runs whose pinned routing inputs now differ: {', '.join(ids)} "
            "(their later dispatches route from the current files; office status shows the drift)")


def _with_affected(res: Result) -> Result:
    line = affected_runs_line()
    if line:
        res.lines.append(line)
    return res


def _catalog_rows() -> list[dict]:
    from office import candidates
    return candidates.catalog_rows()


def _resolve_adapter(ident: str | None, file: str | None = None) -> tuple[dict, str, Path]:
    if file:
        p = Path(file).expanduser()
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise OfficeError("adapter-unreadable", f"{p}: {e}", exit_code=1)
        if not isinstance(data, dict):
            raise OfficeError("adapter-unreadable", f"{p}: not a YAML mapping", exit_code=1)
        return data, "file", p
    if not ident:
        raise _usage("name the harness", next_step="office harness list")
    sources = adapters.load_sources()
    if ident in sources:
        return sources[ident]
    draft = adapters.user_adapter_dir() / f"{ident}.yaml"
    if draft.is_file():
        raise OfficeError("adapter-unreadable", f"{draft} is not a readable YAML mapping", exit_code=1,
                          next_step=f"fix {draft}, then office harness validate {ident}")
    raise _usage(f"no adapter named {ident!r}", next_step="office harness list, or office harness scaffold "
                 f"{ident} --binary <path>")


# ------------------------------------------------------------------ scaffold

def _probe(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def scaffold(ident: str | None, binary: str | None, *, force: bool = False, override_seed: bool = False) -> Result:
    if not ident or not ID_RE.match(ident):
        raise _usage("a harness id is lowercase letters, digits and dashes", next_step="office harness scaffold <id> --binary <path>")
    exe = binary or ident
    if not (shutil.which(exe) or Path(exe).expanduser().is_file()):
        raise _usage(f"{exe} is not an executable on PATH or a file", next_step=f"office harness scaffold {ident} --binary <path>")
    sources = adapters.load_sources()
    if ident in sources and sources[ident][1] == "seed" and not override_seed:
        raise OfficeError("adapter-exists", f"{ident} is a shipped seed adapter; a user adapter with that id is ignored",
                          next_step=f"pick another id, or office harness scaffold {ident} --binary {exe} --override-seed",
                          exit_code=1)
    target = adapters.user_adapter_dir() / f"{ident}.yaml"
    if target.exists() and not force:
        raise OfficeError("adapter-exists", f"{target} already exists", exit_code=1,
                          next_step=f"office harness validate {ident}, or rerun with --force to redraft")
    version_text = _probe([exe, "--version"]).strip().splitlines()
    help_text = _probe([exe, "--help"])
    flags = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", help_text)))
    risky = [f for f in flags if any(m.search(f) for m in UNSAFE_TRUST_FLAGS.values())]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_draft(ident, exe, version_text[0] if version_text else "unknown", flags, risky, override_seed),
                      encoding="utf-8")
    stub = target.parent / "tests" / f"test_{ident.replace('-', '_')}_adapter.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    if force or not stub.exists():
        stub.write_text(_test_stub(ident), encoding="utf-8")
    lines = [f"drafted {target} from {exe} ({version_text[0] if version_text else 'no --version output'})",
             f"fake-binary test stub: {stub}",
             f"{len(flags)} flags seen in --help" + (f"; permission/trust flags to justify or avoid: {', '.join(risky)}" if risky else ""),
             "trust: valid-unverified (nothing in this flow promotes trust)"]
    return Result(lines=lines, next=f"fill every TODO in {target}, then office harness validate {ident}",
                  data={"adapter": str(target), "test_stub": str(stub), "flags": flags, "risky_flags": risky})


def _wrap_comment(prefix: str, items: list[str], width: int = 100) -> list[str]:
    out, line = [], prefix
    for item in items:
        if len(line) + len(item) + 1 > width:
            out.append(line.rstrip())
            line = "#   "
        line += item + " "
    out.append(line.rstrip())
    return out


def _draft(ident: str, exe: str, version: str, flags: list[str], risky: list[str], override_seed: bool) -> str:
    q = json.dumps
    head = [f"# Drafted by `office harness scaffold {ident}` on {now_iso()[:10]} from {exe} ({version}).",
            f"# Fill every TODO, then run `office harness validate {ident}`. Nothing in this file is trust:",
            "# the harness stays valid-unverified until a recorded trust act.",
            "# Read docs or --help for each flag you use; record what a permission flag grants under",
            "# trust_justifications. A herdr_kind needs an interactive form."]
    if flags:
        head += _wrap_comment("# Flags seen in --help: ", flags)
    if risky:
        head += _wrap_comment("# Permission/trust flags seen (justify or avoid): ", risky)
    body = f"""id: {ident}
verified_state: valid-unverified
{"override_seed: true" + chr(10) if override_seed else ""}version_fingerprint:
  command: [{q(exe)}, --version]
  parser: first-line
model_source:
  type: cli
  command: TODO  # argv that lists models, e.g. [{q(exe)}, models]
# Checked before each headless launch; `{{model}}` is the pinned slug. model_check
# must list the slug; every check must exit 0. Delete what the harness cannot do.
preflight:
  model_check: TODO  # e.g. [{q(exe)}, models]
  auth_check: TODO  # e.g. [{q(exe)}, auth, status]
# Office effort -> the harness's value, for every effort a catalog row uses.
# null drops the effort flag for that level.
effort_mapping: TODO  # e.g. {{low: low, medium: medium, high: high}}
benchmark_slug_mapping: {{}}
invocation:
  executable: {q(exe)}
  argv: TODO  # same as office_profiles.worker.argv
  prompt_transport: TODO  # stdin | argv | argv-bound
  cwd_transport: process-cwd
safe_prompt_passing:
  shell: false
  supports_prompt_file: false
  notes: TODO  # how the prompt reaches the harness without a shell
dispatch_forms: [cli]
trusted_for: [discovery]
quota_probe:
  type: external-primitive
  command: null  # or an argv printing quota JSON; unknown is never unlimited
  unknown_is_unlimited: false
shallow_review:
  supported: false
  minimum_effort: null
agentic_capability:
  default: unknown
failure_signatures: []
conformance:
  deterministic: pending
  live: pending
# Every permission or trust flag in a launch form -> what it grants and why it is needed.
trust_justifications: {{}}
source_notes: TODO  # version probed, date, what each flag was verified to do
session:
  id: none
capabilities: [builder]
office_profiles:
  worker:
    argv: TODO  # e.g. [--print, --model, "{{model}}", --effort, "{{effort}}"]
    prompt: TODO  # stdin | argv | argv-bound
    output: stdout
    # herdr_kind: {ident}  # only together with an interactive form:
    # interactive:
    #   argv: [--model, "{{model}}", --effort, "{{effort}}"]
    max_minutes: 60
"""
    return "\n".join(head) + "\n" + body


def _test_stub(ident: str) -> str:
    return f'''"""Fake-binary checks for the {ident} adapter, drafted by `office harness scaffold`.

Never calls the real harness or a real model: a scripted fake stands in for it.
Move this into your test suite and adjust the fake to answer like {ident}.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ADAPTER = Path(__file__).resolve().parents[1] / "{ident}.yaml"


def _office(env, *args):
    return subprocess.run(["office", *args], capture_output=True, text=True, env=env, timeout=120)


def test_{ident.replace("-", "_")}_adapter_validates_and_smokes_on_a_fake(tmp_path):
    fake = tmp_path / "bin" / "{ident}"
    fake.parent.mkdir()
    fake.write_text("#!" + sys.executable + "\\nimport sys\\nsys.stdin.read() if not sys.stdin.isatty() else None\\nprint('READY')\\n")
    fake.chmod(0o755)
    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    data = yaml.safe_load(ADAPTER.read_text())
    data["invocation"]["executable"] = str(fake)
    (adapters_dir / "{ident}.yaml").write_text(yaml.safe_dump(data))
    env = {{**os.environ, "OFFICE_USER_ADAPTERS": str(adapters_dir), "OFFICE_DATA_HOME": str(tmp_path / "data"),
           "OFFICE_STATE_HOME": str(tmp_path / "state"), "OFFICE_USER_CONFIG": str(tmp_path / "config.yaml"),
           "PATH": str(fake.parent) + os.pathsep + os.environ["PATH"]}}
    assert _office(env, "harness", "validate", "{ident}").returncode == 0
    assert _office(env, "harness", "smoke", "{ident}", "--model", "fake-model").returncode == 0
'''


# ------------------------------------------------------------------ validate

def _walk_todos(node, path: str = "") -> list[str]:
    if isinstance(node, str):
        return [path or "(root)"] if "TODO" in node else []
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _walk_todos(v, f"{path}.{k}" if path else str(k))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _walk_todos(v, f"{path}[{i}]")]
    return []


def _schema_problems(adapter: dict) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return []
    schema = json.loads((paths.resources_root() / "schemas" / "adapter.schema.json").read_text(encoding="utf-8"))
    out = []
    for err in sorted(Draft202012Validator(schema).iter_errors(adapter), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"schema: {where}: {err.message}")
    return out


def _profile_argvs(prof: dict) -> list[tuple[str, list]]:
    inter = prof.get("interactive") or {}
    forms = [("argv", prof.get("argv")), ("headless_resume_argv", prof.get("headless_resume_argv")),
             ("interactive.argv", inter.get("argv")), ("interactive.resume_argv", inter.get("resume_argv"))]
    return [(name, form) for name, form in forms if isinstance(form, list)]


_MISSING = " (harness missing here; resolvability unchecked)"


def packaged_justifications(aid: str | None) -> dict:
    """`adapters/trust_justifications.yaml` entries for a shipped seed adapter, kept
    outside adapters/seed/ so seed adapter hashes (run pins) never change."""
    try:
        data = yaml.safe_load((paths.resources_root() / "adapters" / "trust_justifications.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    entry = data.get(aid) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def _is_seed(adapter: dict) -> bool:
    entry = adapters.load_sources().get(adapter.get("id"))
    return bool(entry and entry[1] == "seed" and entry[0] == adapter)


def _resolvable(argv, exe: str | None) -> str | None:
    """None when `argv` can run here, else why not."""
    if not isinstance(argv, list) or not argv:
        return "not a non-empty argv list"
    head = str(argv[0])
    if not (shutil.which(head) or (os.sep in head and Path(head).expanduser().is_file())):
        if exe and head == exe:
            return f"{head} is not installed on this host" + _MISSING
        return f"{head} is not on PATH"
    for piece in argv[1:]:
        piece = str(piece)
        if piece.startswith("scripts/") and not (paths.resources_root() / piece).is_file():
            return f"{piece} is not a packaged script"
    return None


def check(adapter: dict, rows: list[dict] | None = None) -> list[str]:
    """Every problem that stops `adapter` from being registered; empty means valid."""
    return check_full(adapter, rows)[0]


def check_full(adapter: dict, rows: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """(problems, warnings). A harness binary missing on this host is a warning:
    the commands that call it cannot be resolved here, but are not malformed."""
    warnings: list[str] = []
    problems = list(_schema_problems(adapter))
    aid = adapter.get("id")
    problems += [f"unfilled TODO at {p}" for p in _walk_todos(adapter)]
    if adapter.get("verified_state") == "proven":
        problems.append("verified_state: proven is never declared in a file; trust comes only from a recorded trust act")
    exe = adapters.executable(adapter)
    if not exe or "TODO" in str(exe):
        problems.append("invocation.executable is missing")
    elif os.path.basename(str(exe)) in SHELLS:
        problems.append(f"invocation.executable {exe} is a shell: a prompt would pass through shell parsing")
    if (adapter.get("safe_prompt_passing") or {}).get("shell") is True:
        problems.append("safe_prompt_passing.shell is true: the prompt must never pass through a shell")
    profiles = adapter.get("office_profiles")
    if not isinstance(profiles, dict) or not profiles:
        problems.append("office_profiles declares no launch form (worker, reviewer or vision)")
        profiles = {}
    rows = _catalog_rows() if rows is None else rows
    mine = [r for r in rows if r.get("invocation_harness") == aid]
    mapping = adapter.get("effort_mapping")
    mapping = mapping if isinstance(mapping, dict) else {}
    for effort in sorted({str(r.get("effort")) for r in mine if r.get("effort")}):
        if effort not in mapping:
            problems.append(f"effort_mapping has no entry for {effort!r}, which catalog rows on {aid} use")
    efforts = sorted(set(mapping) | {str(r.get("effort")) for r in mine if r.get("effort")} or {"medium"})
    justified = adapter.get("trust_justifications") or {}
    justified = justified if isinstance(justified, dict) else {}
    if _is_seed(adapter):
        justified = {**packaged_justifications(aid), **justified}
    flagged: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="office-validate-") as tmp:
        cwd = Path(tmp)
        for kind, prof in profiles.items():
            if kind not in adapters.PROFILE_KINDS:
                problems.append(f"office_profiles.{kind}: unknown launch form (use {', '.join(adapters.PROFILE_KINDS)})")
                continue
            if not isinstance(prof, dict):
                continue
            label = f"office_profiles.{kind}"
            transport = prof.get("prompt")
            if transport not in SAFE_PROMPT_TRANSPORTS:
                problems.append(f"{label}.prompt {transport!r} is not a safe transport ({', '.join(SAFE_PROMPT_TRANSPORTS)})")
            for name, form in _profile_argvs(prof):
                for token in form:
                    token = str(token)
                    if "{prompt}" in token:
                        problems.append(f"{label}.{name} carries {{prompt}}; the runtime appends the prompt per `prompt`")
                    for key, matcher in UNSAFE_TRUST_FLAGS.items():
                        if matcher.search(token):
                            flagged.setdefault(key, f"{label}.{name}")
            if prof.get("herdr_kind") and not (prof.get("interactive") or {}).get("argv"):
                problems.append(f"{label} sets herdr_kind but has no interactive.argv: Herdr launches would "
                                "silently degrade to headless")
            if not exe or not isinstance(prof.get("argv"), list):
                continue
            output = None if kind == "worker" else cwd / "dispatch" / "reply.md"
            images = [cwd / "probe.png"] if kind == "vision" else None
            for effort in efforts:
                try:
                    rendered, _ = adapters.build_argv(adapter, kind, model="probe/model", effort=effort, cwd=cwd,
                                                      output=output, images=images, include_dirs=[cwd],
                                                      session_id="probe-session")
                    forms = [("argv", rendered)]
                    inter = adapters.interactive_argv(adapter, kind, model="probe/model", effort=effort, cwd=cwd,
                                                      include_dirs=[cwd], output=output)
                    if inter:
                        forms.append(("interactive.argv", inter[0]))
                    resumed = adapters.resume_argv(adapter, kind, session_id="probe-session", model="probe/model",
                                                   effort=effort, cwd=cwd, include_dirs=[cwd], output=output)
                    if resumed:
                        forms.append(("interactive.resume_argv", resumed[0]))
                except Exception as e:  # noqa: BLE001 - any render failure is the finding
                    problems.append(f"{label} argv does not render at effort {effort}: {e}")
                    break
                left = sorted({m for _, form in forms for t in form for m in _PLACEHOLDER.findall(str(t))} - {"{prompt}"})
                if left:
                    problems.append(f"{label} argv leaves unknown placeholders at effort {effort}: {', '.join(left)}")
                    break
    for key, where in sorted(flagged.items()):
        if not str(justified.get(key) or "").strip():
            problems.append(f"{where} uses the permission/trust flag {key} with no trust_justifications.{key} "
                            "recording what it grants and why")
    commands = [(f"preflight.{n}", a) for n, a in ((adapter.get("preflight") or {}).items()
                                                   if isinstance(adapter.get("preflight"), dict) else [])]
    commands.append(("quota_probe.command", (adapter.get("quota_probe") or {}).get("command")))
    commands.append(("version_fingerprint.command", (adapter.get("version_fingerprint") or {}).get("command")))
    commands.append(("model_source.command", (adapter.get("model_source") or {}).get("command")))
    for name, argv in commands:
        if argv is None or (isinstance(argv, str) and "TODO" in argv):
            continue
        why = _resolvable(argv, exe)
        if why and why.endswith(_MISSING):
            warnings.append(f"{name}: {why}")
        elif why:
            problems.append(f"{name} is not resolvable: {why}")
    return problems, warnings


def validate(ident: str | None, *, file: str | None = None) -> Result:
    adapter, origin, where = _resolve_adapter(ident, file)
    aid = adapter.get("id") or ident
    problems, warnings = check_full(adapter)
    head = f"harness {aid} ({origin}, {where})"
    notes = []
    shadow = adapters.user_adapter_dir() / f"{aid}.yaml"
    if origin == "seed" and shadow.is_file():
        notes.append(f"{shadow} is ignored: {aid} is a seed adapter and the user file does not set override_seed: true")
    notes += [f"warning: {w}" for w in warnings]
    trust = "trust: unchanged (only a recorded trust act moves a route past valid-unverified)"
    if problems:
        return Result(lines=[f"{head}: {len(problems)} problem(s)"] + [f"  - {p}" for p in problems] + notes + [trust],
                      next=f"fix {where}, then office harness validate {aid}", exit_code=1,
                      data={"harness": aid, "valid": False, "problems": problems})
    rows = [r for r in _catalog_rows() if r.get("invocation_harness") == aid]
    nxt = (f"office harness smoke {aid} --model {rows[0].get('model_id')}" if rows
           else f"office model add {aid}/<model-slug> --effort <effort>")
    return Result(lines=[f"{head}: valid", *notes, trust], next=nxt,
                  data={"harness": aid, "valid": True, "problems": []})


# ------------------------------------------------------------------ smoke

def smoke_log() -> Path:
    return paths.data_home() / "harness-smoke.jsonl"


def _smoke_records(harness: str | None = None) -> list[dict]:
    try:
        lines = smoke_log().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and (harness is None or rec.get("harness") == harness):
            out.append(rec)
    return out


def _record_smoke(rec: dict) -> None:
    log = smoke_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    os.chmod(log, 0o600)


def _smoke_env() -> dict:
    # The smoke agent acts on no run: drop every Office identity it could inherit.
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("OFFICE_RUN", "OFFICE_TASK", "OFFICE_DISPATCH", "OFFICE_ROLE", "OFFICE_STATE_DIR"))}


def _throwaway_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Auto Office harness smoke test. Nothing here matters.\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Auto Office", "GIT_AUTHOR_EMAIL": "office@localhost",
           "GIT_COMMITTER_NAME": "Auto Office", "GIT_COMMITTER_EMAIL": "office@localhost"}
    for args in (["init", "-q"], ["add", "README.md"], ["commit", "-q", "-m", "smoke"]):
        subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=False)
    return repo


def _unsafe_flags(adapter: dict, prof: dict) -> list[str]:
    """Permission/trust flags in a profile's headless argv: the unsafe-flag set
    plus any flag the adapter itself lists under trust_justifications."""
    listed = adapter.get("trust_justifications")
    listed = set(listed) if isinstance(listed, dict) else set()
    found: list[str] = []
    for token in prof.get("argv") or []:
        token = str(token)
        for key, matcher in UNSAFE_TRUST_FLAGS.items():
            if matcher.search(token) and key not in found:
                found.append(key)
        if token in listed and token not in found:
            found.append(token)
    return found


def _run_capped(argv: list[str], *, cwd: Path, stdin_text: str | None, cap: int) -> tuple[int | None, str, str, bool]:
    """Run in its own session so a timeout kills the whole process group: a
    grandchild holding the pipes open cannot hang the smoke."""
    import signal
    proc = subprocess.Popen(argv, cwd=cwd, env=_smoke_env(), text=True, start_new_session=True,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=cap)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                out, err = proc.communicate(timeout=5)
                return None, out or "", err or "", True
            except subprocess.TimeoutExpired:
                continue
        return None, "", "", True


def smoke(ident: str | None, model: str | None, *, effort: str | None = None, timeout: int | None = None,
          allow_unsafe_flags: bool = False) -> Result:
    adapter, origin, _ = _resolve_adapter(ident)
    aid = adapter.get("id") or ident
    if not model:
        raise _usage("name the model to launch", next_step=f"office harness smoke {aid} --model <model>")
    problems = check(adapter)
    if problems:
        raise OfficeError("adapter-invalid", f"{aid} does not validate ({len(problems)} problem(s)); smoke launches only a valid adapter",
                          next_step=f"office harness validate {aid}", exit_code=1)
    if not adapters.installed(adapter):
        raise OfficeError("harness-missing", f"{adapters.executable(adapter)} is not installed on this host", exit_code=1)
    rows = [r for r in _catalog_rows() if r.get("invocation_harness") == aid
            and model in (r.get("model_id"), r.get("invocation_model_id"))]
    slug = (rows[0].get("invocation_model_id") or rows[0].get("model_id")) if rows else model
    effort = effort or (rows[0].get("effort") if rows else None) or "medium"
    profiles = adapter.get("office_profiles") or {}
    kinds = [k for k in adapters.PROFILE_KINDS if isinstance(profiles.get(k), dict)]
    # Prefer a form that carries no permission/trust flag; one that does runs only when asked.
    kind = next((k for k in kinds if not _unsafe_flags(adapter, profiles[k])), None)
    if kind is None:
        flags = sorted({f for k in kinds for f in _unsafe_flags(adapter, profiles[k])})
        if not allow_unsafe_flags:
            raise OfficeError("unsafe-flags", f"every {aid} launch form carries a permission/trust flag "
                              f"({', '.join(flags)}); smoke will not run one unasked", exit_code=1,
                              next_step=f"office harness smoke {aid} --model {model} --allow-unsafe-flags")
        kind = kinds[0]
    prof = profiles[kind]
    started = time.time()
    rec = {"at": now_iso(), "harness": aid, "origin": origin, "version": adapters.harness_version(adapter),
           "model": model, "slug": slug, "effort": effort, "kind": kind, "evidence": "launch",
           "trust": "unchanged", "catalog_row": bool(rows)}
    ok, detail = adapters.run_preflight(adapter, slug)
    if not ok:
        rec.update(result="preflight_failed", detail=detail, duration_s=round(time.time() - started, 2))
        _record_smoke(rec)
        return Result(lines=[f"smoke {aid}/{slug}@{effort}: preflight failed: {detail}",
                             "recorded as launch evidence; trust unchanged"],
                      next="fix the harness sign-in or model, then rerun the smoke", exit_code=1, data=rec)
    with tempfile.TemporaryDirectory(prefix="office-smoke-") as tmp:
        repo = _throwaway_repo(Path(tmp))
        output = None if kind == "worker" else Path(tmp) / "dispatch" / "reply.md"
        if output:
            output.parent.mkdir(parents=True)
        argv, _ = adapters.build_argv(adapter, kind, model=slug, effort=effort, cwd=repo, output=output)
        evidence_argv = list(argv)
        stdin_text = None
        if prof.get("prompt") == "argv":
            argv, evidence_argv = argv + [SMOKE_PROMPT], evidence_argv + ["[PROMPT REDACTED]"]
        elif prof.get("prompt") == "argv-bound":
            flag = prof.get("prompt_flag", "--prompt=")
            argv, evidence_argv = argv + [flag + SMOKE_PROMPT], evidence_argv + [flag + "[PROMPT REDACTED]"]
        else:
            stdin_text = SMOKE_PROMPT
        rec["argv"] = evidence_argv
        cap = timeout or SMOKE_TIMEOUT_S
        rec["unsafe_flags"] = _unsafe_flags(adapter, prof)
        try:
            # No pty and no one answering: a login, trust or permission prompt the
            # harness shows is never approved, so the launch fails or times out.
            # A form's own permission flags are a separate matter, gated above.
            code, out, err, timed_out = _run_capped(argv, cwd=repo, stdin_text=stdin_text, cap=cap)
        except OSError as e:
            code, out, err, timed_out = None, "", str(e), False
        reply = output.read_text(encoding="utf-8", errors="replace") if output and output.is_file() else ""
    answered = bool((reply or out).strip())
    result = "launched" if code == 0 and answered else ("timed_out" if timed_out else "failed")
    rec.update(result=result, exit_code=code, timed_out=timed_out, duration_s=round(time.time() - started, 2),
               output_bytes=len((reply or out).encode()), stderr_tail=err.strip()[-300:])
    _record_smoke(rec)
    summary = (f"smoke {aid}/{slug}@{effort} ({kind}): {result}"
               + (f", exit {code}" if code is not None else "") + f", {rec['output_bytes']} output bytes in {rec['duration_s']}s")
    lines = [summary, f"recorded as launch evidence in {smoke_log()}; trust and conformance unchanged"]
    if result == "launched":
        nxt = f"record trust only by an explicit user act: office approve trust <route> --quote \"<user's words>\""
        if not rows:
            nxt = f"office model add {aid}/{slug} --effort {effort}"
        return Result(lines=lines, next=nxt, data=rec)
    if err.strip():
        lines.append(f"stderr: {err.strip().splitlines()[-1][:200]}")
    return Result(lines=lines, next="check the argv, sign-in and model, then rerun the smoke", exit_code=1, data=rec)


# ------------------------------------------------------------------ list

def _trust_summary(con, aid: str, version: str | None, rows: list[dict]) -> str:
    from office import scoring
    if not rows:
        return "no catalog rows"
    scoring.ensure_trust_schema(con)
    major = scoring.harness_major(version)
    counts: dict[str, int] = {}
    for r in rows:
        _, st = scoring.evaluate_trust_state(con, f"{aid}@{major}/{r.get('model_id')}@{r.get('effort')}")
        counts[st] = counts.get(st, 0) + 1
    return ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))


def harness_list(con) -> Result:
    from office import onboarding
    rows = _catalog_rows()
    lines, data = [], []
    for aid, (a, origin, where) in sorted(adapters.load_sources().items()):
        if not a.get("office_profiles"):
            continue
        mine = [r for r in rows if r.get("invocation_harness") == aid]
        installed = adapters.installed(a)
        version = adapters.harness_version(a) if installed else None
        auth, auth_detail = onboarding.credentials(aid) if installed else ("-", "")
        if auth == "unknown" and (a.get("preflight") or {}).get("auth_check"):
            auth_detail = "checked by preflight auth_check at each launch"
        trust = _trust_summary(con, aid, adapters.route_version(aid, a) if origin == "user-override" else version, mine)
        last = (_smoke_records(aid) or [None])[-1]
        smoke_txt = f"{last['result']} {last['at'][:10]}" if last else "never"
        lines.append(f"{aid:<8} {origin:<13} {'installed ' + (version or 'unknown') if installed else 'not installed':<22}"
                     f" sign-in {auth:<8} rows {len(mine):<3} trust {trust} | smoke {smoke_txt}")
        data.append({"harness": aid, "origin": origin, "path": str(where), "installed": installed, "version": version,
                     "auth": auth, "auth_detail": auth_detail, "rows": len(mine), "trust": trust, "last_smoke": last})
    return Result(lines=lines or ["no adapters declare a launch form"],
                  next="office harness validate <id>, or office harness scaffold <id> --binary <path> for a new one",
                  data={"harnesses": data, "user_adapters": str(adapters.user_adapter_dir())})


# ------------------------------------------------------------------ model

def _parse_model_ident(ident: str | None) -> tuple[str, str]:
    if not ident or "/" not in ident:
        raise _usage("name the route as <harness>/<model-slug>", next_step="office model add pi/provider/model --effort medium")
    harness, _, slug = ident.partition("/")
    if not harness or not slug:
        raise _usage(f"not a <harness>/<model-slug>: {ident!r}")
    return harness, slug


def _efforts(raw: list[str] | None) -> list[str]:
    out = []
    for item in raw or []:
        for e in str(item).split(","):
            e = e.strip()
            if e and e not in out:
                out.append(e)
    return out


def model_add(ident: str | None, efforts: list[str] | None, *, model_id: str | None = None) -> Result:
    harness, slug = _parse_model_ident(ident)
    adapter = adapters.load_all().get(harness)
    if not adapter or not adapter.get("office_profiles"):
        raise _usage(f"no harness adapter {harness!r}", next_step="office harness list")
    want = _efforts(efforts)
    if not want:
        raise _usage("name at least one --effort", next_step=f"office model add {ident} --effort medium")
    mid = model_id or slug.rsplit("/", 1)[-1]
    if not MODEL_ID_RE.match(mid):
        raise _usage(f"model id {mid!r} must be letters, digits, '.', '_' or '-'", next_step="pass --model-id <name>")
    mapping = adapter.get("effort_mapping") if isinstance(adapter.get("effort_mapping"), dict) else {}
    unmapped = [e for e in want if e not in mapping]
    if unmapped:
        raise OfficeError("effort-unmapped", f"{harness} effort_mapping has no entry for {', '.join(unmapped)}", exit_code=1,
                          next_step=f"add the mapping to the {harness} adapter, then office harness validate {harness}")
    existing = {(r.get("invocation_harness"), r.get("model_id"), r.get("effort")) for r in _catalog_rows()}
    dupes = [e for e in want if (harness, mid, e) in existing]
    if dupes:
        raise OfficeError("row-exists", f"{harness}/{mid}@{','.join(dupes)} is already a catalog row", exit_code=1,
                          next_step=f"office model list {harness}")
    overlay = user_catalog.load()
    added = []
    for e in want:
        row = {"model_id": mid, "invocation_model_id": slug, "invocation_harness": harness,
               "invocation_source": f"unverified: added with office model add on {now_iso()[:10]}; unproven until "
                                    "office harness smoke and conformance",
               "dispatchable": True, "source_identifier": None, "source_name": "user-overlay", "effort": e,
               "source_effort": None, "effort_confidence": "unknown", "benchmark_indexes": {}, "price_fields": None,
               "speed_fields": None, "release_date": None,
               "source_snapshot_metadata": {"kind": "user-added", "added_at": now_iso()},
               "content_hash": None, "supersession_metadata": None}
        overlay["models"].append(row)
        added.append(f"{harness}/{mid}@{e}")
    target = user_catalog.save(overlay)
    return Result(lines=[f"added {', '.join(added)} (slug {slug}) to {target}",
                         "benchmark scores: none yet; an opted-in run fills them through office benchmarks brief|submit",
                         "trust: valid-unverified until a recorded trust act"],
                  next=f"office harness smoke {harness} --model {mid}", data={"added": added, "path": str(target)})


def model_list(harness: str | None = None) -> Result:
    from office import benchmarks
    index = benchmarks.index_version()
    user_keys = {(r.get("invocation_harness"), r.get("model_id"), r.get("effort")) for r in user_catalog.load()["models"]}
    lines, data = [], []
    for r in _catalog_rows():
        h = r.get("invocation_harness")
        if not h or (harness and h != harness):
            continue
        key = (h, r.get("model_id"), r.get("effort"))
        score = (r.get("benchmark_indexes") or {}).get(index)
        enabled = r.get("dispatchable") is not False
        origin = "user" if key in user_keys else "seed"
        lines.append(f"{h}/{r.get('model_id')}@{r.get('effort')}  slug {r.get('invocation_model_id') or '-'}  "
                     f"score {score if score is not None else '-'}  {origin}{'' if enabled else '  disabled'}")
        data.append({"harness": h, "model_id": r.get("model_id"), "effort": r.get("effort"),
                     "slug": r.get("invocation_model_id"), "score": score, "origin": origin, "dispatchable": enabled})
    seed = yaml.safe_load((paths.resources_root() / "catalog" / "seed.yaml").read_text(encoding="utf-8")) or {}
    lines += [f"warning: {w}" for w in user_catalog.merge(list(seed.get("models") or []))[1]]
    return Result(lines=lines or [f"no catalog rows{' for ' + harness if harness else ''}"],
                  next="office model add <harness>/<slug> --effort <e>, or office model disable <harness>/<model>[@effort]",
                  data={"rows": data})


def model_disable(ident: str | None, *, reason: str | None = None) -> Result:
    m = re.match(r"^(?P<h>[a-z0-9-]+)/(?P<m>[A-Za-z0-9_.-]+)(?:@(?P<e>[a-z]+))?$", (ident or "").strip())
    if not m:
        raise _usage("name the row as <harness>/<model>[@effort]", next_step="office model list")
    entry = {"harness": m["h"], "model_id": m["m"]}
    if m["e"]:
        entry["effort"] = m["e"]
    hits = [r for r in _catalog_rows() if user_catalog.matches(entry, r)]
    if not hits:
        raise _usage(f"{ident} names no catalog row", next_step=f"office model list {m['h']}")
    live = [r for r in hits if r.get("dispatchable") is not False]
    if not live:
        return Result(lines=[f"{ident} is already disabled"], next=None, data={"disabled": ident, "changed": False})
    overlay = user_catalog.load()
    overlay["disabled"].append({**entry, "reason": reason or "", "at": now_iso()})
    target = user_catalog.save(overlay)
    rows = ", ".join(f"{r.get('invocation_harness')}/{r.get('model_id')}@{r.get('effort')}" for r in live)
    return Result(lines=[f"disabled {rows} (dispatchable: false) in {target}"],
                  next="routing skips it from the next candidate build; office model list shows it disabled",
                  data={"disabled": ident, "rows": len(live), "changed": True, "path": str(target)})
