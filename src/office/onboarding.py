"""First-run /auto-office onboarding (#484): `office onboard`.

The /auto-office skill asks three questions (default external planner,
preferred executor, preferred reviewer) through the host harness's own
question tool; this module owns everything behind them. It reports whether the
user completed the current onboarding schema, which routes are eligible for each
question here, and the current harness's Office integration, then applies the
answers. Answers are written with `office setup`'s validated, atomic user-file
write as ordinary `roles.<role>.preferred_seed` values, so they keep the routing
semantics those already have: a stronger advisory seed for planner and reviewer,
weighted evidence for executor. They never touch the repo file, never set an
orchestrator preference, and never install hooks; hooks stay with
`office install --only <current harness>`.

Re-onboarding keys on SCHEMA_VERSION alone, never on the package version.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from office import adapters, candidates, route_policy, configcmd, db, discovery, harness_preference, hermes_access, install, paths, portable, routing
from office import config as cfg
from office.result import Result
from office.state import OfficeError

# Bump only when the questions change enough that existing users should see them again.
SCHEMA_VERSION = 2
SCHEMA_KEY = "onboarding.schema_version"
FREE_MODELS_KEY = "onboarding.free_models_enabled"
# Harness preference round: due once per overlapping harness, not a schema bump (the three questions stay as they are).
OVERLAP_SEEN_KEY = "onboarding.harness_overlap_seen"
BUDGETS_KEY = "onboarding.harness_budgets"
# A monthly budget above this is refused as a typo: larger values overflow the share arithmetic to NaN.
MAX_MONTHLY_USD = 1_000_000_000.0
PREFERENCE_KEY = f"routing.{harness_preference.KEY}"
RECOMMENDED = "recommended"
DISCLAIMER = ("Office favors this route according to its routing policy, but may choose another model or effort "
              "when availability, quota, task fit, capability, evidence, or policy calls for it.")
# (answer flag, question, config roles it writes, what the preference does)
QUESTIONS = (
    ("planner", "Default external planner", ("planner",),
     "Used when a run queues a dedicated planner. When you plan inline, the orchestrator (this session) plans."),
    ("executor", "Preferred executor", ("executor",),
     "Weighted evidence in adaptive routing (preference weight 10% by default, capped at 25%); "
     "task fit, capability, effectiveness, cost, speed, quota and gear policy still apply."),
    ("reviewer", "Preferred reviewer", ("plan_reviewer", "code_reviewer"),
     "Plan and code review. Visual review keeps its own capability-checked routes."),
)
OFFICE, KEEP = "office", "keep"
HARNESS_ANSWERS = (OFFICE, KEEP, RECOMMENDED)
HOOK_ACTIONS = ("install-recommended", "review-individually", "skip")
EFFORT_ORDER = ("none", "low", "medium", "high", "xhigh", "max")


def _usage(message: str, next_step: str | None = None) -> OfficeError:
    return OfficeError("usage", message, next_step=next_step, exit_code=2)


# ------------------------------------------------------------------ state

def completed_schema() -> int:
    """The onboarding schema the user last completed or skipped (0: never).
    Read from the user file only: completion is the user's, not a repo's."""
    data = configcmd._load(paths.user_config_path())
    value = (data.get("onboarding") or {}).get("schema_version") if isinstance(data.get("onboarding"), dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def free_models_enabled() -> bool:
    """Read the user's explicit free-model consent; auth and availability are separate."""
    data = configcmd._load(paths.user_config_path())
    onboarding = data.get("onboarding")
    return isinstance(onboarding, dict) and onboarding.get("free_models_enabled") is True


def due_reason(completed: int) -> str | None:
    if completed >= SCHEMA_VERSION:
        return None
    if completed == 0:
        return "first run: onboarding has not been completed or skipped"
    return f"onboarding schema {SCHEMA_VERSION} is newer than the one completed ({completed})"


# ------------------------------------------------------------------ environment

def current_harness(explicit: str | None = None) -> tuple[str | None, str]:
    """The harness hosting this session, and how it was found. The skill passes
    --harness; the environment and the process tree are fallbacks."""
    if explicit:
        return explicit, "--harness"
    if os.environ.get("OFFICE_HARNESS"):
        return os.environ["OFFICE_HARNESS"], "OFFICE_HARNESS"
    if os.environ.get("CLAUDECODE") == "1":
        return "claude", "CLAUDECODE"
    key = discovery.process_key()
    if key:
        return key.split(":", 1)[0], "process tree"
    return None, "unknown"


def _claude_signed_in() -> tuple[bool, str]:
    if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                                       "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")):
        return True, "environment"
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if (home / ".credentials.json").is_file():
        return True, str(home / ".credentials.json")
    if sys.platform == "darwin" and shutil.which("security"):
        # Attributes only (no -w): the secret is never read.
        try:
            proc = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials"],
                                  capture_output=True, timeout=5)
            if proc.returncode == 0:
                return True, "macOS keychain"
        except (OSError, subprocess.SubprocessError):
            pass
    return False, f"no credentials in {home} or the keychain (run claude and log in)"


# `opencode auth list` (default --format), from opencode 2.0.29's own command: it lists
# connections only, one line each: integration.padEnd(W) + source.padEnd(28) + type, where W is
# the longest integration name on any line plus 2 and type is `stored` (source is the saved
# credential's label) or `environment` (source is the variable name). With no connection it
# prints exactly "No authenticated integrations". Exit status is 0 either way, so sign-in is
# recognized from stdout alone, and only when every line has that column structure: a source
# of 28+ characters runs straight into the type, a shorter one is space-padded to 28.
_OPENCODE_NO_INTEGRATIONS = "No authenticated integrations"
_OPENCODE_ENTRY = re.compile(r"(\S.*?)( {2,})(.*)(stored|environment)")
_OPENCODE_SOURCE_WIDTH = 28
_OPENCODE_MAX_LINE = 512


def _opencode_entries(lines: list[str]) -> int:
    """Number of integration entries when every line is one and the integration column is the
    shared width the contract produces; 0 for anything else."""
    rows = []  # (integration length, spaces after it, length from integration end to type)
    for ln in lines:
        m = _OPENCODE_ENTRY.fullmatch(ln) if len(ln) <= _OPENCODE_MAX_LINE else None
        if not m:
            return 0
        rows.append((len(m[1]), len(m[2]), len(ln) - len(m[4]) - len(m[1])))
    width = max(name for name, _, _ in rows) + 2
    for name, gap, tail in rows:
        pad = width - name
        if gap < pad or tail - pad < _OPENCODE_SOURCE_WIDTH:
            return 0
    return len(rows)


def _run_opencode(*args: str) -> tuple[subprocess.CompletedProcess | None, str]:
    """Run one opencode command; on failure return None and a one-line reason."""
    label = "opencode " + " ".join(args)
    try:
        proc = subprocess.run(portable.resolve_argv(["opencode", *args]), capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"{label} timed out"
    except (OSError, subprocess.SubprocessError):
        return None, f"could not run {label}"
    if proc.returncode != 0:
        out = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        detail = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
        return None, (f"{label} exited {proc.returncode}: {detail}" if detail
                      else f"{label} exited {proc.returncode}")
    return proc, ""


def _opencode_signed_in() -> tuple[bool, str]:
    if shutil.which("opencode") is None:
        return False, "opencode is not installed"
    proc, why = _run_opencode("auth", "list")
    if proc is None:
        return False, why
    lines = [ln.rstrip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return False, "opencode auth list printed nothing (run opencode auth login)"
    if any(ln.lstrip().startswith(_OPENCODE_NO_INTEGRATIONS) for ln in lines):
        return False, "no authenticated integrations (run opencode auth login)"
    entries = _opencode_entries(lines)
    if not entries:
        return False, "opencode auth list printed no recognizable integration entry (output not echoed)"
    return True, f"opencode auth list ({entries} authenticated integration{'s' if entries != 1 else ''})"


_KILO_CREDENTIAL_COUNT = re.compile(r"\s*(\d{1,9}) credentials? in (\S(?:.*\S)?)\s*", re.I)
_KILO_CREDENTIAL_ENTRY = re.compile(r"\s*([\w.-][\w .-]{0,63})\s*[:|]\s*(?:API key|OAuth|token|credential)\s*", re.I)
_KILO_AUTH_MAX_LINE = 512


def _kilo_signed_in() -> tuple[bool, str]:
    if shutil.which("kilo") is None:
        return False, "kilo is not installed"
    try:
        proc = subprocess.run(portable.resolve_argv(["kilo", "auth", "list"]), capture_output=True, timeout=10,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "kilo auth list timed out"
    except (OSError, subprocess.SubprocessError):
        return False, "could not run kilo auth list"
    try:
        # Both streams, strictly: invalid UTF-8 on either is unparseable. Decoded here, not by text=True, which on
        # Windows decodes in a reader thread whose error is lost (the stream then reads as nothing).
        proc.stdout, proc.stderr = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")
    except UnicodeDecodeError:
        return False, "kilo auth list printed unparseable output"
    if proc.returncode != 0:
        return False, f"kilo auth list exited {proc.returncode}"

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        return False, "kilo auth list printed nothing"
    counts = [_KILO_CREDENTIAL_COUNT.fullmatch(line) if len(line) <= _KILO_AUTH_MAX_LINE else None
              for line in lines]
    count_lines = [match for match in counts if match]
    if count_lines:
        if len(count_lines) != 1:
            return False, "kilo auth list printed unparseable credential counts"
        if len(lines) != 1:
            return False, "kilo auth list printed no recognizable credential entry"
        count = int(count_lines[0][1])
        if count == 0:
            return False, "kilo auth list reported zero credentials"
        return True, f"kilo auth list ({count} credential{'s' if count != 1 else ''})"

    entries = [match for line in lines if len(line) <= _KILO_AUTH_MAX_LINE
               and (match := _KILO_CREDENTIAL_ENTRY.fullmatch(line))]
    if entries and len(entries) == len(lines):
        return True, f"kilo auth list ({len(entries)} credential entr{'y' if len(entries) == 1 else 'ies'})"
    return False, "kilo auth list printed no recognizable credential entry"


def _opencode_free_model(expected: str | None = None) -> tuple[str | None, str]:
    """Return a listed free model, optionally requiring one exact catalog slug."""
    proc, why = _run_opencode("models")
    if proc is None:
        return None, why
    for line in (proc.stdout or "").splitlines():
        for token in line.split():
            slug = token.strip(" ,")
            if slug.endswith("-free") and "/" in slug and (expected is None or slug == expected):
                return slug, ""
    return None, (f"opencode models does not list {expected}" if expected else
                  "opencode models lists no explicitly free model")


def _files_or_env(files: list[Path], env: tuple[str, ...], hint: str) -> tuple[bool, str]:
    if any(os.environ.get(k) for k in env):
        return True, "environment"
    for f in files:
        if f.is_file():
            return True, str(f)
    return False, f"no {' or '.join(str(f) for f in files)} ({hint})"


def credentials(harness: str) -> tuple[str, str]:
    """`found`, `missing` or `unknown`, with where it looked. For OpenCode,
    `found` means authenticated or explicitly usable without credentials.
    OFFICE_AUTH_FIXTURE ({"claude": "found", ...}) replaces the check in tests."""
    fixture = os.environ.get("OFFICE_AUTH_FIXTURE")
    if fixture:
        import json
        state = json.loads(fixture).get(harness, "unknown")
        return state, "fixture"
    gemini_home = Path.home() / ".gemini"
    if harness == "claude":
        ok, where = _claude_signed_in()
    elif harness == "codex":
        ok, where = _files_or_env([Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"],
                                  ("OPENAI_API_KEY",), "run codex login")
    elif harness == "agy":
        ok, where = _files_or_env([gemini_home / "antigravity-cli" / "antigravity-oauth-token",
                                   gemini_home / "jetski-standalone-oauth-token"], (), "sign in to agy")
    elif harness == "opencode":
        ok, where = _opencode_signed_in()
    elif harness == "kilo":
        ok, where = _kilo_signed_in()
    else:
        return "unknown", "Office has no sign-in check for this harness"
    return ("found" if ok else "missing"), where


def harnesses(auth: dict | None = None) -> list[dict]:
    """Every adapter that can launch an Office role: installed, version, sign-in. Hermes also lists its
    providers with a usable or exhausted credential (names and status only, from hermes_access)."""
    out = []
    for aid, a in sorted(adapters.load_all().items()):
        if not a.get("office_profiles"):
            continue
        row = {"harness": aid, "installed": adapters.installed(a)}
        if row["installed"]:
            row["version"] = adapters.harness_version(a) or "unknown"
            row["auth"], row["auth_detail"] = credentials(aid)
            if aid == "opencode":
                model, detail = _opencode_free_model()
                row["free_model_availability"] = {"listed_model": model, "detail": detail}
            if aid == "hermes":
                row["providers"] = _hermes_providers(auth)
        out.append(row)
    return out


def _hermes_providers(auth: dict | None = None) -> list[dict]:
    auth = hermes_access.auth_status() if auth is None else auth
    return [{"provider": name, "status": "usable" if info["usable"] > 0 else "exhausted"}
            for name, info in sorted(auth.items())]


# ------------------------------------------------------------------ routes

def route_text(c: dict) -> str:
    return f"{c['harness']}/{c['model_id']}@{c['effort']}"


def role_routes(con, config: dict, role: str) -> tuple[list[dict], dict[str, str]]:
    """(eligible candidates, {route: why not}) for one config role, using the
    runtime's own qualification: installed adapter with a launch profile, the
    catalog row and its efforts, model family floors, a found or unknown
    sign-in, then routing stages 1-5 (exclusions, derived trust, required
    capabilities, the role floor). Quota and task fit stay with the router at
    dispatch, so they are not judged here."""
    built, skipped = candidates.build_candidates(con, role, probe=False,
                                                 family_floors=config.get("model_family_floors"))
    why_not = {s["candidate"]: s["reason"] for s in skipped}
    usable = []
    # Each sign-in check and model listing can spawn the harness CLI, so ask once per harness/slug.
    signed_in: dict[str, tuple[str, str]] = {}
    listings: dict[str | None, tuple[str | None, str]] = {}
    for c in built:
        if c["harness"] not in signed_in:
            signed_in[c["harness"]] = credentials(c["harness"])
        state, where = signed_in[c["harness"]]
        if c.get("free_model") and c.get("harness") == "opencode":
            slug = c.get("invocation_model_id")
            if slug not in listings:
                listings[slug] = _opencode_free_model(slug)
            listed, availability = listings[slug]
            if not listed:
                why_not[route_text(c)] = f"free model unavailable: {availability}"
                continue
            if state != "found":
                usable.append(c)
                continue
        if state == "missing":
            why_not[route_text(c)] = f"{c['harness']} is not signed in: {where}"
            continue
        usable.append(c)
    policy = candidates.role_policy(config, role)
    result = routing.route({
        "role": role, "adaptive": False, "candidates": usable, "runs_db": str(paths.runs_db()),
        "allow_free_models": free_models_enabled(),
        "user_policies": [route_policy.user_policy(config), *candidates.current_user_policies()],
        "policy": {"required_capabilities": list(policy.get("required_capabilities") or []),
                   "floor": policy.get("floor")},
    })
    rejected = {r["candidate"]: r["reason"] for r in result.get("rejected") or [] if (r.get("stage") or 99) <= 5}
    eligible = []
    for c in usable:
        reason = rejected.get(routing.candidate_id(c))
        if reason:
            why_not[route_text(c)] = reason
        else:
            eligible.append(c)
    return eligible, why_not


def _question_routes(con, config: dict, roles: tuple[str, ...]) -> tuple[list[dict], dict[str, str]]:
    """Routes eligible for every role a question writes, in catalog order."""
    eligible: list[dict] | None = None
    why_not: dict[str, str] = {}
    for role in roles:
        cands, why = role_routes(con, config, role)
        for route, reason in why.items():
            why_not.setdefault(route, f"{role}: {reason}" if len(roles) > 1 else reason)
        names = {route_text(c) for c in cands}
        eligible = cands if eligible is None else [c for c in eligible if route_text(c) in names]
    seen, out = set(), []
    for c in eligible or []:
        if route_text(c) not in seen:
            seen.add(route_text(c))
            out.append(c)
    return out, why_not


def _seeded(roles: tuple[str, ...], eligible: list[dict]) -> list[str]:
    """The shipped preferred_seed entries of these roles, each as the first
    eligible route it matches, in seed order."""
    default = cfg.load_yaml(cfg.default_config_path()) or {}
    out: list[str] = []
    for role in roles:
        for entry in ((default.get("roles") or {}).get(role) or {}).get("preferred_seed") or []:
            match = next((c for c in eligible if routing.preferred_rank(c, [entry]) is not None), None)
            if match and route_text(match) not in out:
                out.append(route_text(match))
    return out


def _grouped(routes: list[dict]) -> list[str]:
    """`harness/model@{efforts}`, one line per harness/model."""
    by: dict[str, set[str]] = {}
    for c in routes:
        by.setdefault(f"{c['harness']}/{c['model_id']}", set()).add(c["effort"])
    return [f"{k}@{{{','.join(sorted(v, key=lambda e: EFFORT_ORDER.index(e) if e in EFFORT_ORDER else 99))}}}"
            for k, v in by.items()]


def _show_seed(seed) -> str:
    return ",".join(configcmd.format_route(e) for e in seed or [])


def _current(prefs: dict[str, dict], roles: tuple[str, ...], eligible: list[str]) -> dict:
    """The answer that keeps things as they are, for prefilling the question."""
    user = [prefs[r]["user"] for r in roles]
    if all(u is None for u in user):
        cur = {"choice": OFFICE, "value": None, "origin": "default"}
    elif all(u == user[0] for u in user):
        cur = {"choice": "route", "value": _show_seed(user[0]), "origin": "user"}
    else:
        cur = {"choice": "mixed", "value": {r: _show_seed(prefs[r]["user"]) or OFFICE for r in roles}, "origin": "user"}
    if cur["choice"] == "route":
        cur["eligible"] = cur["value"] in eligible
    repo = {r: _show_seed(prefs[r]["repo"]) for r in roles if prefs[r]["repo"] is not None}
    cur["repo_override"] = repo or None
    return cur


def questions(con, cwd: Path | None = None) -> list[dict]:
    root = configcmd._repo_root(cwd)
    config, _ = cfg.resolve(root)
    prefs = configcmd.role_preferences([r for _, _, roles, _ in QUESTIONS for r in roles], cwd)
    out = []
    for qid, title, roles, about in QUESTIONS:
        eligible, why_not = _question_routes(con, config, roles)
        names = [route_text(c) for c in eligible]
        seeded = _seeded(roles, eligible)
        current = _current(prefs, roles, names)
        options = [{"answer": OFFICE, "label": "Let Office decide", "recommended": True,
                    "description": "Clear your user-level preference; the shipped routing defaults apply."}]
        if current["choice"] in ("route", "mixed") and current["value"] not in seeded:
            label = current["value"] if current["choice"] == "route" else "your current per-role preferences"
            options.append({"answer": KEEP, "label": f"Keep {label} (current)",
                            "description": "Leave this preference as it is." + (
                                " Not eligible here right now." if current.get("eligible") is False else "")})
        options += [{"answer": r, "label": r, "seeded": True,
                     "description": "Shipped default route." + (" (current)" if r == current.get("value") else "")}
                    for r in seeded]
        options.append({"answer": "custom", "label": "Custom",
                        "description": "harness/model@effort, one of the eligible routes"})
        out.append({"id": qid, "title": title, "roles": list(roles), "about": about, "current": current,
                    "options": options, "eligible": names, "eligible_grouped": _grouped(eligible),
                    "unavailable": [{"route": k, "reason": v} for k, v in sorted(why_not.items())]})
    return out


def validate_answer(question: dict, answer: str) -> dict | None:
    """The seed entry an answer writes (None for Let Office decide / keep).
    Raises a usage error naming why a route is not selectable."""
    answer = answer.strip()
    if answer in (OFFICE, KEEP):
        return None
    entry = configcmd.parse_route(answer)
    if not entry.get("harness") or not entry.get("effort"):
        raise _usage(f"--{question['id']} {answer!r}: name the full route as harness/model@effort",
                     next_step=f"eligible: {', '.join(question['eligible_grouped']) or 'none here'}")
    problems = configcmd.validate_seed([entry])
    if problems:
        raise _usage(f"--{question['id']}: " + "; ".join(problems),
                     next_step=f"eligible: {', '.join(question['eligible_grouped']) or 'none here'}")
    text = configcmd.format_route(entry)
    if text not in question["eligible"]:
        why = {u["route"]: u["reason"] for u in question["unavailable"]}.get(text)
        raise _usage(f"--{question['id']} {text}: not eligible for {question['title'].lower()} here"
                     + (f" ({why})" if why else ""),
                     next_step=f"eligible: {', '.join(question['eligible_grouped']) or 'none here'}")
    return {"model_id": entry["model_id"], "harness": entry["harness"], "effort": entry["effort"]}


# ------------------------------------------------------------------ harness preference

def _user_onboarding() -> dict:
    data = configcmd._load(paths.user_config_path())
    onboarding = data.get("onboarding")
    return onboarding if isinstance(onboarding, dict) else {}


def _recorded() -> list[str]:
    """Harnesses whose overlap the user has answered. Anything but a list of names is treated as none."""
    value = _user_onboarding().get("harness_overlap_seen")
    return [h for h in value if isinstance(h, str)] if isinstance(value, list) else []


def _stored_budgets() -> dict:
    value = _user_onboarding().get("harness_budgets")
    return value if isinstance(value, dict) else {}


def _usd(value) -> float | None:
    """A budget as a non-negative finite number, else None (a hand-edited value never reaches the arithmetic)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= MAX_MONTHLY_USD:
        return None
    return float(value)


def _apply_harness(answer: str | None, budgets_specs: list[str] | None) -> tuple[list[str], list[tuple[str, object]]]:
    """(summary lines, config changes) for the harness preference round. Validation happens before any change is
    returned, so an invalid answer changes nothing. No answer (a skipped round) writes nothing."""
    if answer is None:
        return [], []
    overlap = harness_overlap(hermes_access.auth_status())
    if not overlap:
        raise _usage("no installed harness shares a model with another here, so there is no harness preference to answer",
                     next_step="office onboard --harness <h> --json")
    onboarding_budgets = _stored_budgets()
    lines: list[str] = []
    changes: list[tuple[str, object]] = []
    current = harness_preference.settings(configcmd._load(paths.user_config_path()))
    if answer == RECOMMENDED:
        budgets = _parse_budgets(budgets_specs, overlap)
        missing = _missing_budgets(overlap, budgets)
        if missing:
            raise _usage("recommended weights need a monthly USD budget for each overlapping harness: " + ", ".join(missing),
                         next_step="office onboard --harness-preference recommended --harness-budget PROVIDER:HARNESS=USD ...")
        weights, reasoning = derive_weights(overlap, budgets)
        new_prefs = {k: v for k, v in current.items() if k not in overlap} | weights
        stored = {**onboarding_budgets, **budgets}
        if stored != onboarding_budgets:
            changes.append((BUDGETS_KEY, stored))
        lines.append("Harness preference: recommended, derived from your monthly budgets")
        lines += [f"  {r}" for r in reasoning]
    elif answer == OFFICE:
        new_prefs = {k: v for k, v in current.items() if k not in overlap}
        lines.append("Harness preference: Let Office decide (weights for the overlapping providers cleared)")
    else:
        new_prefs = current
        lines.append("Harness preference: kept")
    if (new_prefs or None) != (current or None):
        changes.append((PREFERENCE_KEY, new_prefs or None))
    return lines, changes + _seen_changes(overlap)


def _seen_changes(overlap: dict) -> list[tuple[str, object]]:
    """Record the overlap set as seen (only when it differs from what is recorded), so the round is not asked again.
    Bare harness names from before entries carried the model are replaced by the entries they cover."""
    seen = set(_recorded())
    now = overlap_keys(overlap) | {s for s in seen if "|" in s}
    return [(OVERLAP_SEEN_KEY, sorted(now))] if now != seen else []


def _covered(key: str, seen: set[str]) -> bool:
    """A recorded entry covers its key; a bare harness name (recorded before entries carried the model) covers its harness."""
    return key in seen or key.rsplit("|", 1)[-1] in seen


def harness_overlap(auth: dict) -> dict[str, dict]:
    """Providers whose models two or more installed harnesses can launch: {provider: {models, harnesses}}.
    A model is overlapping when at least two harnesses run it; Hermes counts only for a provider it reaches
    with a usable credential (`auth`, from hermes_access.auth_status). Grouped by the model's vendor, which is
    the scope `routing.harness_preference` keys on."""
    installed = {aid for aid, a in adapters.load_all().items() if a.get("office_profiles") and adapters.installed(a)}
    serving: dict[tuple[str, str], set[str]] = {}
    for row in candidates.catalog_rows():
        harness, model = row.get("invocation_harness"), row.get("model_id")
        provider = (row.get("lineage_key") or "").split("/", 1)[0]
        if harness not in installed or not model or not provider:
            continue
        if harness == "hermes":
            reached, _ = hermes_access.expected_route(row.get("invocation_model_id") or "")
            if not reached or (auth.get(reached) or {}).get("usable", 0) < 1:
                continue
        serving.setdefault((provider, model), set()).add(harness)
    out: dict[str, dict] = {}
    for (provider, model), harnesses_ in sorted(serving.items()):
        if len(harnesses_) < 2:
            continue
        entry = out.setdefault(provider, {"models": [], "harnesses": set(), "pairs": []})
        entry["models"].append(model)
        entry["harnesses"] |= harnesses_
        entry["pairs"] += [f"{model}|{h}" for h in sorted(harnesses_)]
    return {p: {"models": e["models"], "harnesses": sorted(e["harnesses"]), "pairs": e["pairs"]}
            for p, e in sorted(out.items())}


def overlap_keys(overlap: dict) -> set[str]:
    """The overlap set the round is answered against: one `provider|model|harness` per harness running a shared model,
    so a newly shared model reopens the round even when its harnesses were already recorded."""
    return {f"{p}|{pair}" for p, info in overlap.items() for pair in info["pairs"]}


def _missing_budgets(overlap: dict, budgets: dict) -> list[str]:
    return [f"{p}:{h}" for p, info in overlap.items() for h in info["harnesses"]
            if (budgets.get(p) or {}).get(h) is None]


def derive_weights(overlap: dict, budgets: dict) -> tuple[dict, list[str]]:
    """Weights from the monthly USD budgets: each harness's share of its provider's total, in percent.
    A provider with no budget at all gets no weights (routing keeps its default). Returns (weights, reasoning)."""
    weights: dict[str, dict[str, float]] = {}
    lines: list[str] = []
    for provider, info in overlap.items():
        spend = budgets[provider]
        total = sum(spend[h] for h in info["harnesses"])
        if total <= 0:
            lines.append(f"{provider}: no monthly budget, so no weights here (routing keeps its default)")
            continue
        shares = {h: round(100 * spend[h] / total, 1) for h in info["harnesses"]}
        weights[provider] = shares
        split = ", ".join(f"{h} ${spend[h]:g}/mo of ${total:g} = {shares[h]:g}%" for h in info["harnesses"])
        lines.append(f"{provider}: {split}; weights " + ", ".join(f"{h} {shares[h]:g}" for h in info["harnesses"]))
    return weights, lines


def _parse_budgets(specs: list[str] | None, overlap: dict) -> dict[str, dict[str, float]]:
    """`--harness-budget PROVIDER:HARNESS=USD`: each must name an overlapping pair; nothing is written on error."""
    budgets: dict[str, dict[str, float]] = {}
    for spec in specs or []:
        pair, sep, value = spec.rpartition("=")
        provider, colon, harness = pair.partition(":")
        if not sep or not colon or not provider or not harness:
            raise _usage(f"--harness-budget {spec!r}: name it as PROVIDER:HARNESS=USD",
                         next_step="office onboard --harness <h> --json lists the pairs to answer")
        try:
            usd = float(value)
        except ValueError:
            usd = math.nan
        if not math.isfinite(usd) or not 0 <= usd <= MAX_MONTHLY_USD:
            raise _usage(f"--harness-budget {spec!r}: USD must be a number from zero to {MAX_MONTHLY_USD:g}")
        if harness not in (overlap.get(provider) or {}).get("harnesses", []):
            raise _usage(f"--harness-budget {spec!r}: {harness} does not run an overlapping {provider} model here",
                         next_step="office onboard --harness <h> --json lists the pairs to answer")
        budgets.setdefault(provider, {})[harness] = usd
    return budgets


def harness_preference_state(overlap: dict, seen: list[str], budgets: dict) -> dict:
    """The round's state for status: what is due, what is asked, and the recommendation once every budget is known."""
    recorded = set(seen)
    pending = sorted({k.rsplit("|", 1)[-1] for k in overlap_keys(overlap) if not _covered(k, recorded)})
    inputs = [f"{p}:{h}" for p, info in overlap.items() for h in info["harnesses"]]
    budgets = {p: row for p, row in budgets.items() if isinstance(row, dict)}
    stored = {p: {h: usd for h in info["harnesses"] if (usd := _usd((budgets.get(p) or {}).get(h))) is not None}
              for p, info in overlap.items()}
    weights, reasoning = derive_weights(overlap, stored) if overlap and not _missing_budgets(overlap, stored) else ({}, [])
    return {"due": bool(pending), "pending": pending, "recorded": sorted(seen),
            "overlap": [{"provider": p, **info} for p, info in overlap.items()],
            "budget_inputs": inputs, "budgets": stored, "recommended": weights or None, "reasoning": reasoning,
            "options": [OFFICE, KEEP, RECOMMENDED]}


# ------------------------------------------------------------------ commands

def status(*, harness: str | None = None, cwd: Path | None = None) -> Result:
    """What /auto-office needs to decide whether to onboard and how: never prompts."""
    completed = completed_schema()
    auth = hermes_access.auth_status()
    overlap = harness_overlap(auth)
    hp = harness_preference_state(overlap, _recorded(), _stored_budgets())
    reasons = [r for r in (due_reason(completed), _harness_reason(hp)) if r]
    reason = "; ".join(reasons) or None
    name, source = current_harness(harness)
    integration = install.integration_status(name) if name else {
        "state": "unknown", "reason": "the current harness was not identified (pass --harness)", "items": []}
    offer = list(HOOK_ACTIONS) if integration["state"] in ("missing", "partial", "stale") else []
    con = db.connect()
    try:
        qs = questions(con, cwd)
    finally:
        con.close()
    commands = {"apply": "office onboard --planner <answer> --executor <answer> --reviewer <answer>",
                "skip": "office onboard --skip",
                "enable_free_models": "office onboard --enable-free-models",
                "disable_free_models": "office onboard --disable-free-models",
                "harness_preference": "office onboard --harness-preference office|keep|recommended "
                                      "[--harness-budget PROVIDER:HARNESS=USD ...]"}
    if offer and name:
        commands["install_recommended"] = f"office install --only {name}"
        commands["install_items"] = f"office install --only {name} --item <id> [--item <id> ...]"
    free_models = {"enabled": free_models_enabled(), "default": False, "options": ["enable", "disable"]}
    data = {"schema_version": SCHEMA_VERSION, "completed_schema": completed, "due": reason is not None,
            "reason": reason or "current", "user_config": str(paths.user_config_path()),
            "harness": {"current": name, "source": source, "integration": integration, "offer": offer},
            "harnesses": harnesses(auth), "questions": qs, "free_models": free_models,
            "harness_preference": hp, "disclaimer": DISCLAIMER, "commands": commands,
            "answers": "office (Let Office decide), keep, or harness/model@effort"}
    lines = [f"onboarding: {'due' if reason else 'current'} (schema {SCHEMA_VERSION}, completed {completed})"
             + (f": {reason}" if reason else "")]
    lines.append(f"current harness: {name or 'unknown'} ({source}); Office integration: {integration['state']}"
                 + (f" ({integration['reason']})" if integration.get("reason") else ""))
    for item in integration.get("items") or []:
        if item["state"] != "current":
            lines.append(f"  {item['id']}: {item['state']}: {item['change']}")
    lines.append("harnesses: " + ", ".join(
        f"{h['harness']} " + (f"{h['version']} sign-in {h['auth']}" if h["installed"] else "not installed")
        for h in data["harnesses"]))
    for row in data["harnesses"]:
        if row["harness"] == "hermes" and row["installed"]:
            lines.append("hermes providers: " + (", ".join(f"{p['provider']} {p['status']}" for p in row["providers"])
                                                  or "none listed"))
    lines.append(f"free models: {'enabled' if free_models['enabled'] else 'disabled'} "
                 "(default off; use --enable-free-models to opt in)")
    for q in qs:
        cur = q["current"]
        now = (OFFICE if cur["choice"] == OFFICE else cur["value"] if cur["choice"] == "route"
               else ", ".join(f"{r}={v}" for r, v in cur["value"].items()))
        lines.append(f"{q['title']} (--{q['id']}): now {now}"
                     + (f"; repo override {cur['repo_override']}" if cur["repo_override"] else ""))
        lines.append(f"  eligible: {', '.join(q['eligible_grouped']) or 'none here'}")
    for o in hp["overlap"]:
        lines.append(f"harness overlap ({o['provider']}): {', '.join(o['models'])} run on {' and '.join(o['harnesses'])}")
    for r in hp["reasoning"]:
        lines.append(f"  {r}")
    lines.append(f"note: {DISCLAIMER}")
    steps = []
    if completed < SCHEMA_VERSION:
        steps.append("ask the three questions (and the integration choice, if offered), then "
                     f"{commands['apply']}, or {commands['skip']}")
    if hp["due"]:
        steps.append("ask the harness-preference question (monthly USD per overlapping provider and harness), then "
                     f"{commands['harness_preference']}")
    nxt = ("; ".join(steps) if steps
           else "onboarding is current; continue the /auto-office request (office onboard --planner ... reconfigures)")
    return Result(lines=lines, next=nxt, data=data)


def _harness_reason(hp: dict) -> str | None:
    if not hp["due"]:
        return None
    pairs = "; ".join(f"{o['provider']} ({' and '.join(o['harnesses'])})" for o in hp["overlap"])
    return f"harness overlap not yet answered: {', '.join(hp['pending'])} share models on {pairs}"


def apply(answers: dict[str, str | None], *, skip: bool = False, enable_free_models: bool | None = None,
          harness_answer: str | None = None, harness_budgets: list[str] | None = None,
          cwd: Path | None = None) -> Result:
    """Write the accepted answers and the completed schema in one atomic user-file
    write. Skip writes only the markers. Any invalid answer writes nothing.

    The harness preference round is not a schema question: `harness_answer` (office, keep or recommended)
    settles the harnesses that overlap, and writes nothing for the schema. It records the overlapping
    harnesses as seen, so they are not asked again. `recommended` needs a monthly USD budget per
    overlapping provider and harness."""
    given = {k: v for k, v in answers.items() if v is not None}
    if harness_answer is not None and harness_answer not in HARNESS_ANSWERS:
        raise _usage(f"--harness-preference {harness_answer!r}: use office, keep or recommended",
                     next_step="office onboard --harness <h> --json")
    if harness_budgets and harness_answer != RECOMMENDED:
        raise _usage("--harness-budget is used by --harness-preference recommended only",
                     next_step="office onboard --harness-preference recommended --harness-budget PROVIDER:HARNESS=USD")
    if skip and (given or enable_free_models is not None or harness_answer is not None):
        raise _usage("--skip keeps every preference; it takes no answers", next_step="office onboard --skip")
    if not skip and not given and enable_free_models is None and harness_answer is None:
        raise _usage("name at least one answer, or --skip",
                     next_step="office onboard --planner <answer> --executor <answer> --reviewer <answer>")
    changes: list[tuple[str, object]] = []
    lines: list[str] = []
    repo_notes: list[str] = []
    if given:
        con = db.connect()
        try:
            qs = {q["id"]: q for q in questions(con, cwd)}
        finally:
            con.close()
        prefs = configcmd.role_preferences([r for _, _, roles, _ in QUESTIONS for r in roles], cwd)
        for qid, title, roles, _ in QUESTIONS:
            answer = (given.get(qid) or KEEP).strip()
            entry = validate_answer(qs[qid], answer)
            for role in roles:
                key = f"roles.{role}.preferred_seed"
                if answer == OFFICE and prefs[role]["user"] is not None:
                    changes.append((key, None))
                elif entry is not None and prefs[role]["user"] != [entry]:
                    changes.append((key, [dict(entry)]))  # a copy per role: no YAML anchors in the file
                if answer != KEEP and prefs[role]["repo"] is not None:
                    repo_notes.append(f"{role} ({_show_seed(prefs[role]['repo'])})")
            lines.append(f"{title}: " + {OFFICE: "Let Office decide", KEEP: "kept"}.get(answer, answer))
    if enable_free_models is not None:
        if free_models_enabled() is not enable_free_models:
            changes.append((FREE_MODELS_KEY, enable_free_models))
        lines.append(f"Free models: {'enabled' if enable_free_models else 'disabled'}")
    harness_lines, harness_changes = _apply_harness(harness_answer, harness_budgets)
    lines += harness_lines
    changes += harness_changes
    if skip:
        changes += _seen_changes(harness_overlap(hermes_access.auth_status()))
    schema_answered = skip or bool(given) or enable_free_models is not None
    if schema_answered and completed_schema() != SCHEMA_VERSION:
        changes.append((SCHEMA_KEY, SCHEMA_VERSION))
    if changes:
        written = configcmd.apply_changes("user", changes, cwd)
    else:
        written = Result(data={"changed": []})
    heading = ("onboarding skipped: preferences unchanged" if skip
               else "onboarding complete" if schema_answered else "harness preference saved")
    res = Result(lines=[heading + (f" (schema {SCHEMA_VERSION})" if schema_answered else ""), *lines],
                 data={"schema_version": SCHEMA_VERSION, "skipped": skip,
                       "free_models_enabled": free_models_enabled(),
                       "harness_preference": harness_answer,
                       "changed": written.data.get("changed", []), "path": str(paths.user_config_path())})
    res.notices += written.notices
    if harness_answer == RECOMMENDED:
        res.notices.append(f"note: {DISCLAIMER}")
    if repo_notes:
        res.notices.append("note: this repository's .auto-office/config.yaml still overrides "
                           + ", ".join(repo_notes) + " here (office config --repo --unset <key> removes it)")
    if not skip and any(a not in (OFFICE, KEEP) for a in given.values()):
        res.notices.append(f"note: {DISCLAIMER}")
    res.next = ("continue the /auto-office request; new runs pick this up and running runs keep their pinned policy "
                "(office config --run <id> --apply-routing re-pins one)")
    return res
