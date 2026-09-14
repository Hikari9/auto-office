#!/usr/bin/env python3
"""Deterministic helpers for the Auto Office v3 skill bundle.

Route-time commands never perform network access. Catalog fetching/normalization should happen
outside routing and be committed to a content-addressed local snapshot before selection.
"""
from __future__ import annotations
import argparse, contextlib, hashlib, io, json, math, os, re, sqlite3, subprocess, sys, uuid
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
    dump_json({"valid": not errors, "errors": errors})
    return 0 if not errors else 2


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
    if not invocation:
        # The catalog row carries no harness-specific slug, so the dispatch will
        # be attempted with the canonical model_id. Spec-seed names ("luna") are
        # not harness slugs ("gpt-5.6-luna"), so this fallback is the single
        # largest source of route-time dispatch failures. Say so in the
        # disclosure instead of letting it look like a resolved slug.
        reasons.append("carries no catalog invocation slug, so model_id is being used unverified")
    return {
        "role": role,
        "model_id": chosen.get("model_id"),
        "invocation_model_id": invocation or chosen.get("model_id"),
        "invocation_model_id_source": "catalog" if invocation else "fallback:model_id",
        "effort": chosen.get("effort"),
        "harness": chosen.get("harness"),
        "harness_version": chosen.get("harness_version"),
        "triple": candidate_id(chosen),
        "reason": "; ".join(reasons),
    }


def route(request: dict) -> dict:
    role = request["role"]
    playbook = request.get("playbook")
    policy = request.get("policy", {})
    required = set(request.get("required_capabilities", policy.get("required_capabilities", [])))
    reserve = float(policy.get("quota_reserve_percent", 20))
    cost_policy = request.get("cost_policy", policy.get("cost_policy", "balanced"))
    allow_override = bool(request.get("allow_unverified_override", False))
    allow_advisory_undercut = bool(request.get("allow_advisory_undercut", True))
    preferred_seed = request.get("preferred_seed") or policy.get("preferred_seed")
    rejected = []
    stage = []

    # 1 hard exclusions
    for c in request.get("candidates", []):
        cid = candidate_id(c)
        if c.get("hard_excluded") or c.get("local_hard_excluded"):
            rejected.append({"candidate":cid,"stage":1,"reason":"hard exclusion"})
        else:
            stage.append(c)

    # 2 adapter validity/trust
    nxt=[]
    for c in stage:
        cid=candidate_id(c); st=c.get("adapter_state")
        if st == "invalid": rejected.append({"candidate":cid,"stage":2,"reason":"invalid adapter"}); continue
        if role in MUTABLE_TRUST_ROLES and st != "proven" and not allow_override:
            rejected.append({"candidate":cid,"stage":2,"reason":"adapter is not proven for normal mutable/gate authority"}); continue
        nxt.append(c)
    stage=nxt

    # 3 required capabilities
    nxt=[]
    for c in stage:
        cid=candidate_id(c); caps=set(c.get("capabilities", []))
        if not required.issubset(caps): rejected.append({"candidate":cid,"stage":3,"reason":f"missing capabilities {sorted(required-caps)}"}); continue
        nxt.append(c)
    stage=nxt

    # 4 absolute role floor
    nxt=[]
    for c in stage:
        cid=candidate_id(c)
        if not c.get("absolute_floor_pass", False): rejected.append({"candidate":cid,"stage":4,"reason":"absolute role floor failed"}); continue
        nxt.append(c)
    stage=nxt

    # 5 task shape
    nxt=[]
    for c in stage:
        cid=candidate_id(c); supported=c.get("supported_playbooks")
        if supported and playbook and playbook not in supported: rejected.append({"candidate":cid,"stage":5,"reason":"task shape unsupported"}); continue
        nxt.append(c)
    stage=nxt

    if not stage:
        return {"selected":None,"status":"no_qualifying_candidate","rejected":rejected}

    # 6 quota safety. Unknown is not unlimited: it is allowed but ranked as uncertain only when no safe-known alternative.
    safe=[]; unknown=[]; unsafe=[]
    for c in stage:
        q=c.get("quota",{}); status=q.get("status","unknown")
        if status != "ok" or q.get("tightest_remaining_percent") is None:
            unknown.append(c); continue
        remaining=float(q["tightest_remaining_percent"]); burn=float(q.get("projected_burn_percent") or 0)
        (safe if remaining-burn >= reserve else unsafe).append(c)
    if safe:
        for c in unsafe: rejected.append({"candidate":candidate_id(c),"stage":6,"reason":"projected quota crosses reserve while safe alternative exists"})
        # Unknown quota is not silently unlimited; prefer known-safe unless user permits uncertainty.
        if not request.get("allow_unknown_quota_with_safe_alternative", False):
            for c in unknown: rejected.append({"candidate":candidate_id(c),"stage":6,"reason":"quota unknown while known-safe alternative exists"})
            stage=safe
        else:
            stage=safe+unknown
    elif unknown:
        for c in unsafe: rejected.append({"candidate":candidate_id(c),"stage":6,"reason":"known quota crosses reserve; only unknown candidates remain"})
        stage=unknown
    else:
        return {"selected":None,"status":"protected_quota_would_be_consumed","rejected":rejected,
                "action":"choose a smaller/cheaper valid strategy, propose another route, or obtain explicit user authority"}

    # 7 advisory quality anchor
    if preferred_seed:
        matched=[c for c in stage if preferred_rank(c, preferred_seed) is not None]
        advisory=matched if matched else stage
    else:
        advisory=[c for c in stage if c.get("advisory_pass", True)]
    if advisory and not allow_advisory_undercut:
        for c in stage:
            if c not in advisory: rejected.append({"candidate":candidate_id(c),"stage":7,"reason":"advisory anchor retained by gear/policy"})
        stage=advisory

    # 8 cost, 9 local tie-break
    def quota_burn(c): return _num(c.get("cost",{}).get("quota_burn"))
    def money(c): return _num(c.get("cost",{}).get("money_estimate"))
    def wall(c): return _num(c.get("cost",{}).get("wall_clock_seconds"))
    def reward(c): return -float(c.get("local_reward",0))

    if preferred_seed:
        # preferred_seed is an ordered fallback chain (first entry = first
        # choice): the chain itself already encodes the user's cost/quality
        # tradeoff, so rank outranks cost_policy's money-band elimination;
        # cost only breaks ties between candidates matching the same entry.
        default_rank=len(preferred_seed)
        stage.sort(key=lambda c:(preferred_rank(c, preferred_seed) if preferred_rank(c, preferred_seed) is not None else default_rank,
                                  quota_burn(c), money(c), wall(c), reward(c), candidate_id(c)))
    elif cost_policy == "quota_saver":
        stage.sort(key=lambda c:(quota_burn(c), money(c), wall(c), reward(c), candidate_id(c)))
    elif cost_policy == "money_saver":
        stage.sort(key=lambda c:(money(c), quota_burn(c), wall(c), reward(c), candidate_id(c)))
    else:
        known_money=[money(c) for c in stage if math.isfinite(money(c))]
        if known_money:
            cheapest=min(known_money); band=cheapest*1.20
            in_band=[c for c in stage if money(c) <= band]
            if in_band:
                out_band=[c for c in stage if c not in in_band]
                for c in out_band: rejected.append({"candidate":candidate_id(c),"stage":8,"reason":"outside balanced 20% cheapest-money band"})
                stage=in_band
                stage.sort(key=lambda c:(quota_burn(c), wall(c), reward(c), money(c), candidate_id(c)))
            else:
                stage.sort(key=lambda c:(quota_burn(c), wall(c), reward(c), money(c), candidate_id(c)))
        else:
            stage.sort(key=lambda c:(quota_burn(c), wall(c), reward(c), candidate_id(c)))

    chosen=stage[0]
    return {"selected":candidate_id(chosen),"status":"selected","candidate":chosen,"rejected":rejected,
            "selection_disclosure":selection_disclosure(role, chosen, preferred_seed, cost_policy),
            "decision_hash":sha256_obj({"role":role,"playbook":playbook,"selected":candidate_id(chosen),"policy":policy,"candidate":chosen})}


def cmd_route(args):
    req=load_data(args.request)
    dump_json(route(req)); return 0


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
    """)
    dispatch_columns = {row[1] for row in con.execute("PRAGMA table_info(dispatches)")}
    if "invocation_model_id" not in dispatch_columns:
        con.execute("ALTER TABLE dispatches ADD COLUMN invocation_model_id TEXT")
    if "selection_reason" not in dispatch_columns:
        con.execute("ALTER TABLE dispatches ADD COLUMN selection_reason TEXT")
    con.commit(); return con


def cmd_init_db(args):
    con=init_db(Path(args.db)); mode=con.execute('PRAGMA journal_mode').fetchone()[0]; con.close(); dump_json({"db":str(Path(args.db)),"journal_mode":mode}); return 0


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


def _start_state_root() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return root.expanduser().resolve() / "auto-office" / "runs"


def _start_holder() -> tuple[str, str]:
    holder_id = os.environ.get("AUTO_OFFICE_HOLDER_ID") or "orchestrator"
    triple = os.environ.get("AUTO_OFFICE_HOLDER_TRIPLE") or "codex@local/orchestrator@none"
    return holder_id, triple


def _start_gear(args) -> str:
    if args.gear:
        return args.gear
    if getattr(args, "irreversible", False):
        return "full"
    return "express" if sum(bool(v) for v in (args.volume, args.interview, args.adversarial)) >= 2 else "direct"


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
        config, config_hash = _start_effective_config(repo)
        gear = _start_gear(args)
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
        errors = validate_with_schema(envelope, "run-envelope.schema.json")
        if errors:
            dump_json({"valid": False, "errors": errors})
            return 2
        state_dir = _start_state_root() / run_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state = {"run_id": run_id, "family_id": family_id, "phase": "intake", "plan_version": 1,
                 "packet_version": 1, "updated_at": envelope["created_at"], "goal": args.goal,
                 "playbook": args.playbook, "gear": gear, "holder_id": holder_id, "triple": triple,
                 "base_sha": base_sha, "plugin_commit": plugin_commit, "policy_hash": policy_hash,
                 "catalog_snapshot_hash": catalog_hash, "adapter_snapshot_hash": adapter_hash,
                 "effective_config_hash": config_hash}
        _atomic_write_json(state_dir / "state.json", state)
        _atomic_write_json(state_dir / "envelope.json", envelope)
        pointer = repo / ".office" / "runs" / f"{run_id}.ref"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(state_dir.resolve()) + "\n", encoding="utf-8")
        _ensure_repo_gitignore(repo)
        kickoff = (f"Auto Office kickoff\nGoal: {args.goal}\nPlaybook: {args.playbook}\n"
                   f"Gear: {gear}\nRun: {run_id}\nBase SHA: {base_sha}")
        dump_json({"state_dir": str(state_dir.resolve()), "run_id": run_id, "gear": gear,
                   "pointer": str(pointer.resolve()), "kickoff": kickoff})
        return 0
    except Exception as exc:
        dump_json({"error": "start_failed", "message": str(exc)})
        return 2


def _plan_file_hash(path: str | None) -> str | None:
    if not path:
        return None
    return "sha256:" + hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()


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
                    "plan_version": plan_version, "plan_sha": _plan_file_hash(args.plan_path)}
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
    current_index = PHASE_ORDER.index(current_phase)
    requested_index = PHASE_ORDER.index(args.phase)
    if requested_index != current_index and requested_index != current_index + 1:
        expected = current_phase if current_index == len(PHASE_ORDER) - 1 else PHASE_ORDER[current_index + 1]
        dump_json({"error": "invalid_phase_transition", "from": current_phase,
                   "to": args.phase, "expected": expected,
                   "message": "lifecycle transitions are monotonic and adjacent"})
        return 2
    obj.update({"run_id": args.run_id, "family_id": args.family_id, "phase": args.phase, "plan_version": args.plan_version, "packet_version": args.packet_version, "updated_at": datetime.now(timezone.utc).isoformat()})
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

def cmd_mark_spoke(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists():
        dump_json({"error": "no state.json; run new-run/state-save first"}); return 1
    obj = json.loads(state_path.read_text(encoding="utf-8"))
    spokes = obj.setdefault("spokes_loaded", {})
    spokes[args.spoke] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(state_path, obj)
    dump_json({"marked": args.spoke, "phase": obj.get("phase")}); return 0

def cmd_check_spoke(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists():
        dump_json({"loaded": False, "reason": "no state.json"}); return 2
    obj = json.loads(state_path.read_text(encoding="utf-8"))
    spokes = obj.get("spokes_loaded", {})
    loaded = args.spoke in spokes
    dump_json({"loaded": loaded, "spoke": args.spoke, "marked_at": spokes.get(args.spoke)})
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

def load_route_defects(state_dir, unresolved_only=False):
    path = Path(state_dir) / "route-defects.jsonl"
    if not path.exists(): return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try: row = json.loads(line)
        except ValueError: continue
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
    """Closeout gate. Exit 2 while any recorded routing defect is unamended."""
    unresolved = load_route_defects(Path(args.state_dir), unresolved_only=True)
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
    state["plan_version"] = args.plan_version
    # Invalidate logic placeholder for state
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    dump_json({"invalidated": 1, "plan_version": args.plan_version}); return 0

def cmd_increment_plan(args):
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists(): dump_json({"error": "no state.json"}); return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old = state.get("plan_version", 1)
    new = old + 1
    state["plan_version"] = new
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    if args.db:
        con = init_db(Path(args.db))
        now = datetime.now(timezone.utc).isoformat()
        run_id = state.get("run_id", "unknown")
        con.execute("INSERT INTO artifact_versions(id, run_id, kind, version, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), run_id, "plan", new, sha256_obj(state), now))
        con.commit(); con.close()
    dump_json({"plan_version": new, "prior": old}); return 0

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
    q=sp.add_parser('start'); q.add_argument('--goal',required=True); q.add_argument('--playbook',choices=['Change','Restructure','Investigate','Prototype','Visual'],required=True); q.add_argument('--gear',choices=['direct','direct+review','light','quick','express','full']); q.add_argument('--repo',default='.'); q.add_argument('--volume',action='store_true'); q.add_argument('--interview',action='store_true'); q.add_argument('--adversarial',action='store_true'); q.add_argument('--irreversible',action='store_true'); q.set_defaults(func=cmd_start)
    q=sp.add_parser('lease-acquire'); q.add_argument('--db',required=True); q.add_argument('--run-id',required=True); q.add_argument('--role',required=True); q.add_argument('--scope',required=True); q.add_argument('--holder-id',required=True); q.add_argument('--ttl',type=int,default=3600); q.set_defaults(func=cmd_lease_acquire)
    q=sp.add_parser('lease-renew'); q.add_argument('--db',required=True); q.add_argument('--lease-id',required=True); q.add_argument('--holder-id',required=True); q.add_argument('--ttl',type=int,default=3600); q.set_defaults(func=cmd_lease_renew)
    q=sp.add_parser('lease-release'); q.add_argument('--db',required=True); q.add_argument('--lease-id',required=True); q.add_argument('--holder-id',required=True); q.set_defaults(func=cmd_lease_release)
    q=sp.add_parser('lease-check'); q.add_argument('--db',required=True); q.add_argument('--run-id',required=True); q.add_argument('--scope',required=True); q.set_defaults(func=cmd_lease_check)
    q=sp.add_parser('state-save'); q.add_argument('--state-dir',required=True); q.add_argument('--run-id',required=True); q.add_argument('--family-id',required=True); q.add_argument('--phase',required=True); q.add_argument('--plan-version',type=int,default=1); q.add_argument('--packet-version',type=int,default=1); q.add_argument('--dispatches'); q.add_argument('--findings'); q.add_argument('--lease'); q.set_defaults(func=cmd_state_save)
    q=sp.add_parser('state-load'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_state_load)
    q=sp.add_parser('approve-plan'); q.add_argument('--state-dir',required=True); q.add_argument('--approved-by',choices=['user'],required=True); q.add_argument('--quote',required=True); q.add_argument('--plan-path'); q.set_defaults(func=cmd_approve_plan)
    q=sp.add_parser('state-reconcile'); q.add_argument('--state-dir',required=True); q.add_argument('--db'); q.set_defaults(func=cmd_state_reconcile)
    q=sp.add_parser('mark-spoke'); q.add_argument('--state-dir',required=True); q.add_argument('--spoke',required=True); q.set_defaults(func=cmd_mark_spoke)
    q=sp.add_parser('check-spoke'); q.add_argument('--state-dir',required=True); q.add_argument('--spoke',required=True); q.set_defaults(func=cmd_check_spoke)
    q=sp.add_parser('route-defect'); q.add_argument('--state-dir',required=True); q.add_argument('--kind',choices=['invalid-invocation-slug','unsupported-effort','missing-adapter','other'],default='invalid-invocation-slug'); q.add_argument('--attempted',required=True); q.add_argument('--observed',required=True); q.add_argument('--correction'); q.add_argument('--harness'); q.set_defaults(func=cmd_route_defect)
    q=sp.add_parser('resolve-route-defect'); q.add_argument('--state-dir',required=True); q.add_argument('--id',required=True); q.add_argument('--proposal-ref',required=True); q.set_defaults(func=cmd_resolve_route_defect)
    q=sp.add_parser('check-route-defects'); q.add_argument('--state-dir',required=True); q.set_defaults(func=cmd_check_route_defects)
    args=p.parse_args(); sys.exit(args.func(args))

if __name__=='__main__': main()
