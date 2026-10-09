"""PLAN.md: the plan a planner authors, parsed into tasks.

The plan is content, not bookkeeping. It is Markdown with a few `key: value`
lines per task so a planner never writes JSON:

    ## Requirements
    done:
    - a user can reset their password
    blast_radius: repo
    non_goals:
    - no SSO changes
    actions:
    - deploy preview | preconditions: tests pass
    checks: pytest -q
    end_state: e2e
    deploy_prod: vercel deploy --prod
    deploy_verify: curl -fsS https://example.test/health

    ## Tasks
    ### T1: Reset endpoint
    scope: src/auth/**, tests/auth/**
    shared: src/auth/gateManifest.ts
    depends: none
    checks: pytest -q tests/auth
    accept:
    - POST /reset returns 202 for a known email
    visual: none
    route: codex/gpt-6-luna@high, agy/gemini-3.8-flash@medium
    route_why: mechanical rename; the plan pins every edit
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from office import task_descriptors

TASK_HEADING = re.compile(r"^###\s+(T\d+)\s*[:.\-–]\s*(.+?)\s*$")
SECTION = re.compile(r"^##\s+(.+?)\s*$")
KEYVAL = re.compile(r"^([A-Za-z_][A-Za-z_ ]*?)\s*:\s*(.*)$")
BLAST = ("local", "repo", "production", "production-data")
# How far a run goes after its PRs (3.2): stop and ask, preview deploy only,
# merge only, or merge + prod deploy end to end.
END_STATES = ("ask", "preview", "merge", "e2e")
# A scope entry this prefix marks (written as `shared: <paths>` on a task) is an
# append-only registry several tasks may edit (an auth gate manifest, an
# endpoint list, a mock table). Two shared entries never overlap: leases and
# plan review do not serialize them, and the compose step merges the appends.
SHARED = "+"
LIST_KEYS = {"accept", "done", "non_goals", "non-goals", "actions", "checks", "notes", "interfaces", "questions"}
VISUAL_KEYS = {"url", "start", "reference", "viewports", "states", "selectors", "strict", "affects", "ready", "auth",
               "deviations"}


# office writes the plan diagram into PLAN.md between these markers; the block
# is output, never plan content, so it is stripped before parsing and hashing.
DIAGRAM_BEGIN = "<!-- office:diagram"
DIAGRAM_END = "<!-- /office:diagram -->"
_DIAGRAM_BLOCK = re.compile(r"\n*<!-- office:diagram.*?<!-- /office:diagram -->\n*", re.S)


def strip_generated(text: str) -> str:
    return _DIAGRAM_BLOCK.sub("\n", text).rstrip("\n") + "\n" if DIAGRAM_BEGIN in text else text


@dataclass
class ParsedPlan:
    requirements: dict = field(default_factory=dict)
    tasks: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    run_checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Structured scope problems, so a revision can keep what the accepted plan already had:
    # (task id, entry, error text) and (task a, task b, shared entry, error text).
    entry_problems: list[tuple] = field(default_factory=list)
    pair_problems: list[tuple] = field(default_factory=list)


def _is_no_check(value: str) -> bool:
    return value.strip().lower() in ("none", "n/a")


def _split_list(value: str) -> list[str]:
    value = value.strip()
    if not value or value.lower() in ("none", "-", "n/a"):
        return []
    return [v.strip() for v in re.split(r"[,;]", value) if v.strip()]


def parse(text: str) -> ParsedPlan:
    text = strip_generated(text)
    plan = ParsedPlan()
    section = None
    task: dict | None = None
    list_key: str | None = None
    in_visual = False
    req: dict = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("<!--"):
            continue
        m = SECTION.match(line)
        if m and not line.startswith("###"):
            section = m.group(1).strip().lower()
            task, list_key, in_visual = None, None, False
            continue
        m = TASK_HEADING.match(line)
        if m:
            task = {"id": m.group(1), "title": m.group(2), "scope": [], "depends": [], "interfaces": [],
                    "accept": [], "checks": None, "visual": None, "descriptor": {}, "notes": [], "line": lineno}
            plan.tasks.append(task)
            section = "tasks"
            list_key, in_visual = None, False
            continue
        stripped = line.strip()
        indented = raw.startswith((" ", "\t"))
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip()
            if section == "questions":
                plan.questions.append(item)
            elif task is not None and list_key:
                _append(task, list_key, item)
            elif task is None and section == "requirements" and list_key:
                req.setdefault(list_key, []).append(item)
            continue
        m = KEYVAL.match(stripped)
        if not m:
            if task is not None:
                task["notes"].append(stripped)
            continue
        key, value = m.group(1).strip().lower().replace(" ", "_"), m.group(2).strip()
        if task is not None and in_visual and indented and key in VISUAL_KEYS:
            task["visual"][key] = value
            continue
        in_visual = False
        if task is not None:
            list_key = key if (not value and key in LIST_KEYS) else None
            if key == "scope":
                task["scope"] = _split_list(value) + [s for s in task["scope"] if is_shared(s)]
                task["scope_none"] = _is_no_check(value)  # `none`: no file scope (a comment or issue edit)
            elif key == "shared":
                task["scope"] += [SHARED + p.lstrip(SHARED) for p in _split_list(value)]
            elif key == "depends":
                task["depends"] = _split_list(value)
            elif key == "interfaces":
                task["interfaces"] = _split_list(value)
                list_key = "interfaces" if not value else None
            elif key == "checks":
                if _is_no_check(value):
                    task["checks"] = []
                elif value:
                    task["checks"] = [value]
                else:
                    task["checks"] = []
            elif key == "accept":
                if value:
                    task["accept"].append(value)
            elif key == "visual":
                if value.lower() in ("none", "n/a", "no"):
                    task["visual"] = {"none": True}
                else:
                    task["visual"] = {"url": value} if value else {}
                    in_visual = True
            elif key in task_descriptors.PLAN_KEYS:
                try:
                    task["descriptor"][key] = task_descriptors.parse_field(key, value)
                except ValueError as exc:
                    plan.errors.append(f"{task['id']} (line {lineno}): {exc}")
            elif key == "route":
                # The planner's executor route: primary, then fallbacks (#300).
                task["route"] = _split_list(value)
            elif key == "route_why":
                task["route_why"] = value
            elif key == "lane":
                # #337: tasks naming one lane converge together.
                task["lane"] = value.strip()
            elif key == "converge":
                # #337: lanes naming one shared boundary get one more review together.
                task["converge"] = _split_list(value)
            elif key == "notes":
                if value:
                    task["notes"].append(value)
            else:
                task["notes"].append(f"{key}: {value}")
            continue
        if section == "requirements":
            list_key = key if not value else None
            if value:
                if key == "checks":
                    if not _is_no_check(value):
                        plan.run_checks.append(value)
                else:
                    req[key] = value
            continue
    plan.requirements = _requirements(req, plan)
    for t in plan.tasks:
        if t["visual"] == {}:
            t["visual"] = None
            plan.warnings.append(f"{t['id']}: empty visual block ignored")
    _validate(plan)
    return plan


def _append(task: dict, key: str, item: str) -> None:
    if key == "checks":
        if task["checks"] is None:
            task["checks"] = []
        task["checks"].append(item)
    elif key in ("accept",):
        task["accept"].append(item)
    elif key == "interfaces":
        task["interfaces"].append(item)
    else:
        task["notes"].append(item)


def _requirements(req: dict, plan: ParsedPlan) -> dict:
    actions = []
    for entry in req.get("actions", []) if isinstance(req.get("actions"), list) else []:
        action, _, pre = entry.partition("|")
        pre = pre.split(":", 1)[1] if ":" in pre else pre
        actions.append({"action": action.strip(), "preconditions": [p.strip() for p in pre.split(";") if p.strip()]})
    checks = req.get("checks") if isinstance(req.get("checks"), list) else []
    plan.run_checks.extend(c for c in checks if not _is_no_check(c))
    return {
        "goal": req.get("goal"),
        "done_criteria": req.get("done") if isinstance(req.get("done"), list) else [],
        "blast_radius": req.get("blast_radius"),
        "non_goals": (req.get("non_goals") or req.get("non-goals") or []),
        "named_actions": actions,
        "end_state": req.get("end_state"),
        "deploy": {k: req[f"deploy_{k}"] for k in ("preview", "prod", "verify") if req.get(f"deploy_{k}")},
    }


def _validate(plan: ParsedPlan) -> None:
    ids = [t["id"] for t in plan.tasks]
    if not plan.tasks:
        plan.errors.append("no tasks: add `### T1: <title>` blocks under `## Tasks`")
    dup = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dup):
        plan.errors.append(f"{d}: duplicate task id")
    known = set(ids)
    for t in plan.tasks:
        if not t["scope"] and not t.get("scope_none"):
            plan.errors.append(f"{t['id']} (line {t['line']}): missing `scope:` (paths this task may write)")
        for entry in t["scope"]:
            problem = _entry_problem(entry)
            if problem:
                # Each entry is matched as one path or glob: a note after it never matches (#416).
                err = f"{t['id']} (line {t['line']}): {ENTRY_ERROR} {entry!r} {problem}"
                plan.errors.append(err)
                plan.entry_problems.append((t["id"], entry, err))
        if not t["accept"]:
            plan.errors.append(f"{t['id']} (line {t['line']}): missing `accept:` criteria")
        if t["checks"] is None:
            plan.errors.append(f"{t['id']} (line {t['line']}): missing `checks:` (a command, or `none` for no deterministic check)")
        for dep in t["depends"]:
            if dep not in known:
                plan.errors.append(f"{t['id']}: depends on unknown task {dep}")
            if dep == t["id"]:
                plan.errors.append(f"{t['id']}: depends on itself")
        if t["visual"] is not None and not t["visual"].get("none") and not t["visual"].get("url"):
            plan.errors.append(f"{t['id']}: visual block needs `url:`")
    if _has_cycle({t["id"]: t["depends"] for t in plan.tasks}):
        plan.errors.append("task dependencies contain a cycle")
    r = plan.requirements
    if r.get("blast_radius") and r["blast_radius"] not in BLAST:
        plan.errors.append(f"requirements blast_radius must be one of {', '.join(BLAST)}")
    end = r.get("end_state")
    if end and end not in END_STATES:
        plan.errors.append(f"requirements end_state must be one of {', '.join(END_STATES)}")
    for a, b, entry in _unordered_shared_trees(plan.tasks):
        err = (f"{a} and {b} may run in parallel but share the directory {entry.lstrip(SHARED)!r}; a shared directory "
               f"is allowed only for tasks ordered by `depends` (add `depends: {a}` to {b}, or share files)")
        plan.errors.append(err)
        plan.pair_problems.append((a, b, entry, err))
    for a, b in _parallel_overlaps(plan.tasks):
        plan.warnings.append(f"{a} and {b} may run in parallel but their scopes overlap; leases will serialize them")


def end_state_problems(end: str | None, deploy: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for an end state and its deploy commands, checked on
    the merged requirements (start flags plus the plan)."""
    errors, warnings = [], []
    if end == "preview" and not deploy.get("preview"):
        errors.append("end_state preview needs `deploy_preview: <command>`")
    if end == "e2e" and not deploy.get("prod"):
        errors.append("end_state e2e needs `deploy_prod: <command>`")
    if end in ("preview", "e2e") and not deploy.get("verify"):
        warnings.append("no `deploy_verify:` command; the deploy is recorded without verification")
    return errors, warnings


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        for m in graph.get(n, []):
            if m in graph and visit(m):
                return True
        state[n] = 2
        return False

    return any(visit(n) for n in graph)


def ancestors(graph: dict[str, list[str]], node: str) -> set[str]:
    seen, stack = set(), list(graph.get(node, []))
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(graph.get(n, []))
    return seen


def dependants(graph: dict[str, list[str]], node: str) -> set[str]:
    out = set()
    changed = True
    while changed:
        changed = False
        for n, deps in graph.items():
            if n not in out and (node in deps or out.intersection(deps)):
                out.add(n)
                changed = True
    return out


def is_shared(pattern: str) -> bool:
    return pattern.startswith(SHARED)


ENTRY_ERROR = "scope/shared entry"
# Whitespace, backticks and a colon mean prose rode along with the path
# (`x.csv (append-only: ...)`). Parentheses and brackets alone are path
# characters (Next.js route groups `(app)`, dynamic segments `[slug]`).
_NOT_PATH = re.compile(r"[\s`:]")


def _entry_problem(entry: str) -> str | None:
    """Why a scope or shared entry is not one path or glob, or None."""
    bare = entry.lstrip(SHARED)
    if not bare or _NOT_PATH.search(bare) or bare.count("(") != bare.count(")") or bare.count("[") != bare.count("]") \
            or re.search(r"[^/][(\[]", bare):
        # Brackets are path characters only as whole segments (`(group)/`, `[slug]/`);
        # `x.csv(A3)` is a note glued to a path.
        return ("is not a path or glob; list bare paths or globs (no notes) and put limits "
                "(e.g. 'only the importer entry') in `accept:`")
    return None


def shared_tree(entry: str) -> bool:
    """A `shared:` entry naming a directory rather than registry files: a trailing `/`, a
    `**`, or a last segment with no file suffix (`src/reg`, `src/*`). A file glob such as
    `locales/*.json` names append-only files and stays parallel-safe."""
    bare = entry.lstrip(SHARED)
    last = bare.rstrip("/").rsplit("/", 1)[-1]
    # A root-level suffixless name (`Makefile`, `Dockerfile`) is a file, not a directory.
    return is_shared(entry) and (bare.endswith("/") or "**" in bare
                                 or ("." not in last and "/" in bare.rstrip("/")))


def _unordered_shared_trees(tasks: list[dict]):
    """(a, b, entry) for each pair of tasks that could run at once while one shares a
    directory the other also lists or overlaps. A shared file is append-only and may be
    edited in parallel; a shared directory may be shared only by tasks a `depends` path
    orders, so its editors never run together."""
    graph = {t["id"]: t["depends"] for t in tasks}
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a["id"] in ancestors(graph, b["id"]) or b["id"] in ancestors(graph, a["id"]):
                continue
            for x, y in ((a, b), (b, a)):
                hit = next((e for e in x["scope"] if shared_tree(e) and scopes_overlap([e], y["scope"])), None)
                if hit:
                    yield a["id"], b["id"], hit
                    break


def grandfather_entries(parsed: "ParsedPlan", prev_tasks: list[dict] | None) -> None:
    """A revision of an accepted plan keeps an entry the earlier version already
    had: its entry error becomes a warning, so a run accepted before entry
    validation can still be amended. New or changed entries stay errors."""
    prev = {t["id"]: set(t.get("scope") or []) for t in prev_tasks or []}
    old = {err for tid, entry, err in parsed.entry_problems if entry in prev.get(tid, ())}
    try:
        before = {frozenset((a, b)) for a, b, _ in _unordered_shared_trees(
            [{"id": t["id"], "depends": t.get("depends") or [], "scope": t.get("scope") or []} for t in prev_tasks or []])}
    except (KeyError, TypeError):
        before = set()
    old |= {err for a, b, _, err in parsed.pair_problems if frozenset((a, b)) in before}
    for err in [e for e in parsed.errors if e in old]:
        parsed.warnings.append(err + " (kept: the accepted plan already had it)")
    parsed.errors[:] = [e for e in parsed.errors if e not in old]


def literal_prefix(pattern: str) -> str:
    m = re.search(r"[*?]", pattern)  # brackets are path characters in scope entries
    return pattern[: m.start()] if m else pattern


def scopes_overlap(a: list[str], b: list[str]) -> bool:
    for pa in a:
        for pb in b:
            if is_shared(pa) and is_shared(pb) and not (shared_tree(pa) or shared_tree(pb)):
                continue  # both append to a shared registry file; compose resolves it
            la, lb = literal_prefix(pa.lstrip(SHARED)), literal_prefix(pb.lstrip(SHARED))
            if la.startswith(lb) or lb.startswith(la):
                return True
    return False


def _parallel_overlaps(tasks: list[dict]):
    graph = {t["id"]: t["depends"] for t in tasks}
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            ordered = a["id"] in ancestors(graph, b["id"]) or b["id"] in ancestors(graph, a["id"])
            if not ordered and scopes_overlap(a["scope"], b["scope"]):
                yield a["id"], b["id"]


def path_in_scope(path: str, scope: list[str]) -> bool:
    import fnmatch
    for raw in scope:
        pattern = raw.lstrip(SHARED)
        if pattern.endswith("/**"):
            if path == pattern[:-3] or path.startswith(pattern[:-2]):
                return True
        # Brackets are path characters (`[slug]`), never a glob class.
        literal = re.sub(r"[\[\]]", lambda m: "[[]" if m.group() == "[" else "[]]", pattern)
        if fnmatch.fnmatchcase(path, literal) or path == pattern:
            return True
        # A directory entry (`src/auth/`, `src/auth`, `src/app/[slug]/`) owns everything under it,
        # compared literally so brackets are path characters, not a glob class (#334). A shared
        # directory is valid only for tasks ordered by `depends` (plan validation), and it
        # counts in the overlap check, so its tasks never hold leases together.
        if pattern and not re.search(r"[*?]", pattern) \
                and path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False
