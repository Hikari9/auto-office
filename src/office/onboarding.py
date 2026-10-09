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

import os
import shutil
import subprocess
import sys
from pathlib import Path

from office import adapters, candidates, configcmd, db, discovery, install, paths, routing
from office import config as cfg
from office.result import Result
from office.state import OfficeError

# Bump only when the questions change enough that existing users should see them again.
SCHEMA_VERSION = 1
SCHEMA_KEY = "onboarding.schema_version"
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
    if os.environ.get("GEMINI_CLI") == "1":
        return "gemini", "GEMINI_CLI"
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


def _files_or_env(files: list[Path], env: tuple[str, ...], hint: str) -> tuple[bool, str]:
    if any(os.environ.get(k) for k in env):
        return True, "environment"
    for f in files:
        if f.is_file():
            return True, str(f)
    return False, f"no {' or '.join(str(f) for f in files)} ({hint})"


def credentials(harness: str) -> tuple[str, str]:
    """`found`, `missing` or `unknown`, with where it looked. A binary on PATH
    is not a sign-in: this checks the credential each harness stores, without
    reading any secret. OFFICE_AUTH_FIXTURE ({"claude": "found", ...}) replaces
    the check in tests."""
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
    elif harness == "gemini":
        ok, where = _files_or_env([gemini_home / "oauth_creds.json"], ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                                  "sign in to gemini")
    else:
        return "unknown", "Office has no sign-in check for this harness"
    return ("found" if ok else "missing"), where


def harnesses() -> list[dict]:
    """Every adapter that can launch an Office role: installed, version, sign-in."""
    out = []
    for aid, a in sorted(adapters.load_all().items()):
        if not a.get("office_profiles"):
            continue
        row = {"harness": aid, "installed": adapters.installed(a)}
        if row["installed"]:
            row["version"] = adapters.harness_version(a) or "unknown"
            row["auth"], row["auth_detail"] = credentials(aid)
        out.append(row)
    return out


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
    for c in built:
        state, where = credentials(c["harness"])
        if state == "missing":
            why_not[route_text(c)] = f"{c['harness']} is not signed in: {where}"
            continue
        usable.append(c)
    policy = candidates.role_policy(config, role)
    result = routing.route({
        "role": role, "adaptive": False, "candidates": usable, "runs_db": str(paths.runs_db()),
        "policy": {"required_capabilities": list(policy.get("required_capabilities") or []),
                   "floor": policy.get("floor")},
    })
    rejected = {r["candidate"]: r["reason"] for r in result.get("rejected") or [] if (r.get("stage") or 99) <= 5}
    eligible = []
    for c in usable:
        reason = rejected.get(routing.candidate_id(c))
        if reason:
            if "trust" in reason:
                reason += f" (only you promote trust: office approve trust {routing.candidate_id(c)} --quote \"<words>\")"
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


# ------------------------------------------------------------------ commands

def status(*, harness: str | None = None, cwd: Path | None = None) -> Result:
    """What /auto-office needs to decide whether to onboard and how: never prompts."""
    completed = completed_schema()
    reason = due_reason(completed)
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
                "skip": "office onboard --skip"}
    if offer and name:
        commands["install_recommended"] = f"office install --only {name}"
        commands["install_items"] = f"office install --only {name} --item <id> [--item <id> ...]"
    data = {"schema_version": SCHEMA_VERSION, "completed_schema": completed, "due": reason is not None,
            "reason": reason or "current", "user_config": str(paths.user_config_path()),
            "harness": {"current": name, "source": source, "integration": integration, "offer": offer},
            "harnesses": harnesses(), "questions": qs, "disclaimer": DISCLAIMER, "commands": commands,
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
    for q in qs:
        cur = q["current"]
        now = (OFFICE if cur["choice"] == OFFICE else cur["value"] if cur["choice"] == "route"
               else ", ".join(f"{r}={v}" for r, v in cur["value"].items()))
        lines.append(f"{q['title']} (--{q['id']}): now {now}"
                     + (f"; repo override {cur['repo_override']}" if cur["repo_override"] else ""))
        lines.append(f"  eligible: {', '.join(q['eligible_grouped']) or 'none here'}")
    lines.append(f"note: {DISCLAIMER}")
    if reason:
        nxt = ("ask the three questions (and the integration choice, if offered), then "
               f"{commands['apply']}, or {commands['skip']}")
    else:
        nxt = "onboarding is current; continue the /auto-office request (office onboard --planner ... reconfigures)"
    return Result(lines=lines, next=nxt, data=data)


def apply(answers: dict[str, str | None], *, skip: bool = False, cwd: Path | None = None) -> Result:
    """Write the accepted answers and the completed schema in one atomic user-file
    write. Skip writes only the marker. Any invalid answer writes nothing."""
    given = {k: v for k, v in answers.items() if v is not None}
    if skip and given:
        raise _usage("--skip keeps every preference; it takes no answers", next_step="office onboard --skip")
    if not skip and not given:
        raise _usage("name at least one answer, or --skip",
                     next_step="office onboard --planner <answer> --executor <answer> --reviewer <answer>")
    changes: list[tuple[str, object]] = []
    lines: list[str] = []
    repo_notes: list[str] = []
    if not skip:
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
    if completed_schema() != SCHEMA_VERSION:
        changes.append((SCHEMA_KEY, SCHEMA_VERSION))
    if changes:
        written = configcmd.apply_changes("user", changes, cwd)
    else:
        written = Result(data={"changed": []})
    res = Result(lines=[("onboarding skipped: preferences unchanged" if skip else "onboarding complete")
                        + f" (schema {SCHEMA_VERSION})", *lines],
                 data={"schema_version": SCHEMA_VERSION, "skipped": skip,
                       "changed": written.data.get("changed", []), "path": str(paths.user_config_path())})
    res.notices += written.notices
    if repo_notes:
        res.notices.append("note: this repository's .auto-office/config.yaml still overrides "
                           + ", ".join(repo_notes) + " here (office config --repo --unset <key> removes it)")
    if not skip and any(a not in (OFFICE, KEEP) for a in given.values()):
        res.notices.append(f"note: {DISCLAIMER}")
    res.next = ("continue the /auto-office request; new runs pick this up and running runs keep their pinned policy "
                "(office config --run <id> --apply-routing re-pins one)")
    return res
