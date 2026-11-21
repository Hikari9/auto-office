"""Hermes access: which providers Hermes can reach, how to launch it, and what a run reports back.

Nothing here reads a secret value. `hermes auth list` is parsed for provider names and credential status only; a
credential's label, id and source are dropped at the parser.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

AUTH_LIST_TIMEOUT_S = 20
# Every HERMES_* variable steers Hermes (model, provider, config, session). A launch clears them so a dispatch
# runs the route Office pinned, not whatever the operator's shell exported; HERMES_HOME names the profile.
ENV_PREFIX = "HERMES_"
ENV_KEEP = frozenset({"HERMES_HOME"})

_PROVIDER = re.compile(r"^(?P<name>[A-Za-z0-9][\w.-]*) \((?P<count>\d+) credentials?\):\s*$")
_CREDENTIAL = re.compile(r"^\s+#\d+\s+\S+\s+\S+\s+(?P<rest>.*)$")


def parse_auth_list(text: str) -> dict[str, dict[str, int]]:
    """provider -> {"credentials", "usable", "exhausted"} from `hermes auth list` output.

    A `name (N credentials):` heading opens a provider; each `#n` line under it is one credential, exhausted when
    the line says so. A provider that is only mentioned (`nous: ... Run hermes auth upgrade`) holds no credential.
    """
    out: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for line in (text or "").splitlines():
        heading = _PROVIDER.match(line)
        if heading:
            current = out.setdefault(heading["name"], {"credentials": 0, "usable": 0, "exhausted": 0})
            continue
        cred = _CREDENTIAL.match(line)
        if cred and current is not None:
            current["credentials"] += 1
            current["exhausted" if re.search(r"\bexhausted\b", cred["rest"]) else "usable"] += 1
        elif line.strip() and not line.startswith(" "):
            current = None
    return out


def auth_status(*, hermes: str | None = None) -> dict[str, dict[str, int]]:
    """`parse_auth_list` of a live `hermes auth list`; {} when Hermes is absent, slow or errors."""
    exe = hermes or shutil.which("hermes")
    if not exe:
        return {}
    try:
        proc = subprocess.run([exe, "auth", "list"], capture_output=True, text=True, timeout=AUTH_LIST_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, env=launch_env(os.environ))
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_auth_list(proc.stdout) if proc.returncode == 0 else {}


def reachable_providers(*, hermes: str | None = None) -> list[str]:
    """Sorted provider names with at least one credential that is not exhausted."""
    return sorted(name for name, info in auth_status(hermes=hermes).items() if info["usable"] > 0)


def launch_env(env) -> dict[str, str]:
    """`env` without any HERMES_* variable but HERMES_HOME."""
    return {k: v for k, v in env.items() if not k.startswith(ENV_PREFIX) or k in ENV_KEEP}


def scrub_shell_line() -> str:
    """A POSIX line for a pane shell (its environment is not ours) that unsets the same variables."""
    keep = " ".join(sorted(ENV_KEEP))
    return (f"for _v in $(env | sed -n 's/^\\({ENV_PREFIX}[A-Za-z0-9_]*\\)=.*/\\1/p'); do "
            f"case \" {keep} \" in *\" $_v \"*) ;; *) unset \"$_v\";; esac; done; unset _v")


_EXEC = re.compile(r"^exec\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))\s", re.M)
_ROOT = re.compile(r"sys\.path\.insert\(0,\s*(?:'\"'\"')?([^'\"\s,)]+)")


def resolve_launcher(hermes: str | None = None) -> list[str] | None:
    """argv prefix that starts Hermes as a process Herdr's `hermes` agent kind recognises.

    A git-installed Hermes puts a shell wrapper on PATH that execs `python3 -I -c '<inline script>'`: the running
    process then carries no `hermes` in its argv and Herdr cannot detect it. The checkout holds the real launcher
    script (`<install>/hermes`), which runs under the same interpreter, so the resolved form is
    `[interpreter, <install>/hermes]`. Any other `hermes` (a console script, a binary) is returned as itself;
    None when Hermes is not installed.
    """
    exe = hermes or shutil.which("hermes")
    if not exe:
        return None
    try:
        head = Path(exe).read_bytes()[:65536]
    except OSError:
        return [exe]
    if not head.startswith(b"#!/bin/sh"):
        return [exe]
    text = head.decode("utf-8", "replace")
    found = _EXEC.search(text)
    interpreter = next((g for g in found.groups() if g), None) if found else None
    root = _ROOT.search(text)
    script = Path(root[1]) / "hermes" if root else None
    if interpreter and script and Path(interpreter).is_file() and script.is_file():
        return [interpreter, str(script)]
    return [exe]


def write_launcher_shim(directory: Path, hermes: str | None = None) -> dict[str, str]:
    """Env that puts a detectable `hermes` first on PATH; {} when the PATH one is already detectable.

    `herdr agent start --kind hermes` runs the PATH `hermes`. The shim execs the resolved launcher, so the
    foreground process in the pane is `python3 <install>/hermes ...`.
    """
    launcher = resolve_launcher(hermes)
    if not launcher or len(launcher) < 2:
        return {}
    shim = Path(directory) / "hermes"
    shim.parent.mkdir(parents=True, exist_ok=True)
    quoted = " ".join("'" + part.replace("'", "'\"'\"'") + "'" for part in launcher)
    shim.write_text(f"#!/bin/sh\nexec {quoted} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    return {"PATH": f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"}


def read_usage(path: str | Path) -> dict | None:
    """The `--usage-file` report, or None when it is missing or not a JSON object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def expected_route(slug: str) -> tuple[str | None, str]:
    """(provider, model) a pinned slug names: `provider:model` pins both, a bare id only the model."""
    provider, sep, model = slug.partition(":")
    return (provider, model) if sep and provider and model else (None, slug)


def readback_problem(slug: str, usage: dict | None, *, require_completed: bool = False) -> str | None:
    """Why the usage report is not the route the dispatch pinned, or None when it matches.

    A missing report is a problem too: with no readback the model that ran is unknown. A model matches exactly
    or as the tail of a vendor-qualified id (`anthropic/claude-x` reports `claude-x`, or the reverse).
    `require_completed` (a clean exit) fails closed unless `completed` is exactly true.
    """
    if usage is None:
        return "Hermes wrote no usage report, so the model that ran is unknown"
    provider, model = expected_route(slug)
    got_model, got_provider = _norm(usage.get("model")), _norm(usage.get("provider"))
    want = _norm(model)
    if not got_model or not (got_model == want or got_model.endswith("/" + want) or want.endswith("/" + got_model)):
        return f"Hermes ran model {usage.get('model')!r}, not the pinned {model!r}"
    if provider and got_provider != _norm(provider):
        return f"Hermes ran provider {usage.get('provider')!r}, not the pinned {provider!r}"
    if require_completed and usage.get("completed") is not True:
        return f"Hermes exited cleanly but reported completed={usage.get('completed')!r}, not true"
    return None
