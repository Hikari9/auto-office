#!/usr/bin/env python3
"""Deterministic helpers for the Auto Office v3 skill bundle.

Route-time commands never perform network access. Catalog fetching/normalization should happen
outside routing and be committed to a content-addressed local snapshot before selection.
"""
from __future__ import annotations
import argparse, contextlib, hashlib, io, json, math, os, re, shlex, sqlite3, subprocess, sys, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
CANON_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
MUTABLE_TRUST_ROLES = {"executor", "code_reviewer", "browser_verifier", "closeout_verifier"}
ATTRIBUTIONS = {"model","harness","adapter","quota/account","environment/network","planner","brief","repository","verification","unknown"}
OUTCOMES = {"pending","verified_no_observed_failure","recurrence_failure","revert_failure","material_post_merge_defect","abandoned","environment_failure"}
PHASE_ORDER = ("intake", "planned", "approved", "executing", "reviewed", "closed")
REUSE_COMPACT_THRESHOLD = 272_000
REUSE_COMPACT_ROLES = ("executor", "plan_reviewer")
START_PINNED_FIELDS = (
    "run_id",
    "family_id",
    "base_sha",
    "policy_hash",
    "catalog_snapshot_hash",
    "adapter_snapshot_hash",
    "effective_config_hash",
    "plugin_commit",
)


def load_data(path: str | Path) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def dump_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_obj(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_with_schema(data: Any, schema_name: str) -> list[str]:
    schema = json.loads((SCHEMAS / schema_name).read_text())
    validator = Draft202012Validator(schema)
    errors = []
    for e in sorted(validator.iter_errors(data), key=lambda x: list(x.path)):
        loc = ".".join(str(x) for x in e.path) or "$"
        errors.append(f"{loc}: {e.message}")
    return errors


def cmd_validate_packet(args):
    data = load_data(args.file)
    schema = "execution-packet.schema.json" if args.kind == "execution" else "run-envelope.schema.json"
    errors = validate_with_schema(data, schema)
    if args.kind == "execution":
        errors.extend(_packet_semantic_errors(data))
    dump_json({"valid": not errors, "errors": errors})
    return 0 if not errors else 2


def _packet_semantic_errors(data):
    """Checks the JSON shape cannot express.

    ASKING A READ-ONLY DISPATCH TO WRITE A FILE (incident 2026-09-15, run
    e6167374): a code reviewer was dispatched `--sandbox read-only` with a
    brief ending "write your findings to /tmp/office/review-findings.md and
    reply with only that path". It reviewed correctly for ~16 minutes at xhigh
    effort, then could not deliver: the write was refused, and it burned
    further turns trying TextEdit, Terminal, an IDE and a browser as write
    fallbacks before giving up. The findings -- four real defects -- were
    recovered only because the orchestrator went and read the pane. Nothing in
    the packet was malformed; the brief simply asked for an output channel the
    sandbox forbade.

    The packet already declares `allowed_mutations`. A delivery path outside
    it is the same contradiction the validator exists to catch, so catch it
    before dispatch rather than after the reasoning budget is spent.
    """
    errors = []
    delivery = (data.get("output") or {}).get("delivery")
    mutations = data.get("allowed_mutations")
    if delivery == "file":
        if mutations in (None, [], "none"):
            errors.append(
                "output.delivery: 'file' contradicts allowed_mutations "
                "(none) -- a dispatch that may not write cannot deliver its "
                "result as a file; use delivery 'reply'"
            )
        elif isinstance(mutations, list):
            target = (data.get("output") or {}).get("path")
            if target and not any(str(target).startswith(str(m)) for m in mutations):
                errors.append(
                    f"output.path {target!r} is outside allowed_mutations "
                    f"{mutations} -- the dispatch cannot write where it is "
                    "told to deliver"
                )
    return errors


def cmd_validate_adapter(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "adapter.schema.json")
    # Semantic checks beyond JSON shape.
    if data.get("verified_state") != "invalid":
        for key in ["version_fingerprint","model_source","effort_mapping","invocation","safe_prompt_passing","quota_probe","agentic_capability"]:
            if not data.get(key):
                errors.append(f"{key}: mandatory semantics missing")
        if data.get("safe_prompt_passing", {}).get("shell") is not False:
            errors.append("safe_prompt_passing.shell: must be false for shipped adapters")
        for k,v in data.get("effort_mapping", {}).items():
            if k not in CANON_EFFORTS:
                errors.append(f"effort_mapping.{k}: non-canonical effort key")
    dump_json({"valid": not errors, "state": data.get("verified_state"), "errors": errors})
    return 0 if not errors else 2


def candidate_id(c):
    return f"{c.get('harness')}@{c.get('harness_version')}/{c.get('model_id')}@{c.get('effort')}"


def _num(v, default=float("inf")):
    return default if v is None else float(v)


def preferred_rank(c, preferred_seed):
    """Index of the first roles.<role>.preferred_seed entry c matches, or None.

    An entry matches on model_id (required) plus effort/harness when the entry
    specifies them, so a config seed of {model_id, effort} without harness
    matches that model/effort on any harness.
    """
    for i, p in enumerate(preferred_seed or []):
        if p.get("model_id") != c.get("model_id"):
            continue
        if p.get("effort") and p.get("effort") != c.get("effort"):
            continue
        if p.get("harness") and p.get("harness") != c.get("harness"):
            continue
        return i
    return None


def invocation_provenance(candidate: dict) -> str:
    """Return the machine-readable provenance state for a candidate's invocation slug.

    States:
      - 'proven': slug proven against a local harness command
      - 'documented': slug documented in authoritative sources but unproven against local harness
      - 'none': no harness invocation slug
    """
    invocation = candidate.get("invocation_model_id")
    if not invocation:
        return "none"
    explicit = candidate.get("invocation_provenance")
    if explicit in ("proven", "documented", "none"):
        return explicit
    source = candidate.get("invocation_source")
    if not source:
        return "proven"
    source = str(source).strip()
    if source.startswith("documented") or source == "unproven":
        return "documented"
    if source.startswith("local-evidence") or source.startswith("proven"):
        return "proven"
    if source.startswith("unverified") or source == "none":
        return "none"
    return "proven"


def selection_disclosure(role: str, chosen: dict, preferred_seed, cost_policy: str) -> dict:
    """Build the durable, user-visible explanation for a selected route."""
    rank = preferred_rank(chosen, preferred_seed)
    reasons = ["cleared the applicable trust, capability, role-floor, and task-shape gates"]
    quota = chosen.get("quota", {})
    if quota.get("status") == "ok" and quota.get("tightest_remaining_percent") is not None:
        reasons.append("fit within the protected quota reserve")
    elif quota.get("status") != "ok" or quota.get("tightest_remaining_percent") is None:
        reasons.append("was selected with quota headroom explicitly unknown")
    if rank is not None:
        reasons.append(f"matched preferred seed #{rank + 1}, which decided the advisory ranking")
    else:
        reasons.append(f"won the {cost_policy} cost and local-evidence comparison")
    invocation = chosen.get("invocation_model_id")
    prov = invocation_provenance(chosen)
    if not invocation or prov == "none":
        # The catalog row carries no harness-specific slug, so the dispatch will
        # be attempted with the canonical model_id. Spec-seed names ("luna") are
        # not harness slugs ("gpt-5.6-luna"), so this fallback is the single
        # largest source of route-time dispatch failures. Say so in the
        # disclosure instead of letting it look like a resolved slug.
        reasons.append("carries no catalog invocation slug, so model_id is being used unverified")
    elif prov == "documented":
        reasons.append("catalog invocation slug is documented but unproven against local harness")
    return {
        "role": role,
        "model_id": chosen.get("model_id"),
        "invocation_model_id": invocation or chosen.get("model_id"),
        "invocation_model_id_source": "catalog" if invocation else "fallback:model_id",
        "invocation_provenance": prov,
        "effort": chosen.get("effort"),
        "harness": chosen.get("harness"),
        "harness_version": chosen.get("harness_version"),
        "triple": candidate_id(chosen),
        "reason": "; ".join(reasons),
    }



def route(request: dict) -> dict:
    """Route a role dispatch request, delegating to scripts.office_routing."""
    try:
        from scripts import office_routing
    except ImportError:
        try:
            # Both parents, because neither is guaranteed to be on sys.path: pytest puts
            # the repo root there, but `python3 tests/test_schemas.py` -- which is exactly
            # how .github/workflows/validate.yml invokes it -- puts only tests/ there, and
            # the shim then degraded to routing_module_unavailable in CI while passing
            # locally under pytest.
            _here = os.path.dirname(os.path.abspath(__file__))
            for _p in (_here, os.path.dirname(_here)):
                if _p not in sys.path:
                    sys.path.insert(0, _p)
            import office_routing
        except ImportError as exc:
            sys.stderr.write(
                f"ERROR: office_routing module unavailable ({exc}). "
                "Task T2B implementation of scripts/office_routing.py is required.\n"
            )
            return {
                "selected": None,
                "status": "routing_module_unavailable",
                "error": str(exc),
                "rejected": [],
            }

    # Hard stop: verify recorded override if unverified override requested
    if request.get("allow_unverified_override") or request.get("allow_override"):
        override_record = request.get("recorded_override")
        db_path = request.get("runs_db")
        if not hasattr(office_routing, "validate_override_record") or not office_routing.validate_override_record(
            override_record, request, db_path=db_path
        ):
            return {
                "selected": None,
                "status": "override_not_authorized",
                "reason": (
                    "Execution requested unverified override without a valid, unexpired "
                    "recorded override authorization in runs.db."
                ),
                "rejected": [],
            }

    return office_routing.route(request)


def cmd_route(args):
    """CLI handler for `office_runtime.py route <request_file>`."""
    req = load_data(args.request)
    req.setdefault("runs_db", str(configured_runs_db(repo_root=Path.cwd())))
    result = route(req)
    if req.get("run_id"):
        try:
            result["routing_decision"] = record_routing_decision(req["runs_db"], req, result)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            dump_json({"error": "routing_decision_record_failed", "message": str(exc), "db": req["runs_db"]})
            return 2
    else:
        result["routing_decision"] = {
            "recorded": False,
            "reason": "missing_run_id",
            "db": req["runs_db"],
        }
    dump_json(result)
    if result.get("status") in (
        "no_qualifying_candidate",
        "protected_quota_would_be_consumed",
        "override_not_authorized",
        "routing_module_unavailable",
    ):
        return 1
    return 0


def cmd_hash(args):
    dump_json({"hash":sha256_obj(load_data(args.file))}); return 0


def maturity_age(points: float) -> float:
    p=max(0.0,float(points))
    age=100.0*(1.0-math.exp(-p/60.0))
    # Preserve the spec invariant that 100 is asymptotic even when exp() underflows.
    return min(age, math.nextafter(100.0, 0.0))


def cmd_maturity(args):
    dump_json({"points":float(args.points),"clamped_points":max(0,float(args.points)),"age":maturity_age(args.points)}); return 0


PRIVACY_PATTERNS = [
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("url", re.compile(r"https?://[^\s)\]}>]+", re.I)),
    ("unix-absolute-path", re.compile(r"(?<![\w.])/(?:Users|home|var|opt|srv|private|Volumes)/[^\s'\"]+")),
    ("windows-absolute-path", re.compile(r"\b[A-Z]:\\(?:[^\s\\]+\\)+[^\s]+", re.I)),
    ("credential-like", re.compile(r"\b(?:token|api[_-]?key|secret|password)\s*[:=]\s*[^\s]{6,}", re.I)),
]

def privacy_findings(text: str, deny_terms=None):
    findings=[]
    for name,rx in PRIVACY_PATTERNS:
        for m in rx.finditer(text): findings.append({"kind":name,"start":m.start(),"sample":m.group(0)[:80]})
    for term in deny_terms or []:
        if term and re.search(re.escape(term), text, re.I): findings.append({"kind":"deny-term","term":term})
    return findings


def cmd_privacy(args):
    text=Path(args.file).read_text(encoding='utf-8')
    deny=[]
    if args.deny_file: deny=[x.strip() for x in Path(args.deny_file).read_text().splitlines() if x.strip()]
    findings=privacy_findings(text,deny)
    dump_json({"valid":not findings,"findings":findings}); return 0 if not findings else 3


def init_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript("""
    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, family_id TEXT, created_at TEXT, plugin_commit TEXT, policy_hash TEXT, catalog_hash TEXT, adapter_hash TEXT, config_hash TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS dispatches(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, holder_id TEXT, triple TEXT, invocation_model_id TEXT, selection_reason TEXT, task_shape TEXT, size_class TEXT, started_at TEXT, ended_at TEXT, money_estimate REAL, money_actual REAL, quota_estimate REAL, quota_delta REAL, wall_clock_seconds REAL, attribution TEXT, outcome TEXT);
    CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, dispatch_id TEXT, reviewer_dispatch_id TEXT, status TEXT, severity TEXT, summary TEXT, evidence_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS validations(id TEXT PRIMARY KEY, dispatch_id TEXT, kind TEXT, command TEXT, passed INTEGER, known_bad_proven INTEGER, evidence_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS routing_decisions(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, request_hash TEXT, selected_triple TEXT, decision_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS artifact_versions(id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, version INTEGER, content_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS ownership_events(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, scope TEXT, prior_holder TEXT, new_holder TEXT, event TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS outcome_labels(id TEXT PRIMARY KEY, dispatch_id TEXT, label TEXT, primary_attribution TEXT, contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT);
    CREATE TABLE IF NOT EXISTS lineage(id TEXT PRIMARY KEY, component_kind TEXT, component_id TEXT, parent_id TEXT, event TEXT, multiplier REAL, created_at TEXT);
    CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, scope TEXT NOT NULL, holder_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT, revoked_at TEXT, revoke_reason TEXT);
    CREATE TABLE IF NOT EXISTS adapter_trust_acts(id TEXT PRIMARY KEY, triple TEXT NOT NULL, target_state TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL, evidence_reference TEXT, recorded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS recorded_overrides(override_id TEXT PRIMARY KEY, run_id TEXT, family_id TEXT, task_id TEXT, role TEXT, candidate_id TEXT, bypass_stage INTEGER, rationale TEXT, authorized_by TEXT, authorized_at TEXT, expires_at TEXT);
    """)
    dispatch_columns = {row[1] for row in con.execute("PRAGMA table_info(dispatches)")}
    if "invocation_model_id" not in dispatch_columns:
        con.execute("ALTER TABLE dispatches ADD COLUMN invocation_model_id TEXT")
    if "selection_reason" not in dispatch_columns:
        con.execute("ALTER TABLE dispatches ADD COLUMN selection_reason TEXT")
    con.commit(); return con


def cmd_init_db(args):
    con=init_db(Path(args.db)); mode=con.execute('PRAGMA journal_mode').fetchone()[0]; con.close(); dump_json({"db":str(Path(args.db)),"journal_mode":mode}); return 0


def record_run(db_path: str | Path, envelope: dict, status: str = "intake") -> dict:
    """Persist the run identity before any dispatch evidence can reference it."""
    con = init_db(Path(db_path))
    try:
        con.execute(
            "INSERT OR IGNORE INTO runs(id, family_id, created_at, plugin_commit, "
            "policy_hash, catalog_hash, adapter_hash, config_hash, status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                envelope["run_id"], envelope.get("family_id"), envelope.get("created_at"),
                envelope.get("plugin_commit"), envelope.get("policy_hash"),
                envelope.get("catalog_snapshot_hash"), envelope.get("adapter_snapshot_hash"),
                envelope.get("effective_config_hash"), status,
            ),
        )
        con.commit()
    finally:
        con.close()
    return {"run_id": envelope["run_id"], "db": str(Path(db_path).resolve()), "status": status}


def record_routing_decision(db_path: str | Path, request: dict, result: dict) -> dict:
    """Append the CLI route decision without making pure route() stateful."""
    run_id = request.get("run_id")
    if not run_id:
        raise ValueError("route request must include run_id to persist routing evidence")
    decision_hash = result.get("decision_hash") or sha256_obj({"request": request, "result": result})
    row_id = str(uuid.uuid4())
    con = init_db(Path(db_path))
    try:
        con.execute(
            "INSERT INTO routing_decisions(id, run_id, role, request_hash, selected_triple, decision_hash, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                row_id, run_id, request.get("role"), sha256_obj(request), result.get("selected"),
                decision_hash, datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
    finally:
        con.close()
    return {"id": row_id, "run_id": run_id, "decision_hash": decision_hash, "db": str(Path(db_path).resolve())}


def cmd_record_dispatch(args):
    data=load_data(args.file)
    if data.get('attribution') and data['attribution'] not in ATTRIBUTIONS: raise SystemExit('invalid attribution')
    if data.get('outcome') and data['outcome'] not in OUTCOMES: raise SystemExit('invalid outcome')
    con=init_db(Path(args.db))
    disclosure=data.get('selection_disclosure') or {}
    data.setdefault('invocation_model_id', disclosure.get('invocation_model_id'))
    data.setdefault('selection_reason', disclosure.get('reason'))
    cols=['id','run_id','role','holder_id','triple','invocation_model_id','selection_reason','task_shape','size_class','started_at','ended_at','money_estimate','money_actual','quota_estimate','quota_delta','wall_clock_seconds','attribution','outcome']
    row=[data.get(k) for k in cols]; row[0]=row[0] or str(uuid.uuid4())
    con.execute(f"INSERT INTO dispatches({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", row); con.commit(); con.close(); dump_json({'recorded':row[0]}); return 0


def cmd_scaffold_adapter(args):
    template=load_data(ROOT/'adapters/templates/adapter.template.yaml'); template['id']=args.id
    out=Path(args.out); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(yaml.safe_dump(template,sort_keys=False),encoding='utf-8')
    dump_json({'created':str(out),'verified_state':'valid-unverified'}); return 0


def cmd_catalog_snapshot(args):
    data=load_data(args.input)
    if not isinstance(data,dict): raise SystemExit('catalog input must be an object')
    for row in data.get('models',[]):
        effort=row.get('effort')
        if effort not in CANON_EFFORTS: raise SystemExit(f"unknown/unmapped effort is not routable: {effort!r}")
    digest=hashlib.sha256(canonical_bytes(data)).hexdigest()
    outdir=Path(args.out_dir).expanduser(); outdir.mkdir(parents=True,exist_ok=True)
    out=outdir/f'{digest}.yaml'
    if not out.exists(): out.write_text(yaml.safe_dump(data,sort_keys=False),encoding='utf-8')
    dump_json({'snapshot':str(out),'catalog_snapshot_hash':'sha256:'+digest,'immutable_existing':out.exists()}); return 0


NON_CONFIGURABLE_KEYS = {"schema_version", "config_precedence", "hard_invariants"}
CONFIG_TIERS = ("plugin_default", "user", "repo", "prompt_cli")


def config_default_path() -> Path:
    return ROOT / "config" / "config.default.yaml"


def configured_runs_db(config: dict | None = None, repo_root: Path | None = None) -> Path:
    """Resolve the one durable recorder used by a real Auto Office run.

    ``AUTO_OFFICE_RUNS_DB`` is an explicit test/embedding override.  Normal runs
    follow the configured ``paths.runs_db`` value so dispatch, validation,
    routing, and trust evidence do not silently split across per-run filenames.
    """
    override = os.environ.get("AUTO_OFFICE_RUNS_DB")
    if override:
        return Path(override).expanduser().resolve()
    cfg = config if config is not None else (load_data(config_default_path()) or {})
    raw = ((cfg.get("paths", {}) or {}).get("runs_db")
           or "~/.local/share/auto-office/runs.db")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (repo_root or Path.cwd()) / path
    return path.resolve()


def state_runs_db(state_dir: str | Path) -> Path:
    """Resolve a run's recorder, preserving compatibility with legacy state dirs."""
    state_path = Path(state_dir) / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            configured = state.get("runs_db")
            if isinstance(configured, str) and configured.strip():
                return Path(configured).expanduser().resolve()
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return (Path(state_dir) / "runs.db").resolve()


def deep_merge(base: Any, over: Any, tier: str, warnings: list, path: str = "") -> Any:
    """Merge `over` onto `base`. Dicts merge recursively; lists and scalars replace.

    A type conflict warns and keeps the lower-precedence value, which is what the spec
    means by "invalid keys warn and fall back to the next lower-precedence tier".
    """
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            loc = f"{path}.{k}" if path else k
            out[k] = deep_merge(base[k], v, tier, warnings, loc) if k in base else v
        return out
    if base is not None and over is not None and not _same_shape(base, over):
        warnings.append({"tier": tier, "key": path, "reason": "type-mismatch-ignored",
                         "expected": type(base).__name__, "got": type(over).__name__})
        return base
    return over


def _same_shape(a: Any, b: Any) -> bool:
    """True when b may replace a. Ints and floats are interchangeable; bools are not."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    return type(a) is type(b)


def resolve_config_tiers(repo_root: Path, overrides_file: str | None, sets: list[str] | None,
                         user_path: str | None = None) -> tuple[dict, list, list]:
    default_p = config_default_path()
    default_cfg = load_data(default_p) or {}
    allowed_top = set(default_cfg) - NON_CONFIGURABLE_KEYS
    paths = default_cfg.get("paths", {}) or {}

    up = Path(user_path).expanduser() if user_path else Path(paths.get("user", "~/.config/auto-office/config.yaml")).expanduser()
    rp = (repo_root / paths.get("repo", ".auto-office/config.yaml")).expanduser()

    layers = [("plugin_default", default_p, default_cfg)]
    for tier, p in (("user", up), ("repo", rp)):
        layers.append((tier, p, (load_data(p) or {}) if p.is_file() else None))

    cli: dict | None = None
    if overrides_file:
        cli = load_data(overrides_file) or {}
    for expr in (sets or []):
        if "=" not in expr:
            raise SystemExit(f"--set expects key.path=value, got {expr!r}")
        k, v = expr.split("=", 1)
        try:
            v = yaml.safe_load(v)
        except yaml.YAMLError:
            pass
        node = cli = (cli if cli is not None else {})
        parts = k.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = v
    layers.append(("prompt_cli", Path(overrides_file) if overrides_file else None, cli))

    warnings: list = []
    effective = default_cfg
    report = []
    for tier, p, data in layers:
        entry = {"tier": tier, "path": str(p) if p else None, "present": data is not None}
        if data is None:
            report.append(entry)
            continue
        if tier != "plugin_default":
            kept = {}
            for k, v in data.items():
                if k == "schema_version":
                    if v != default_cfg.get("schema_version"):
                        warnings.append({"tier": tier, "key": k, "reason": "schema-version-mismatch-ignored",
                                         "expected": default_cfg.get("schema_version"), "got": v})
                elif k in NON_CONFIGURABLE_KEYS:
                    warnings.append({"tier": tier, "key": k, "reason": "not-configurable-ignored"})
                elif k not in allowed_top:
                    warnings.append({"tier": tier, "key": k, "reason": "unknown-key-ignored"})
                else:
                    kept[k] = v
            data = kept
            effective = deep_merge(effective, data, tier, warnings)
        entry["applied_keys"] = sorted(data)
        report.append(entry)
    return effective, report, warnings


def cmd_effective_config(args):
    effective, tiers, warnings = resolve_config_tiers(
        Path(args.repo_root).expanduser().resolve(), args.overrides, args.set, args.user)
    digest = sha256_obj(effective)
    if args.hash_only:
        print(digest)
        return 0
    dump_json({"effective_config_hash": digest, "tiers": tiers,
               "warnings": warnings, "config": effective})
    return 0


def cmd_proposal_id(args):
    obj={'stream':args.stream,'kind':args.kind,'payload':load_data(args.file) if args.file else args.text}
    dump_json({'identity_hash':sha256_obj(obj)}); return 0


def cmd_replay(args):
    dataset=load_data(args.dataset); oldp=load_data(args.old_policy); newp=load_data(args.new_policy)
    rows=dataset.get('rows',dataset if isinstance(dataset,list) else [])
    flips=[]; decisions=[]
    for i,row in enumerate(rows):
        req=dict(row); req['policy']=oldp; old=route(req)
        req2=dict(row); req2['policy']=newp; new=route(req2)
        rec={'row':i,'old':old.get('selected'),'new':new.get('selected'),'old_status':old.get('status'),'new_status':new.get('status')}
        decisions.append(rec)
        if rec['old']!=rec['new'] or rec['old_status']!=rec['new_status']: flips.append(rec)
    dump_json({'rows':len(rows),'flips':flips,'decisions':decisions,'zero_flip_warning':len(rows)>0 and not flips}); return 0


def _new_run_envelope(args):
    now=datetime.now(timezone.utc).isoformat()
    envelope = {'run_id':getattr(args,'run_id',None) or str(uuid.uuid4()),'family_id':args.family_id,'dispatch_id':str(uuid.uuid4()),'role':'orchestrator','holder_id':args.holder_id,'triple':args.triple,'mode':args.gear,'playbook':args.playbook,'base_sha':args.base_sha,'policy_hash':args.policy_hash,'catalog_snapshot_hash':args.catalog_hash,'adapter_snapshot_hash':args.adapter_hash,'effective_config_hash':args.config_hash,'plan_version':1,'packet_version':1,'created_at':now}
    if getattr(args, "plugin_commit", None):
        envelope["plugin_commit"] = args.plugin_commit
    return envelope


def cmd_new_run(args):
    obj=_new_run_envelope(args)
    errors=validate_with_schema(obj,'run-envelope.schema.json')
    if errors: dump_json({'valid':False,'errors':errors}); return 2
    out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(obj,indent=2)+'\n')
    dump_json({'created':str(out),'run_id':obj['run_id']}); return 0


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _start_effective_config(repo: Path) -> tuple[dict, str]:
    captured = io.StringIO()
    call_args = argparse.Namespace(repo_root=str(repo), overrides=None, set=None, user=None, hash_only=False)
    with contextlib.redirect_stdout(captured):
        result = cmd_effective_config(call_args)
    if result != 0:
        raise RuntimeError("effective-config failed")
    data = json.loads(captured.getvalue())
    return data["config"], data["effective_config_hash"]


def _start_catalog_hash() -> str:
    return sha256_obj(load_data(ROOT / "catalog" / "seed.yaml"))


def _start_adapter_hash() -> str:
    adapter_dir = ROOT / "adapters" / "seed"
    data = {str(path.relative_to(ROOT)): load_data(path) for path in sorted(adapter_dir.glob("*.yaml"))}
    return sha256_obj(data)


def _start_plugin_commit() -> str:
    configured = os.environ.get("AUTO_OFFICE_PLUGIN_COMMIT")
    if configured:
        return configured
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True,
                                capture_output=True, text=True)
        commit = result.stdout.strip()
        if commit:
            return commit
    except (OSError, subprocess.CalledProcessError):
        pass
    # Installed plugin bundles may not carry a .git directory.  A content hash
    # still pins the exact loaded plugin snapshot in that environment.
    return sha256_file(ROOT / "SKILL.md")


def _start_policy_hash() -> str:
    # This is the policy artifact's byte hash, deliberately independent from
    # the canonical hash of the fully merged effective configuration.
    return sha256_file(ROOT / "config" / "config.default.yaml")


def _start_base_sha(repo: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _start_repo_root(repo: Path) -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(repo), check=True,
                            capture_output=True, text=True)
    root = result.stdout.strip()
    if not root:
        raise RuntimeError("git rev-parse --show-toplevel returned an empty path")
    return Path(root).expanduser().resolve()


def _start_state_root() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return root.expanduser().resolve() / "auto-office" / "runs"


def _runs_tmp_dir() -> Path:
    tmp = _start_state_root() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def _start_holder() -> tuple[str, str]:
    holder_id = os.environ.get("AUTO_OFFICE_HOLDER_ID") or "orchestrator"
    triple = os.environ.get("AUTO_OFFICE_HOLDER_TRIPLE") or "codex@local/orchestrator@none"
    return holder_id, triple


def _resolve_risk(args, config: dict) -> dict:
    """Deterministic risk classification from explicit `start` inputs.

    Absence is never risk. A caller who passes neither `--blast-radius` nor
    `--size-class` gets `high: False` — this function widens uncertainty by
    refusing to guess, not by defaulting to safe. It never inspects the repo
    itself; that stays evidence the planner gathers and can use to *raise*
    blast_radius/size_class on a later `amend`, never something this function
    infers on its own from a diff.
    """
    signals = (config.get("risk_signals") or {})
    high_blast = set(signals.get("high_blast_radius") or ["production", "production-data"])
    high_size = set(signals.get("high_size_class") or ["L", "XL"])
    blast_radius = getattr(args, "blast_radius", None)
    size_class = getattr(args, "size_class", None)
    irreversible = bool(getattr(args, "irreversible", False))
    high = irreversible or (blast_radius in high_blast) or (size_class in high_size)
    return {"blast_radius": blast_radius, "size_class": size_class, "irreversible": irreversible, "high": high}


def _start_gear(args, risk: dict | None = None) -> str:
    if args.gear:
        return args.gear
    if getattr(args, "irreversible", False):
        return "full"
    base = "express" if sum(bool(v) for v in (args.volume, args.interview, args.adversarial)) >= 2 else "direct"
    # issue-66 (runsheet.favor.church#66 self-improve): the fit test used to stop here,
    # deciding gear from three opt-in CLI flags alone and never from blast radius or size
    # class despite skills/auto-planning and docs/v3-acceptance.md both describing it as
    # risk-evaluating. A size-M, multi-surface, production-facing change with none of the
    # three flags set landed on `direct` -- the cheapest gear -- purely because nobody
    # opted a boolean in. `direct`/`direct+review`/`light`/`quick` never fund plan_review
    # on their own (skills/auto-review/SKILL.md); only escalating past them gives a
    # default-routed run any chance at a plan reviewer without the user asking ad hoc.
    if risk and risk.get("high") and base in ("direct", "direct+review", "light", "quick"):
        return "express"
    return base


def resolve_gates(gear: str, risk_high: bool, config: dict) -> dict:
    """Turn a gear preset row plus a risk verdict into concrete gate decisions.

    `risk_forced` was, before this change, a token that appeared exactly once in
    config.default.yaml with no definition anywhere in code, protocol, or skills --
    every gate resolution was left to an LLM orchestrator's unguided interpretation
    of the bare word. This is the definition: `risk_forced` resolves to `risk_high`.
    `True`/`False` pass through unchanged. Any other string (`shallow`,
    `cost_bounded`, `policy_optional`, `acceptance_forced`) is returned as-is --
    those already carry established advisory meaning elsewhere and are out of this
    fix's scope.
    """
    presets = (config.get("gear_presets") or {})
    preset = presets.get(gear, {})
    ad_hoc_cap = config.get("ad_hoc_review_max_rounds")

    def resolve_value(value):
        if value == "risk_forced":
            return bool(risk_high)
        return value

    plan_review = resolve_value(preset.get("plan_review", False))
    code_review = resolve_value(preset.get("independent_code_review", False))
    plan_rounds = preset.get("plan_review_max_rounds")
    code_rounds = preset.get("code_review_max_rounds")
    # A gate that only fired because risk forced it (the preset's own row funds
    # nothing) draws the ad-hoc round budget, not an unbounded loop -- same rule
    # skills/auto-review/SKILL.md already states in prose for a user-requested
    # plan review on top of a gear with no budget of its own.
    if plan_review and plan_rounds is None:
        plan_rounds = ad_hoc_cap
    if code_review and code_rounds is None:
        code_rounds = ad_hoc_cap

    return {
        "plan_review": plan_review,
        "independent_code_review": code_review,
        "funded_browser_verification": resolve_value(preset.get("funded_browser_verification", False)),
        "plan_review_max_rounds": plan_rounds,
        "code_review_max_rounds": code_rounds,
    }


_PLAN_REVIEW_ROUND_VERDICTS = {"PLAN_DEFECT", "PLAN DEFECT"}


def plan_review_round_authorized(last_verdict: str) -> bool:
    """Does a prior plan-review verdict authorize spending another round?

    Only `PLAN DEFECT` does. It contradicts an assumption the plan was built on, which
    is what actually needs a fresh independent look once fixed -- not another pass over
    the same, still-valid plan. `CHANGES REQUIRED` is the producer's to fix without
    sending the plan back to the reviewer, and `ACCEPTED` ends review outright; treating
    either as grounds for a second round is exactly the overengineering the round-cap
    config exists to bound, not something the cap should spend budget accommodating.
    `plan_review_max_rounds` counts PLAN-DEFECT-triggered re-reviews only -- the
    practical default for a normal review, with no defect, stays one round regardless
    of the configured ceiling. An unrecognized verdict string is never authorization;
    only a named, accepted PLAN DEFECT is.
    """
    normalized = (last_verdict or "").strip().upper()
    return normalized in _PLAN_REVIEW_ROUND_VERDICTS


def _ensure_repo_gitignore(repo: Path) -> None:
    path = repo / ".gitignore"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if any(line.strip() == ".office/" for line in text.splitlines()):
            return
        with path.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write(".office/\n")
        return
    path.write_text(".office/\n", encoding="utf-8")


def cmd_start(args):
    try:
        repo = Path(args.repo or ".").expanduser().resolve()
        if not repo.is_dir():
            dump_json({"error": "invalid_repo", "repo": str(repo)})
            return 2
        repo = _start_repo_root(repo)
        config, config_hash = _start_effective_config(repo)
        risk = _resolve_risk(args, config)
        gear = _start_gear(args, risk)
        gates = resolve_gates(gear, risk["high"], config)
        base_sha = _start_base_sha(repo)
        catalog_hash = _start_catalog_hash()
        adapter_hash = _start_adapter_hash()
        plugin_commit = _start_plugin_commit()
        policy_hash = _start_policy_hash()
        holder_id, triple = _start_holder()
        run_id = str(uuid.uuid4())
        family_id = str(uuid.uuid4())
        envelope_args = argparse.Namespace(family_id=family_id, holder_id=holder_id, triple=triple,
            gear=gear, playbook=args.playbook, base_sha=base_sha, plugin_commit=plugin_commit,
            policy_hash=policy_hash, catalog_hash=catalog_hash, adapter_hash=adapter_hash,
            config_hash=config_hash, run_id=run_id)
        envelope = _new_run_envelope(envelope_args)
        envelope["goal"] = args.goal
        envelope["repo_root"] = str(repo)
        errors = validate_with_schema(envelope, "run-envelope.schema.json")
        if errors:
            dump_json({"valid": False, "errors": errors})
            return 2
        state_dir = _start_state_root() / run_id
        runs_db = configured_runs_db(config, repo)
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = state_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _runs_tmp_dir()
        state = {"run_id": run_id, "family_id": family_id, "phase": "intake", "plan_version": 1,
                 "packet_version": 1, "updated_at": envelope["created_at"], "goal": args.goal,
                 "playbook": args.playbook, "gear": gear, "holder_id": holder_id, "triple": triple,
                 "base_sha": base_sha, "plugin_commit": plugin_commit, "policy_hash": policy_hash,
                 "catalog_snapshot_hash": catalog_hash, "adapter_snapshot_hash": adapter_hash,
                 "effective_config_hash": config_hash, "runs_db": str(runs_db), "repo_root": str(repo),
                 "tmp_dir": str(tmp_dir.resolve()), "risk": risk, "gates": gates}
        _atomic_write_json(state_dir / "state.json", state)
        _atomic_write_json(state_dir / "envelope.json", envelope)
        # Create and register the configured recorder before returning a usable run.
        try:
            record_run(runs_db, envelope, status="intake")
        except Exception as exc:
            dump_json({"error": "recorder_init_failed", "message": str(exc), "db": str(runs_db)})
            return 2
        # issue-77 kickoff registers family state before execution landings exist
        # (schema comment, family-registry.schema.json). Never fatal to `start`: family
        # registration is supplementary durable bookkeeping, not part of this command's
        # own long-established contract.
        try:
            session_id = os.environ.get("AUTO_OFFICE_SESSION_ID") or run_id
            _office_family().register_family(state_dir, session_id, family_id, str(repo), 0)
        except Exception:
            pass
        pointer = repo / ".office" / "runs" / f"{run_id}.ref"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(state_dir.resolve()) + "\n", encoding="utf-8")
        _ensure_repo_gitignore(repo)
        gate_note = ""
        if risk["high"] and gates["plan_review"]:
            gate_note = "\nPlan review: FUNDED (risk-forced)"
        kickoff = (f"Auto Office kickoff\nGoal: {args.goal}\nPlaybook: {args.playbook}\n"
                   f"Gear: {gear}\nRun: {run_id}\nBase SHA: {base_sha}{gate_note}")
        dump_json({"state_dir": str(state_dir.resolve()), "runs_db": str(runs_db), "run_id": run_id, "gear": gear,
                   "pointer": str(pointer.resolve()), "kickoff": kickoff, "tmp_dir": str(tmp_dir.resolve()),
                   "risk": risk, "gates": gates})
        return 0
    except Exception as exc:
        dump_json({"error": "start_failed", "message": str(exc)})
        return 2


def _plan_file_hash(path: str | None) -> str | None:
    if not path:
        return None
    return "sha256:" + hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()


def cmd_plan_review_round_authorized(args):
    try:
        authorized = plan_review_round_authorized(args.verdict)
        dump_json({"verdict": args.verdict, "authorized": authorized})
        return 0
    except Exception as exc:
        dump_json({"error": "plan_review_round_authorized_failed", "message": str(exc)})
        return 2


def cmd_resolve_gates(args):
    try:
        state_dir = Path(args.state_dir).expanduser()
        state_path = state_dir / "state.json"
        if not state_path.exists():
            dump_json({"error": "no_state", "state_dir": str(state_dir)})
            return 2
        state = json.loads(state_path.read_text(encoding="utf-8"))
        repo = Path(state.get("repo_root") or ".").expanduser().resolve()
        config, _ = _start_effective_config(repo)
        risk_args = argparse.Namespace(
            blast_radius=getattr(args, "blast_radius", None) or (state.get("risk") or {}).get("blast_radius"),
            size_class=getattr(args, "size_class", None) or (state.get("risk") or {}).get("size_class"),
            irreversible=getattr(args, "irreversible", False) or bool((state.get("risk") or {}).get("irreversible")),
        )
        risk = _resolve_risk(risk_args, config)
        gear = state.get("gear")
        gates = resolve_gates(gear, risk["high"], config)
        # Re-resolution never writes state.json on its own -- a widened blast_radius/size_class
        # after intake is a PLAN DEFECT/amendment event with its own owner, not a side effect of
        # asking what the gates currently say. The caller decides whether to persist it.
        dump_json({"gear": gear, "risk": risk, "gates": gates})
        return 0
    except Exception as exc:
        dump_json({"error": "resolve_gates_failed", "message": str(exc)})
        return 2


FROZEN_INTENT_FIELDS = ("goal", "done_criteria", "blast_radius", "named_actions", "non_goals")
BLAST_RADIUS_VALUES = ("local", "repo", "production", "production-data")


def _frozen_intent_errors(intent):
    """Type/value errors for the five frozen fields; empty list when valid."""
    errors = []
    if not isinstance(intent.get("goal"), str) or not intent["goal"].strip():
        errors.append("goal must be a non-empty string")
    done = intent.get("done_criteria")
    if not isinstance(done, list) or not done or not all(isinstance(x, str) and x.strip() for x in done):
        errors.append("done_criteria must be a non-empty list of non-empty strings")
    if intent.get("blast_radius") not in BLAST_RADIUS_VALUES:
        errors.append("blast_radius must be one of " + ", ".join(BLAST_RADIUS_VALUES))
    non_goals = intent.get("non_goals")
    if not isinstance(non_goals, list) or not all(isinstance(x, str) and x.strip() for x in non_goals):
        errors.append("non_goals must be a list of non-empty strings")
    actions = intent.get("named_actions")
    if not isinstance(actions, list):
        errors.append("named_actions must be a list")
    else:
        # An irreversible step is only a receipt when its preconditions are written
        # out exactly (auto-intake), so an entry must name both the action and them.
        for index, entry in enumerate(actions):
            ok = (isinstance(entry, dict)
                  and isinstance(entry.get("action"), str) and entry["action"].strip()
                  and isinstance(entry.get("preconditions"), list) and entry["preconditions"]
                  and all(isinstance(x, str) and x.strip() for x in entry["preconditions"]))
            if not ok:
                errors.append(f"named_actions[{index}] must be an object with a non-empty 'action' "
                              "and a non-empty 'preconditions' list of non-empty strings")
    return errors


def cmd_freeze_intent(args):
    """Planner freeze: validate the five execution fields, re-derive risk and gates
    from the frozen blast_radius, and move intake -> planned in one write, so a
    plan that widens risk can never reach approve-plan with start-time gates."""
    try:
        state_path = Path(args.state_dir).expanduser() / "state.json"
        if not state_path.exists():
            dump_json({"error": "no_state", "state_dir": str(Path(args.state_dir).expanduser())})
            return 2
        state = json.loads(state_path.read_text(encoding="utf-8"))
        intent = json.loads(Path(args.intent).expanduser().read_text(encoding="utf-8"))
        if not isinstance(intent, dict):
            dump_json({"error": "invalid_intent", "errors": ["intent must be a JSON object"]})
            return 2
        missing = [k for k in FROZEN_INTENT_FIELDS if k not in intent]
        if missing:
            dump_json({"error": "missing_fields", "missing": missing})
            return 2
        errors = _frozen_intent_errors(intent)
        if errors:
            dump_json({"error": "invalid_intent", "errors": errors})
            return 2
        frozen = {k: intent[k] for k in FROZEN_INTENT_FIELDS}
        phase = state.get("phase")
        refreeze = phase == "planned" and state.get("frozen_intent") == frozen
        if phase != "intake" and not refreeze:
            dump_json({"error": "invalid_phase", "phase": phase, "expected": "intake"})
            return 2
        repo = Path(state.get("repo_root") or ".").expanduser().resolve()
        config, _ = _start_effective_config(repo)
        prior = state.get("risk") or {}
        risk = _resolve_risk(argparse.Namespace(
            blast_radius=frozen["blast_radius"],
            size_class=prior.get("size_class"),
            irreversible=bool(prior.get("irreversible")),
        ), config)
        gates = resolve_gates(state.get("gear"), risk["high"], config)
        gates_changed = gates != state.get("gates")
        # A same-intent refreeze is a no-op only when risk and gates already agree;
        # otherwise it repairs them (e.g. state frozen by an older runtime).
        if refreeze and not gates_changed and risk == state.get("risk"):
            dump_json({"frozen": True, "idempotent": True, "phase": phase})
            return 0
        now = datetime.now(timezone.utc).isoformat()
        state["frozen_intent"] = frozen
        state["risk"] = risk
        state["gates"] = gates
        state["phase"] = "planned"
        state["updated_at"] = now
        _atomic_write_json(state_path, state)
        dump_json({"frozen": True, "idempotent": False, "repaired": refreeze, "phase": "planned",
                   "frozen_intent": frozen, "risk": risk, "gates": gates,
                   "gates_changed": gates_changed})
        return 0
    except Exception as exc:
        dump_json({"error": "freeze_intent_failed", "message": str(exc)})
        return 2


def cmd_approve_plan(args):
    try:
        state_path = Path(args.state_dir).expanduser() / "state.json"
        if not state_path.exists():
            dump_json({"error": "no_state", "state_dir": str(Path(args.state_dir).expanduser())})
            return 2
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not args.quote.strip():
            dump_json({"error": "empty_quote", "message": "--quote must not be empty or whitespace-only"})
            return 2
        phase = state.get("phase")
        plan_version = state.get("plan_version", 1)
        if phase == "approved" and isinstance(state.get("approval"), dict):
            approval = state["approval"]
            if approval.get("plan_version") == plan_version:
                dump_json({"approved": True, "idempotent": True, "phase": phase,
                           "plan_version": plan_version})
                return 0
            dump_json({"error": "plan_version_mismatch", "phase": phase,
                       "approved_plan_version": approval.get("plan_version"),
                       "current_plan_version": plan_version})
            return 2
        if phase != "planned":
            dump_json({"error": "invalid_phase", "phase": phase, "expected": "planned"})
            return 2
        approval = {"by": args.approved_by, "quote": args.quote,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "plan_version": plan_version,
                    "packet_version": state.get("packet_version", 1),
                    "plan_sha": _plan_file_hash(args.plan_path)}
        state["phase"] = "approved"
        state["approval"] = approval
        state["updated_at"] = approval["at"]
        _atomic_write_json(state_path, state)
        dump_json({"approved": True, "idempotent": False, "phase": "approved",
                   "plan_version": plan_version, "approval": approval})
        return 0
    except Exception as exc:
        dump_json({"error": "approve_plan_failed", "message": str(exc)})
        return 2


def cmd_lease_acquire(args):
    now=datetime.now(timezone.utc); now_str=now.isoformat(); expires=(now+timedelta(seconds=args.ttl)).isoformat()
    con=init_db(Path(args.db))
    cur=con.execute("SELECT id, holder_id, expires_at FROM leases WHERE run_id=? AND scope=? AND released_at IS NULL AND revoked_at IS NULL ORDER BY acquired_at DESC LIMIT 1", (args.run_id, args.scope)).fetchone()
    if cur:
        lid, h, exp = cur
        if datetime.fromisoformat(exp) <= now:
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='stale' WHERE id=?", (now_str, lid))
            con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), args.run_id, args.role, args.scope, h, args.holder_id, 'revoke_and_acquire', now_str))
            prior = h
        else:
            if h != args.holder_id:
                dump_json({"acquired":False,"holder_id":h,"expires_at":exp}); return 1
            else:
                con.execute("UPDATE leases SET expires_at=? WHERE id=?", (expires, lid))
                con.commit(); con.close(); dump_json({"acquired":True,"lease_id":lid,"expires_at":expires,"prior_holder":h}); return 0
    else:
        prior = None
        con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), args.run_id, args.role, args.scope, None, args.holder_id, 'acquire', now_str))
    new_id = str(uuid.uuid4())
    con.execute("INSERT INTO leases(id, run_id, role, scope, holder_id, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id, args.run_id, args.role, args.scope, args.holder_id, now_str, expires))
    con.commit(); con.close(); dump_json({"acquired":True,"lease_id":new_id,"expires_at":expires,"prior_holder":prior}); return 0

def cmd_lease_renew(args):
    now=datetime.now(timezone.utc); expires=(now+timedelta(seconds=args.ttl)).isoformat()
    con=init_db(Path(args.db))
    cur=con.execute("SELECT holder_id FROM leases WHERE id=? AND released_at IS NULL AND revoked_at IS NULL", (args.lease_id,)).fetchone()
    if not cur or cur[0] != args.holder_id: dump_json({"renewed":False}); return 1
    con.execute("UPDATE leases SET expires_at=? WHERE id=?", (expires, args.lease_id))
    con.commit(); con.close(); dump_json({"renewed":True,"expires_at":expires}); return 0

def cmd_lease_release(args):
    now=datetime.now(timezone.utc).isoformat()
    con=init_db(Path(args.db)); row = con.execute("SELECT run_id, role, scope, holder_id FROM leases WHERE id=?", (args.lease_id,)).fetchone()
    if row:
        con.execute("UPDATE leases SET released_at=? WHERE id=? AND holder_id=?", (now, args.lease_id, args.holder_id))
        con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), row[0], row[1], row[2], args.holder_id, None, 'release', now))
    con.commit(); con.close(); dump_json({"released":True}); return 0

def cmd_lease_check(args):
    now=datetime.now(timezone.utc)
    con=init_db(Path(args.db)); cur=con.execute("SELECT holder_id, expires_at FROM leases WHERE run_id=? AND scope=? AND released_at IS NULL AND revoked_at IS NULL ORDER BY acquired_at DESC LIMIT 1", (args.run_id, args.scope)).fetchone(); con.close()
    if cur:
        stale = datetime.fromisoformat(cur[1]) <= now
        dump_json({"active":not stale,"holder_id":cur[0],"expires_at":cur[1],"stale":stale}); return 0
    dump_json({"active":False}); return 0

def _canonical_state_error(state_dir: Path, state: dict) -> dict | None:
    required_fields = START_PINNED_FIELDS + ("repo_root", "plan_version", "packet_version")
    missing = [key for key in required_fields if key not in state]
    if missing:
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "missing pinned fields", "missing": missing}

    envelope_path = state_dir / "envelope.json"
    if not envelope_path.exists():
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "missing envelope.json"}
    try:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "invalid envelope.json", "detail": str(exc)}
    if not isinstance(envelope, dict):
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "envelope.json must contain a JSON object"}

    envelope_errors = validate_with_schema(envelope, "run-envelope.schema.json")
    if envelope_errors:
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "envelope schema validation failed", "details": envelope_errors}

    mismatches = {
        key: {"state": state.get(key), "envelope": envelope.get(key)}
        for key in START_PINNED_FIELDS + ("repo_root",)
        if state.get(key) != envelope.get(key)
    }
    if mismatches:
        return {"error": "state envelope mismatch", "state_path": str(state_dir / "state.json"),
                "mismatches": mismatches}

    repo_root = Path(str(state["repo_root"])).expanduser().resolve()
    pointer = repo_root / ".office" / "runs" / f"{state['run_id']}.ref"
    if not pointer.is_file():
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "missing run pointer", "pointer": str(pointer)}
    try:
        pointer_target_text = pointer.read_text(encoding="utf-8").strip()
        pointer_target = Path(pointer_target_text).expanduser()
        if not pointer_target.is_absolute():
            pointer_target = pointer.parent / pointer_target
        pointer_target = pointer_target.resolve()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": "invalid_start_receipt", "state_path": str(state_dir / "state.json"),
                "reason": "invalid run pointer", "pointer": str(pointer), "detail": str(exc)}
    if pointer_target != state_dir.resolve():
        return {"error": "state pointer mismatch", "state_path": str(state_dir / "state.json"),
                "pointer": str(pointer), "pointer_target": str(pointer_target),
                "expected_target": str(state_dir.resolve())}
    return None


def _approval_version_error(state: dict) -> dict | None:
    approval = state.get("approval")
    if approval is None:
        if state.get("phase") in PHASE_ORDER[PHASE_ORDER.index("approved"):]:
            return {"error": "approval_required", "phase": state.get("phase"),
                    "message": "approved lifecycle state must contain a recorded approval"}
        return None
    if not isinstance(approval, dict) or approval.get("plan_version") != state.get("plan_version"):
        return {"error": "approval_version_mismatch", "phase": state.get("phase"),
                "approved_plan_version": approval.get("plan_version") if isinstance(approval, dict) else None,
                "current_plan_version": state.get("plan_version")}
    if "packet_version" in approval and approval.get("packet_version") != state.get("packet_version"):
        return {"error": "approval_version_mismatch", "phase": state.get("phase"),
                "approved_packet_version": approval.get("packet_version"),
                "current_packet_version": state.get("packet_version")}
    return None


def cmd_state_save(args):
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    if not state_path.exists():
        dump_json({"error": "no_state", "state_dir": str(state_dir),
                   "message": "run start before saving lifecycle state"})
        return 2
    try:
        obj = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A malformed state may contain an interrupted write or belong to a
        # run we cannot safely identify.  Never recover by overwriting it
        # implicitly: preserve the evidence and require explicit repair.
        dump_json({"error": "invalid state.json", "reason": "malformed JSON",
                   "state_path": str(state_path), "detail": str(exc)})
        return 2
    if not isinstance(obj, dict):
        dump_json({"error": "invalid state.json",
                   "reason": "state.json must contain a JSON object",
                   "state_path": str(state_path),
                   "json_type": type(obj).__name__})
        return 2
    mismatches = {
        key: {"existing": obj[key], "incoming": getattr(args, key)}
        for key in ("run_id", "family_id")
        if key in obj and obj[key] != getattr(args, key)
    }
    if mismatches:
        dump_json({"error": "state identity mismatch",
                   "state_path": str(state_path), "mismatches": mismatches})
        return 2
    receipt_error = _canonical_state_error(state_dir, obj)
    if receipt_error:
        dump_json(receipt_error)
        return 2
    if args.phase == "approved":
        dump_json({"error": "approval_required", "phase": obj.get("phase"),
                   "message": "only approve-plan may transition a run to approved"})
        return 2
    if args.phase not in PHASE_ORDER:
        dump_json({"error": "invalid_phase", "phase": args.phase,
                   "expected": list(PHASE_ORDER)})
        return 2
    current_phase = obj.get("phase")
    if current_phase not in PHASE_ORDER:
        dump_json({"error": "invalid_phase", "phase": current_phase,
                   "expected": list(PHASE_ORDER)})
        return 2
    approval_error = _approval_version_error(obj)
    if approval_error:
        dump_json(approval_error)
        return 2
    current_index = PHASE_ORDER.index(current_phase)
    requested_index = PHASE_ORDER.index(args.phase)
    if requested_index != current_index and requested_index != current_index + 1:
        expected = current_phase if current_index == len(PHASE_ORDER) - 1 else PHASE_ORDER[current_index + 1]
        dump_json({"error": "invalid_phase_transition", "from": current_phase,
                   "to": args.phase, "expected": expected,
                   "message": "lifecycle transitions are monotonic and adjacent"})
        return 2
    current_plan_version = obj["plan_version"]
    current_packet_version = obj["packet_version"]
    plan_version = current_plan_version if args.plan_version is None else args.plan_version
    packet_version = current_packet_version if args.packet_version is None else args.packet_version
    if current_index >= PHASE_ORDER.index("approved") and (
        plan_version != current_plan_version or packet_version != current_packet_version
    ):
        dump_json({"error": "version_change_after_approval", "phase": current_phase,
                   "current_plan_version": current_plan_version,
                   "current_packet_version": current_packet_version,
                   "requested_plan_version": plan_version,
                   "requested_packet_version": packet_version})
        return 2
    obj.update({"run_id": args.run_id, "family_id": args.family_id, "phase": args.phase,
                "plan_version": plan_version, "packet_version": packet_version,
                "updated_at": datetime.now(timezone.utc).isoformat()})
    if args.dispatches: obj["dispatches"] = json.loads(args.dispatches)
    if args.findings: obj["findings"] = json.loads(args.findings)
    if args.lease: obj["lease"] = json.loads(args.lease)
    _atomic_write_json(state_path, obj)
    hash_obj = {k: v for k, v in obj.items() if k != "updated_at"}
    dump_json({"saved": str(state_path), "content_hash": sha256_obj(hash_obj)}); return 0

def cmd_state_load(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists(): dump_json({"exists": False}); return 0
    dump_json(json.loads(state_path.read_text(encoding="utf-8"))); return 0

SPOKE_DIGEST_PREFIX_LEN = 16


def spoke_skill_path(spoke):
    """On-disk SKILL.md for a spoke, resolved relative to this script."""
    return Path(__file__).resolve().parents[1] / "skills" / spoke / "SKILL.md"


def spoke_digest(spoke):
    """Short sha256 of the spoke's SKILL.md as it exists right now.

    Short, not full: this value is transcribed by hand into a CLI call, and a
    64-char hex string invites copy-paste of a remembered value from an earlier
    run. 16 hex chars is 64 bits -- far beyond collision risk for a handful of
    files, and short enough to read off a terminal.
    """
    path = spoke_skill_path(spoke)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:SPOKE_DIGEST_PREFIX_LEN]


def cmd_mark_spoke(args):
    """Record that a spoke was loaded, requiring proof it was located first.

    WHY THIS IS NOT A BARE FLAG (incident 2026-09-15, run e6167374): the
    previous implementation recorded a timestamp from a spoke NAME alone. That
    made the receipt self-attesting -- the same agent the gate constrains could
    satisfy the gate by typing the spoke's name, with no coupling whatsoever to
    having loaded its content. It happened exactly that way: an orchestrator
    batch-marked `auto-routing` and `auto-execution` in a single shell call as
    a convenience, having loaded neither, and `check-spoke` then returned 0 for
    both. The drift was caught by the USER reading the transcript, which is the
    one detector this runtime is supposed to make unnecessary.

    `--digest` does NOT prove the agent comprehended the spoke; nothing a CLI
    can check does. What it proves is that the caller located this specific
    file at its current version. That is enough to convert the failure mode
    from an accident anyone can have into a deliberate act: you can no longer
    mark a spoke you never found, mark a spoke whose name you guessed, mark a
    renamed or stale spoke, or batch-mark five spokes in one keystroke.

    A mark without a verified digest is still permitted (`--unverified`), so an
    unusual layout cannot hard-block a run -- but it is recorded as such, and
    auto-closeout surfaces it. Fail-soft and visible beats fail-closed and
    routed around.
    """
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists():
        dump_json({"error": "no state.json; run new-run/state-save first"}); return 1

    expected = spoke_digest(args.spoke)
    verified = False

    if args.digest:
        if expected is None:
            dump_json({
                "error": "spoke_not_found",
                "spoke": args.spoke,
                "looked_in": str(spoke_skill_path(args.spoke)),
                "hint": "a --digest was supplied for a spoke that does not exist on disk; "
                        "check the spoke name against skills/",
            })
            return 2
        if args.digest.strip().lower() != expected:
            dump_json({
                "error": "digest_mismatch",
                "spoke": args.spoke,
                "supplied": args.digest.strip().lower(),
                "hint": "the digest does not match the spoke's SKILL.md as it exists now. "
                        "Either the spoke changed since you read it, or this digest came "
                        "from a different spoke or an earlier run. Re-read the spoke and "
                        "recompute.",
            })
            return 2
        verified = True
    elif not args.unverified:
        dump_json({
            "error": "digest_required",
            "spoke": args.spoke,
            "skill_path": str(spoke_skill_path(args.spoke)),
            "hint": "mark-spoke now requires --digest <short-sha256 of that spoke's "
                    "SKILL.md>, so a receipt cannot be produced for a spoke that was "
                    "never located. Pass --unverified only when the spoke genuinely is "
                    "not on disk; closeout will report it.",
        })
        return 2

    obj = json.loads(state_path.read_text(encoding="utf-8"))
    spokes = obj.setdefault("spokes_loaded", {})
    spokes[args.spoke] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "verified": verified,
        "digest": expected if verified else None,
    }
    _atomic_write_json(state_path, obj)
    dump_json({"marked": args.spoke, "verified": verified, "phase": obj.get("phase")})
    return 0


def cmd_spoke_digest(args):
    """Print the digest to pass to `mark-spoke --digest` for a spoke."""
    d = spoke_digest(args.spoke)
    if d is None:
        dump_json({"error": "spoke_not_found", "spoke": args.spoke,
                   "looked_in": str(spoke_skill_path(args.spoke))})
        return 2
    dump_json({"spoke": args.spoke, "digest": d,
               "skill_path": str(spoke_skill_path(args.spoke))})
    return 0

def cmd_check_spoke(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists():
        dump_json({"loaded": False, "reason": "no state.json"}); return 2
    obj = json.loads(state_path.read_text(encoding="utf-8"))
    spokes = obj.get("spokes_loaded", {})
    loaded = args.spoke in spokes
    # Rows written before the digest requirement are plain ISO strings; rows
    # written after are dicts. A resumed run must not be re-gated just because
    # its state predates this change, so both shapes read as loaded.
    row = spokes.get(args.spoke)
    if isinstance(row, dict):
        marked_at, verified = row.get("at"), row.get("verified", False)
    else:
        marked_at, verified = row, False
    dump_json({"loaded": loaded, "spoke": args.spoke,
               "marked_at": marked_at, "verified": verified})
    return 0 if loaded else 2

def cmd_route_defect(args):
    """Record a routing defect and block closeout until it is amended.

    A routing defect is a route this runtime emitted that the harness could not
    actually invoke — overwhelmingly an invocation slug that does not exist
    (`luna` where the harness wanted `gpt-5.6-luna`). Retrying by hand fixes the
    run and loses the lesson, so the defect is durable state: `auto-closeout`
    refuses to complete while an unresolved row remains, which is what forces the
    isolated `auto-self-improve` amendment to the catalog row.
    """
    state_dir = Path(args.state_dir)
    if not (state_dir / "state.json").exists():
        dump_json({"error": "no state.json; run new-run/state-save first"}); return 1
    path = state_dir / "route-defects.jsonl"
    row = {
        "id": str(uuid.uuid4()),
        "kind": args.kind,
        "attempted": args.attempted,
        "observed_error": args.observed,
        "correction": args.correction,
        "harness": args.harness,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "resolved": False,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    dump_json({"recorded": row["id"], "file": str(path), "unresolved": len(load_route_defects(state_dir, unresolved_only=True))})
    return 0


class RouteDefectsUnreadable(ValueError):
    """Raised when durable route-defect evidence cannot be read safely."""

    def __init__(self, path: Path, row_number: int | None, detail: str):
        self.path = path
        self.row_number = row_number
        location = f" row {row_number}" if row_number is not None else ""
        super().__init__(f"{path}{location} is unreadable: {detail}")


def load_route_defects(state_dir, unresolved_only=False):
    path = Path(state_dir) / "route-defects.jsonl"
    if not path.exists(): return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RouteDefectsUnreadable(path, None, str(exc)) from exc
    for row_number, line in enumerate(lines, start=1):
        if not line.strip(): continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RouteDefectsUnreadable(path, row_number, str(exc)) from exc
        if not isinstance(row, dict):
            raise RouteDefectsUnreadable(path, row_number, "row must contain a JSON object")
        if unresolved_only and row.get("resolved"): continue
        rows.append(row)
    return rows

def cmd_resolve_route_defect(args):
    """Mark a recorded defect amended, naming the proposal that carries the fix."""
    state_dir = Path(args.state_dir)
    rows = load_route_defects(state_dir)
    if not rows:
        dump_json({"error": "no recorded route defects"}); return 1
    found = False
    for row in rows:
        if row.get("id") == args.id:
            row["resolved"] = True
            row["proposal_ref"] = args.proposal_ref
            row["resolved_at"] = datetime.now(timezone.utc).isoformat()
            found = True
    if not found:
        dump_json({"error": f"no route defect with id {args.id}"}); return 1
    path = state_dir / "route-defects.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)
    dump_json({"resolved": args.id, "unresolved": len(load_route_defects(state_dir, unresolved_only=True))})
    return 0

def cmd_check_route_defects(args):
    """Closeout gate. Exit 2 while any recorded routing defect is unamended.

    Absent run state is not a clean bill of health. `route-defects.jsonl` is only
    written when a defect is recorded, so its absence is ambiguous: it means
    either "this run dispatched routes and none failed" or "there is no run here
    at all" — a run that never called `start`, a mistyped `--state-dir`, or state
    that was cleaned up. Reporting the second as `clear: true` hands `auto-closeout`
    an affirmative pass from the one gate that exists to block completion, so a
    lifecycle that skipped `start` closes out looking gated.

    Resolve the ambiguity the same way `cmd_route_defect` and `cmd_check_spoke`
    already do — on `state.json` — and fail closed when it is missing.
    """
    state_dir = Path(args.state_dir)
    if not (state_dir / "state.json").exists():
        dump_json({
            "clear": False,
            "unresolved": [],
            "error": "no_run_state",
            "message": f"no state.json under {state_dir}; this run was never started, so routing defects cannot be checked",
        })
        return 2
    try:
        unresolved = load_route_defects(state_dir, unresolved_only=True)
    except RouteDefectsUnreadable as exc:
        result = {
            "clear": False,
            "unresolved": [],
            "error": "route_defects_unreadable",
            "message": str(exc),
        }
        if exc.row_number is not None:
            result["row"] = exc.row_number
        dump_json(result)
        return 2
    dump_json({"clear": not unresolved, "unresolved": unresolved})
    return 0 if not unresolved else 2

def cmd_state_reconcile(args):
    report = {"stale_leases_revoked": 0, "expired_dispatches": 0, "dirty_worktrees": 0}
    if args.db:
        now = datetime.now(timezone.utc)
        con = init_db(Path(args.db))
        stale_leases = con.execute("SELECT id, run_id, role, scope, holder_id FROM leases WHERE expires_at <= ? AND released_at IS NULL AND revoked_at IS NULL", (now.isoformat(),)).fetchall()
        for lid, rid, role, scope, h in stale_leases:
            con.execute("UPDATE leases SET revoked_at=?, revoke_reason='reconcile' WHERE id=?", (now.isoformat(), lid))
            con.execute("INSERT INTO ownership_events(id, run_id, role, scope, prior_holder, new_holder, event, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), rid, role, scope, h, None, 'reconcile_revoke', now.isoformat()))
            report["stale_leases_revoked"] += 1
        con.commit(); con.close()
    dump_json(report); return 0

def cmd_record_finding(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "finding.schema.json")
    if errors: dump_json({"valid": False, "errors": errors}); return 2
    if data.get("dispatch_id") and data.get("reviewer_dispatch_id") and data.get("dispatch_id") == data.get("reviewer_dispatch_id"):
        dump_json({"valid": False, "error": "Self-approval rejected: dispatch_id equals reviewer_dispatch_id"}); return 2
    con = init_db(Path(args.db))
    cols = ['id', 'dispatch_id', 'reviewer_dispatch_id', 'status', 'severity', 'summary', 'evidence_hash', 'created_at']
    row = [data.get(k) for k in cols]
    row[0] = row[0] or data.get("finding_id") or str(uuid.uuid4())
    row[7] = row[7] or datetime.now(timezone.utc).isoformat()
    con.execute(f"INSERT INTO findings({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", row)
    con.commit(); con.close(); dump_json({'recorded': row[0]}); return 0

def cmd_record_validation(args):
    data = load_data(args.file)
    con = init_db(Path(args.db))
    cols = ['id', 'dispatch_id', 'kind', 'command', 'passed', 'known_bad_proven', 'evidence_hash', 'created_at']
    row = [data.get(k) for k in cols]
    row[0] = row[0] or str(uuid.uuid4())
    row[4] = 1 if row[4] else 0
    row[5] = 1 if row[5] else 0
    row[7] = row[7] or datetime.now(timezone.utc).isoformat()
    con.execute(f"INSERT INTO validations({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", row)
    con.commit(); con.close(); dump_json({'recorded': row[0]}); return 0

def cmd_invalidate_packets(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists(): dump_json({"invalidated": 0, "error": "no state.json"}); return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old_version = state.get("plan_version", 1)
    approval_invalidated = _invalidate_approval_for_version_change(
        state,
        plan_version=args.plan_version,
        packet_version=state.get("packet_version", 1),
    )
    state["plan_version"] = args.plan_version
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    dump_json({"invalidated": 1, "plan_version": args.plan_version,
               "approval_invalidated": approval_invalidated}); return 0


def _invalidate_approval_for_version_change(
    state: dict,
    *,
    plan_version: int,
    packet_version: int,
) -> bool:
    """Remove live approval when its approved artifact version no longer matches."""
    approval = state.get("approval")
    if not isinstance(approval, dict):
        return False
    packet_aligned = (
        "packet_version" not in approval
        or approval.get("packet_version") == packet_version
    )
    if approval.get("plan_version") == plan_version and packet_aligned:
        return False

    invalidated_at = datetime.now(timezone.utc).isoformat()
    state.setdefault("invalidated_approvals", []).append({
        **approval,
        "invalidated_at": invalidated_at,
        "invalidated_for": {
            "plan_version": plan_version,
            "packet_version": packet_version,
        },
    })
    state.pop("approval", None)
    if state.get("phase") in PHASE_ORDER[PHASE_ORDER.index("approved"):]:
        state["phase"] = "planned"
    state["updated_at"] = invalidated_at
    return True

def cmd_increment_plan(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists(): dump_json({"error": "no state.json"}); return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old = state.get("plan_version", 1)
    new = old + 1
    approval_invalidated = _invalidate_approval_for_version_change(
        state,
        plan_version=new,
        packet_version=state.get("packet_version", 1),
    )
    state["plan_version"] = new
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    if args.db:
        con = init_db(Path(args.db))
        now = datetime.now(timezone.utc).isoformat()
        run_id = state.get("run_id", "unknown")
        con.execute("INSERT INTO artifact_versions(id, run_id, kind, version, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), run_id, "plan", new, sha256_obj(state), now))
        con.commit(); con.close()
    dump_json({"plan_version": new, "prior": old,
               "approval_invalidated": approval_invalidated}); return 0


# --------------------------------------------------------------------------
# Family/amendment/landing/checkpoint/completion CLI commands (amendment v2
# finding F6): T2 implements every command T0 pinned in §5 for T4, so T4 never
# needs to edit office_runtime.py or office_packets.py directly. Family/amendment
# logic lives in scripts/office_family.py; packet logic in scripts/office_packets.py;
# completion-event/replay-cursor logic is reused from scripts/office_monitor.py (T3)
# rather than reimplemented here, matching the route() delegation-shim pattern.
# --------------------------------------------------------------------------

def _office_family():
    try:
        from scripts import office_family
    except ImportError:
        import office_family
    return office_family


def _office_packets():
    try:
        from scripts import office_packets
    except ImportError:
        import office_packets
    return office_packets


def _office_monitor():
    try:
        from scripts import office_monitor
    except ImportError:
        import office_monitor
    return office_monitor


def cmd_family_show(args):
    fam = _office_family()
    state_dir = Path(args.state_dir)
    family_id = args.family_id
    if not family_id:
        target = fam.resolve_focus_target(state_dir)
        if target["status"] != "ok":
            dump_json({"error": "ambiguous_focus", "reason": target.get("reason")})
            return 3
        family_id = target["family_id"]
    registry = fam.load_family_registry(state_dir)
    entry = registry.get("families", {}).get(family_id)
    if entry is None:
        dump_json({"error": "family_not_found", "family_id": family_id})
        return 2
    dump_json(entry)
    return 0


def cmd_family_focus(args):
    fam = _office_family()
    result = fam.update_family_focus(Path(args.state_dir), args.family_id)
    dump_json(result)
    return 2 if result.get("status") == "error" else 0


def cmd_family_list(args):
    fam = _office_family()
    registry = fam.load_family_registry(Path(args.state_dir))
    families = [
        {
            "family_id": fid,
            "phase": entry.get("phase"),
            "versions": {
                "requirements_version": entry.get("requirements_version"),
                "plan_version": entry.get("plan_version"),
                "routing_version": entry.get("routing_version"),
            },
        }
        for fid, entry in sorted(registry.get("families", {}).items())
    ]
    dump_json({"current_focus": registry.get("current_focus_family_id"), "families": families})
    return 0


def cmd_family_update(args):
    fam = _office_family()
    latest_landing = load_data(args.latest_landing) if args.latest_landing else None
    result = fam.update_family(Path(args.state_dir), args.family_id, phase=args.phase,
                                latest_landing=latest_landing, issue=getattr(args, "issue", None))
    dump_json(result)
    return 2 if result.get("status") == "error" else 0


def cmd_amend(args):
    fam = _office_family()
    delta = load_data(args.delta_file)
    errors = validate_with_schema(delta, "amendment.schema.json")
    if errors:
        dump_json({"error": "schema_invalid", "errors": errors})
        return 2
    family_id = args.family_id or delta.get("family_id")
    if not family_id:
        dump_json({"error": "missing_family_id"})
        return 1
    kind = delta["kind"]
    owned_field = fam.KIND_VERSION_FIELD.get(kind)
    resulting = delta.get("resulting_versions", {})
    version_bumps = {owned_field: resulting[owned_field]} if owned_field in resulting else {}
    result = fam.apply_amendment(
        Path(args.state_dir), family_id, kind, delta["affected_scopes"], delta["reason"],
        delta["evidence"], delta["evidence_hash"], version_bumps,
        expected_prior_versions=delta.get("expected_prior_versions"),
        session_id=delta.get("session_id"), amendment_id=delta.get("amendment_id"),
    )
    dump_json(result)
    if result.get("status") == "conflict":
        return 3
    if result.get("status") == "error":
        return 2
    return 0


def cmd_save_checkpoint(args):
    fam = _office_family()
    data = load_data(args.file)
    family_id = args.family_id or data.get("family_id")
    result = fam.save_checkpoint(Path(args.state_dir), family_id, data)
    dump_json(result)
    return 2 if result.get("status") == "error" else 0


def cmd_load_checkpoint(args):
    fam = _office_family()
    if args.file:
        data = load_data(args.file)
        errors = fam.validate_checkpoint(data)
        if errors:
            dump_json({"error": "schema_invalid", "errors": errors})
            return 2
        dump_json(data)
        return 0
    data = fam.load_checkpoint(Path(args.state_dir), checkpoint_id=args.checkpoint_id)
    if data is None:
        dump_json({"error": "not_found", "checkpoint_id": args.checkpoint_id})
        return 1
    dump_json(data)
    return 0


def cmd_validate_checkpoint(args):
    fam = _office_family()
    data = load_data(args.file)
    errors = fam.validate_checkpoint(data)
    if errors:
        dump_json({"valid": False, "errors": errors})
        return 2
    dump_json({"valid": True})
    return 0


def cmd_record_landing(args):
    fam = _office_family()
    data = load_data(args.file)
    family_id = args.family_id or data.get("family_id")
    # Cross-check the cited evidence against the recorder when one is reachable: explicitly via
    # --db, else the run's resolved recorder. A landing's `passed` boolean is the producer
    # describing itself; the validations table is the record of a command having run.
    db_path = Path(args.db) if getattr(args, "db", None) else state_runs_db(args.state_dir)
    if not db_path.exists():
        # Silently skipping the cross-check when no recorder is reachable would make the strongest
        # check on this path the easiest one to switch off -- point --state-dir somewhere without
        # a runs.db and an invented evidence hash is accepted again.
        # Exit 1, not 4: the contract pins record-landing's exit 4 to "missing validation
        # evidence", which is a statement about the landing. An unreachable recorder is a
        # statement about the environment.
        dump_json({"status": "error", "reason": "recorder_unreachable", "db": str(db_path)})
        return 1
    result = fam.record_landing(Path(args.state_dir), family_id, data, db_path=db_path)
    dump_json(result)
    if result.get("status") == "error":
        return 4 if result.get("reason") in (
            "missing_validation_evidence", "empty_evidence_hash", "evidence_not_recorded") else 2
    return 0


def cmd_validate_landing(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "landing.schema.json")
    if errors:
        dump_json({"valid": False, "errors": errors})
        return 2
    dump_json({"valid": True})
    return 0


def cmd_verify_landing(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "landing.schema.json")
    if errors:
        dump_json({"verified": False, "errors": errors})
        return 4
    commands = (data.get("validation_evidence") or {}).get("commands") or []
    if not commands:
        dump_json({"verified": False, "reason": "no validation commands recorded"})
        return 4
    if getattr(args, "strict", False):
        for command in commands:
            proc = subprocess.run(shlex.split(command), cwd=str(ROOT), capture_output=True)
            if proc.returncode != 0:
                dump_json({"verified": False, "reason": f"command failed: {command}",
                           "exit_code": proc.returncode})
                return 4
    else:
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True,
                                   capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            head = None
        head_sha = data.get("head_sha")
        if head and head_sha and not head.startswith(head_sha) and not head_sha.startswith(head):
            dump_json({"verified": False, "reason": "head_sha does not match current commit tree",
                       "head_sha": head})
            return 4
    dump_json({"verified": True, "head_sha": data.get("head_sha")})
    return 0


def cmd_record_event(args):
    return _office_monitor().cmd_record_event(args)


def cmd_list_events(args):
    return _office_monitor().cmd_list_events(args)


def cmd_ack_event(args):
    return _office_monitor().cmd_ack_event(args)


def cmd_completion_status(args):
    return _office_monitor().cmd_completion_status(args)


def cmd_record_start_receipt(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "start-receipt.schema.json")
    if errors:
        dump_json({"error": "schema_invalid", "errors": errors})
        return 2
    dispatch_id = data["dispatch_id"]
    out_dir = Path(args.state_dir) / "dispatches" / dispatch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(out_dir / "start_receipt.json", data)
    dump_json({"status": "recorded", "receipt_id": data["receipt_id"]})
    return 0


def cmd_record_review(args):
    fam = _office_family()
    data = load_data(args.file)
    result = fam.record_review(Path(args.state_dir), data)
    dump_json(result)
    if result.get("status") == "error":
        return 2 if result.get("reason") == "schema_invalid" else 4
    return 0


def cmd_validate_review(args):
    data = load_data(args.file)
    errors = validate_with_schema(data, "review-result.schema.json")
    if errors:
        dump_json({"valid": False, "errors": errors})
        return 2
    dump_json({"valid": True})
    return 0


def cmd_tmp_dir(args):
    try:
        if args.state_dir:
            p = Path(args.state_dir).expanduser().resolve() / "tmp"
        else:
            p = _runs_tmp_dir()
        p.mkdir(parents=True, exist_ok=True)
        dump_json({"tmp_dir": str(p.resolve())})
        return 0
    except Exception as exc:
        dump_json({"error": "tmp_dir_failed", "message": str(exc)})
        return 2


def cmd_cleanup_worktrees(args):
    """Auto-delete used worktrees and merged branches for a run."""
    try:
        state_dir = Path(args.state_dir).expanduser().resolve()
        state_path = state_dir / "state.json"
        if not state_path.exists():
            dump_json({"error": "no_state", "state_dir": str(state_dir)})
            return 2
        state = json.loads(state_path.read_text(encoding="utf-8"))
        run_id = state.get("run_id", "")
        repo_root = Path(args.repo or state.get("repo_root") or ".").expanduser().resolve()

        worktrees_to_remove = set()
        branches_to_delete = set()

        # 1. Discover worktrees from dispatches
        dispatches_dir = state_dir / "dispatches"
        if dispatches_dir.is_dir():
            for meta_file in dispatches_dir.glob("*/meta.json"):
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    wt = meta.get("worktree")
                    if wt and Path(wt).resolve() != repo_root and Path(wt).exists():
                        wt_resolved = str(Path(wt).resolve())
                        worktrees_to_remove.add(wt_resolved)
                        try:
                            res_br = subprocess.run(["git", "-C", wt_resolved, "rev-parse", "--abbrev-ref", "HEAD"],
                                                    capture_output=True, text=True, check=True)
                            br = res_br.stdout.strip()
                            if br and br != "HEAD" and br.startswith("office/"):
                                branches_to_delete.add(br)
                        except Exception:
                            pass
                except Exception:
                    pass

        # 2. Discover worktrees from git worktree list matching run_id
        if run_id:
            try:
                res = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(repo_root),
                                     capture_output=True, text=True, check=True)
                current_wt = None
                for line in res.stdout.splitlines():
                    if line.startswith("worktree "):
                        current_wt = line[len("worktree "):].strip()
                    elif line.startswith("branch refs/heads/"):
                        br = line[len("branch refs/heads/"):].strip()
                        if current_wt and Path(current_wt).resolve() != repo_root:
                            if f"/{run_id}/" in br or br.endswith(f"/{run_id}"):
                                worktrees_to_remove.add(str(Path(current_wt).resolve()))
                                branches_to_delete.add(br)
            except Exception:
                pass

        removed_worktrees = []
        skipped_dirty = []
        deleted_branches = []
        errors = []

        for wt in sorted(worktrees_to_remove):
            wt_path = Path(wt)
            if not wt_path.exists():
                continue
            if not args.force:
                try:
                    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt_path),
                                        capture_output=True, text=True)
                    if st.stdout.strip():
                        skipped_dirty.append(wt)
                        continue
                except Exception:
                    pass

            cmd = ["git", "worktree", "remove"]
            if args.force:
                cmd.append("--force")
            cmd.append(str(wt_path))
            try:
                res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
                if res.returncode == 0:
                    removed_worktrees.append(wt)
                else:
                    errors.append(f"Failed to remove {wt}: {res.stderr.strip()}")
            except Exception as exc:
                errors.append(f"Error removing {wt}: {exc}")

        try:
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root),
                           capture_output=True, text=True)
        except Exception:
            pass

        for br in sorted(branches_to_delete):
            try:
                res = subprocess.run(["git", "branch", "-d", br], cwd=str(repo_root),
                                     capture_output=True, text=True)
                if res.returncode == 0:
                    deleted_branches.append(br)
            except Exception:
                pass

        dump_json({
            "cleaned": True,
            "removed_worktrees": removed_worktrees,
            "skipped_dirty": skipped_dirty,
            "deleted_branches": deleted_branches,
            "errors": errors
        })
        return 0
    except Exception as exc:
        dump_json({"error": "cleanup_worktrees_failed", "message": str(exc)})
        return 2


def reuse_dispatch_plan(role: str, context_tokens: int | None, herdr_available: bool,
                         compact_supported: bool, target: str, brief_path: str) -> dict:
    """Pure decision: reuse a worker as-is, or compact it then queue the next brief.

    Never emits a spawn/start command and never waits on compaction; the brief command is
    queued immediately after the compact command (Herdr queues the prompt for the worker).
    """
    brief_command = ["herdr", "agent", "prompt", target,
                      f"Read and carry out the brief at {brief_path} exactly."]

    if role not in REUSE_COMPACT_ROLES:
        reason = "role_not_eligible"
    elif context_tokens is None:
        reason = "context_unknown"
    elif not (context_tokens > REUSE_COMPACT_THRESHOLD):
        reason = "at_or_below_threshold"
    elif not herdr_available:
        reason = "herdr_unavailable"
    elif not compact_supported:
        reason = "compact_unsupported"
    else:
        reason = None

    if reason is None:
        return {
            "mode": "compact_then_queue",
            "reason": "eligible_over_threshold",
            "commands": [["herdr", "agent", "prompt", target, "/compact"], brief_command],
            "await_compaction": False,
            "threshold": REUSE_COMPACT_THRESHOLD,
            "context_tokens": context_tokens,
        }

    return {
        "mode": "normal_reuse",
        "reason": reason,
        "commands": [brief_command],
        "await_compaction": False,
        "threshold": REUSE_COMPACT_THRESHOLD,
        "context_tokens": context_tokens,
    }


def cmd_reuse_plan(args):
    result = reuse_dispatch_plan(
        role=args.role,
        context_tokens=args.context_tokens,
        herdr_available=args.herdr_available,
        compact_supported=args.compact_supported,
        target=args.target,
        brief_path=args.brief_path,
    )
    dump_json(result)
    return 0


def main():
    p=argparse.ArgumentParser(description='Auto Office v3 deterministic runtime helpers')
    sp=p.add_subparsers(dest='cmd',required=True)
    q=sp.add_parser('validate-packet'); q.add_argument('--kind',choices=['execution','envelope'],required=True); q.add_argument('file'); q.set_defaults(func=cmd_validate_packet)
    q=sp.add_parser('validate-adapter'); q.add_argument('file'); q.set_defaults(func=cmd_validate_adapter)
    q=sp.add_parser('route'); q.add_argument('request'); q.set_defaults(func=cmd_route)
    q=sp.add_parser('hash'); q.add_argument('file'); q.set_defaults(func=cmd_hash)
    q=sp.add_parser('maturity'); q.add_argument('--points',type=float,required=True); q.set_defaults(func=cmd_maturity)
    q=sp.add_parser('privacy-lint'); q.add_argument('file'); q.add_argument('--deny-file'); q.set_defaults(func=cmd_privacy)
    q=sp.add_parser('init-db'); q.add_argument('--db',required=True); q.set_defaults(func=cmd_init_db)
    q=sp.add_parser('record-dispatch'); q.add_argument('--db',required=True); q.add_argument('file'); q.set_defaults(func=cmd_record_dispatch)
    q=sp.add_parser('record-finding'); q.add_argument('--db',required=True); q.add_argument('file'); q.set_defaults(func=cmd_record_finding)
    q=sp.add_parser('record-validation'); q.add_argument('--db',required=True); q.add_argument('file'); q.set_defaults(func=cmd_record_validation)
    q=sp.add_parser('invalidate-packets'); q.add_argument('--state-dir',required=True); q.add_argument('--plan-version',type=int,required=True); q.set_defaults(func=cmd_invalidate_packets)
    q=sp.add_parser('increment-plan'); q.add_argument('--state-dir',required=True); q.add_argument('--db'); q.set_defaults(func=cmd_increment_plan)
    q=sp.add_parser('scaffold-adapter'); q.add_argument('id'); q.add_argument('--out',required=True); q.set_defaults(func=cmd_scaffold_adapter)
    q=sp.add_parser('catalog-snapshot'); q.add_argument('--input',required=True); q.add_argument('--out-dir',required=True); q.set_defaults(func=cmd_catalog_snapshot)
    q=sp.add_parser('effective-config'); q.add_argument('--repo-root',default='.'); q.add_argument('--user'); q.add_argument('--overrides'); q.add_argument('--set',action='append'); q.add_argument('--hash-only',action='store_true'); q.set_defaults(func=cmd_effective_config)
    q=sp.add_parser('proposal-id'); q.add_argument('--stream',choices=['learned-pattern','catalog-policy'],required=True); q.add_argument('--kind',required=True); g=q.add_mutually_exclusive_group(required=True); g.add_argument('--file'); g.add_argument('--text'); q.set_defaults(func=cmd_proposal_id)
    q=sp.add_parser('replay'); q.add_argument('--dataset',required=True); q.add_argument('--old-policy',required=True); q.add_argument('--new-policy',required=True); q.set_defaults(func=cmd_replay)
    q=sp.add_parser('new-run'); q.add_argument('--family-id',required=True); q.add_argument('--holder-id',required=True); q.add_argument('--triple',required=True); q.add_argument('--gear',required=True); q.add_argument('--playbook',choices=['Change','Restructure','Investigate','Prototype','Visual'],required=True); q.add_argument('--base-sha',required=True); q.add_argument('--policy-hash',required=True); q.add_argument('--catalog-hash',required=True); q.add_argument('--adapter-hash',required=True); q.add_argument('--config-hash',required=True); q.add_argument('--out',required=True); q.set_defaults(func=cmd_new_run)
    q=sp.add_parser('start'); q.add_argument('--goal',required=True); q.add_argument('--playbook',choices=['Change','Restructure','Investigate','Prototype','Visual'],required=True); q.add_argument('--gear',choices=['direct','direct+review','light','quick','express','full']); q.add_argument('--repo',default='.'); q.add_argument('--volume',action='store_true'); q.add_argument('--interview',action='store_true'); q.add_argument('--adversarial',action='store_true'); q.add_argument('--irreversible',action='store_true'); q.add_argument('--blast-radius',dest='blast_radius',choices=['local','repo','production','production-data']); q.add_argument('--size-class',dest='size_class',choices=['S','M','L','XL']); q.set_defaults(func=cmd_start)
    q=sp.add_parser('lease-acquire'); q.add_argument('--db',required=True); q.add_argument('--run-id',required=True); q.add_argument('--role',required=True); q.add_argument('--scope',required=True); q.add_argument('--holder-id',required=True); q.add_argument('--ttl',type=int,default=3600); q.set_defaults(func=cmd_lease_acquire)
    q=sp.add_parser('lease-renew'); q.add_argument('--db',required=True); q.add_argument('--lease-id',required=True); q.add_argument('--holder-id',required=True); q.add_argument('--ttl',type=int,default=3600); q.set_defaults(func=cmd_lease_renew)
    q=sp.add_parser('lease-release'); q.add_argument('--db',required=True); q.add_argument('--lease-id',required=True); q.add_argument('--holder-id',required=True); q.set_defaults(func=cmd_lease_release)
    q=sp.add_parser('lease-check'); q.add_argument('--db',required=True); q.add_argument('--run-id',required=True); q.add_argument('--scope',required=True); q.set_defaults(func=cmd_lease_check)
    q=sp.add_parser('state-save'); q.add_argument('--state-dir',required=True); q.add_argument('--run-id',required=True); q.add_argument('--family-id',required=True); q.add_argument('--phase',required=True); q.add_argument('--plan-version',type=int); q.add_argument('--packet-version',type=int); q.add_argument('--dispatches'); q.add_argument('--findings'); q.add_argument('--lease'); q.set_defaults(func=cmd_state_save)
    q=sp.add_parser('state-load'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_state_load)
    q=sp.add_parser('plan-review-round-authorized'); q.add_argument('--verdict',required=True); q.set_defaults(func=cmd_plan_review_round_authorized)
    q=sp.add_parser('resolve-gates'); q.add_argument('--state-dir',required=True); q.add_argument('--blast-radius',dest='blast_radius',choices=['local','repo','production','production-data']); q.add_argument('--size-class',dest='size_class',choices=['S','M','L','XL']); q.add_argument('--irreversible',action='store_true'); q.set_defaults(func=cmd_resolve_gates)
    q=sp.add_parser('freeze-intent'); q.add_argument('--state-dir',required=True); q.add_argument('--intent',required=True,help='JSON file with goal, done_criteria, blast_radius, named_actions, non_goals'); q.set_defaults(func=cmd_freeze_intent)
    q=sp.add_parser('approve-plan'); q.add_argument('--state-dir',required=True); q.add_argument('--approved-by',choices=['user'],required=True); q.add_argument('--quote',required=True); q.add_argument('--plan-path'); q.set_defaults(func=cmd_approve_plan)
    q=sp.add_parser('state-reconcile'); q.add_argument('--state-dir',required=True); q.add_argument('--db'); q.set_defaults(func=cmd_state_reconcile)
    q=sp.add_parser('mark-spoke'); q.add_argument('--state-dir',required=True); q.add_argument('--spoke',required=True); q.add_argument('--digest'); q.add_argument('--unverified',action='store_true'); q.set_defaults(func=cmd_mark_spoke)
    q=sp.add_parser('spoke-digest'); q.add_argument('--spoke',required=True); q.set_defaults(func=cmd_spoke_digest)
    q=sp.add_parser('check-spoke'); q.add_argument('--state-dir',required=True); q.add_argument('--spoke',required=True); q.set_defaults(func=cmd_check_spoke)
    q=sp.add_parser('route-defect'); q.add_argument('--state-dir',required=True); q.add_argument('--kind',choices=['invalid-invocation-slug','unsupported-effort','missing-adapter','other'],default='invalid-invocation-slug'); q.add_argument('--attempted',required=True); q.add_argument('--observed',required=True); q.add_argument('--correction'); q.add_argument('--harness'); q.set_defaults(func=cmd_route_defect)
    q=sp.add_parser('resolve-route-defect'); q.add_argument('--state-dir',required=True); q.add_argument('--id',required=True); q.add_argument('--proposal-ref',required=True); q.set_defaults(func=cmd_resolve_route_defect)
    q=sp.add_parser('check-route-defects'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_check_route_defects)

    # --- family / amendment / checkpoint / landing / completion / review (§5) ---
    q=sp.add_parser('family-show'); q.add_argument('--family-id'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_family_show)
    q=sp.add_parser('family-focus'); q.add_argument('--family-id',required=True); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_family_focus)
    q=sp.add_parser('family-list'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_family_list)
    q=sp.add_parser('family-update'); q.add_argument('--family-id',required=True); q.add_argument('--phase'); q.add_argument('--latest-landing'); q.add_argument('--issue'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_family_update)
    q=sp.add_parser('amend'); q.add_argument('--kind',choices=['routing','requirements','plan_contract'],required=True); q.add_argument('--delta-file',required=True); q.add_argument('--family-id'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_amend)
    q=sp.add_parser('save-checkpoint'); q.add_argument('--file',required=True); q.add_argument('--family-id'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_save_checkpoint)
    q=sp.add_parser('load-checkpoint'); g=q.add_mutually_exclusive_group(required=True); g.add_argument('--file'); g.add_argument('--checkpoint-id'); q.add_argument('--state-dir'); q.set_defaults(func=cmd_load_checkpoint)
    q=sp.add_parser('validate-checkpoint'); q.add_argument('file'); q.set_defaults(func=cmd_validate_checkpoint)
    q=sp.add_parser('record-landing'); q.add_argument('--file',required=True); q.add_argument('--family-id'); q.add_argument('--state-dir',required=True); q.add_argument('--db'); q.set_defaults(func=cmd_record_landing)
    q=sp.add_parser('validate-landing'); q.add_argument('file'); q.set_defaults(func=cmd_validate_landing)
    q=sp.add_parser('verify-landing'); q.add_argument('--file',required=True); q.add_argument('--strict',action='store_true'); q.set_defaults(func=cmd_verify_landing)
    q=sp.add_parser('record-event'); q.add_argument('--file'); q.add_argument('--event-id'); q.add_argument('--session-id'); q.add_argument('--family-id'); q.add_argument('--dispatch-id'); q.add_argument('--sequence',type=int); q.add_argument('--observed-status'); q.add_argument('--terminal-classification'); q.add_argument('--source'); q.add_argument('--evidence-timestamp'); q.add_argument('--evidence-hash'); q.add_argument('--evidence-payload'); q.add_argument('--state-dir',default='.office'); q.set_defaults(func=cmd_record_event)
    q=sp.add_parser('list-events'); q.add_argument('--dispatch-id'); q.add_argument('--since-seq',type=int); q.add_argument('--state-dir',default='.office'); q.set_defaults(func=cmd_list_events)
    q=sp.add_parser('ack-event'); q.add_argument('--session-id',required=True); q.add_argument('--family-id',required=True); q.add_argument('--dispatch-id',required=True); q.add_argument('--sequence',type=int,required=True); q.add_argument('--event-id',required=True); q.add_argument('--state-dir',default='.office'); q.set_defaults(func=cmd_ack_event)
    q=sp.add_parser('completion-status'); q.add_argument('--dispatch-id',required=True); q.add_argument('--state-dir',default='.office'); q.set_defaults(func=cmd_completion_status)
    q=sp.add_parser('record-start-receipt'); q.add_argument('--file',required=True); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_record_start_receipt)
    q=sp.add_parser('record-review'); q.add_argument('--file',required=True); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_record_review)
    q=sp.add_parser('validate-review'); q.add_argument('file'); q.set_defaults(func=cmd_validate_review)
    q=sp.add_parser('tmp-dir'); q.add_argument('--state-dir'); q.set_defaults(func=cmd_tmp_dir)
    q=sp.add_parser('cleanup-worktrees'); q.add_argument('--state-dir',required=True); q.add_argument('--repo'); q.add_argument('--force',action='store_true'); q.set_defaults(func=cmd_cleanup_worktrees)
    q=sp.add_parser('reuse-plan'); q.add_argument('--role',required=True); q.add_argument('--context-tokens',dest='context_tokens',type=int); q.add_argument('--herdr-available',dest='herdr_available',action='store_true'); q.add_argument('--no-herdr-available',dest='herdr_available',action='store_false'); q.add_argument('--compact-supported',dest='compact_supported',action='store_true'); q.add_argument('--no-compact-supported',dest='compact_supported',action='store_false'); q.add_argument('--target',required=True); q.add_argument('--brief-path',dest='brief_path',required=True); q.set_defaults(herdr_available=os.environ.get('HERDR_ENV')=='1', compact_supported=False, func=cmd_reuse_plan)

    args=p.parse_args(); sys.exit(args.func(args))

if __name__=='__main__': main()
