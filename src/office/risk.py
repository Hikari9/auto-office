"""Risk classification and the review floor it implies (#420), and the planner-declared lightweight path (#424).

A run's risk record is tri-state: `low` (an explicit local/repo blast radius, nothing high), `elevated` (any
high signal), or `unknown` (nothing declared). Absence is never low. Gear and mode tune ceremony; they cannot
drop independent code review below the floor `unknown` and `elevated` imply. The planner may classify an
unknown run in its plan's Requirements, and may declare the lightweight path for an explicitly low run.

The record lives in `runs.risk_json` (new keys only) and the effective gates in `gates_json`; both are
written once and restored on resume, never recomputed. A record without `classification` predates this
contract and keeps the gates it was started with."""
from __future__ import annotations

LOW, ELEVATED, UNKNOWN = "low", "elevated", "unknown"
LOW_BLAST = ("local", "repo")
# Preset values the lightweight declaration may drop. A literal `true` is the user's or the gear's firm choice.
_TUNABLE = ("risk_forced", "policy_optional", "shallow", "cost_bounded")
FLOOR_KEYS = ("code_review", "code_review_depth", "risk_classification", "review_basis", "review_floor")


def classification(risk: dict | None) -> str | None:
    """The stored classification, or None for a record that predates #420."""
    c = (risk or {}).get("classification")
    return c if c in (LOW, ELEVATED, UNKNOWN) else None


def classify(blast_radius: str | None, size_class: str | None, irreversible: bool, high: bool) -> dict:
    if high:
        c, basis = ELEVATED, "a high-risk signal (irreversible, production blast radius, or L/XL size)"
    elif blast_radius in LOW_BLAST:
        c, basis = LOW, f"explicit blast radius {blast_radius}"
    else:
        c, basis = UNKNOWN, "no blast radius declared (absence is not low risk)"
    given = bool(blast_radius or size_class or irreversible)
    return {"classification": c, "classification_basis": basis, "classified_by": "intake" if given else None}


def review_basis(gear: str, cls: str | None, code_review: bool, floor: bool) -> str:
    if cls is None:
        return "legacy run: gates as started"
    if floor:
        return (f"independent code review required: risk {cls} sets a review floor that gear {gear} "
                "does not fund on its own")
    if code_review:
        return f"independent code review required: funded by gear {gear} (risk {cls})"
    return f"independent code review not required: explicit {cls} risk and gear {gear} funds none"


def floor_gates(risk: dict, code_review) -> tuple:
    """(code_review value, floor_applied). Unknown or elevated risk never resolves to no review."""
    cls = classification(risk)
    if code_review or cls in (None, LOW):
        return code_review, False
    return "full", True


def planner_reclassify(risk: dict, req: dict) -> dict | None:
    """The risk record after a plan's Requirements classify it, or None when nothing changes.
    The planner may classify an unknown run and may raise any run to elevated; it may not lower what the
    user declared at intake."""
    if classification(risk) is None:
        return None
    blast = req.get("blast_radius")
    irreversible = str(req.get("irreversible") or "").strip().lower() in ("yes", "true", "1")
    size = req.get("size_class")
    if not (blast or irreversible or size):
        return None
    cur_by = risk.get("classified_by")
    merged = {**risk}
    if cur_by != "intake" or classification(risk) == UNKNOWN:
        merged["blast_radius"] = blast or risk.get("blast_radius")
        merged["size_class"] = size or risk.get("size_class")
        merged["irreversible"] = bool(risk.get("irreversible")) or irreversible
    else:
        # Intake classified it: only a raise is accepted.
        if blast in ("production", "production-data"):
            merged["blast_radius"] = blast
        if size in ("L", "XL"):
            merged["size_class"] = size
        merged["irreversible"] = bool(risk.get("irreversible")) or irreversible
    # Same thresholds as intake's defaults.
    merged["high"] = bool(risk.get("high")) or bool(merged["irreversible"]) or \
        merged.get("blast_radius") in ("production", "production-data") or merged.get("size_class") in ("L", "XL")
    merged.update(classify(merged.get("blast_radius"), merged.get("size_class"), merged["irreversible"], merged["high"]))
    if cur_by != "intake" or merged["classification"] != classification(risk):
        merged["classified_by"] = "intake" if cur_by == "intake" else "planner"
    else:
        merged["classified_by"] = cur_by
    return merged if merged != risk else None


def lightweight_problem(run: dict, risk: dict, declaration: dict | None) -> str | None:
    """Why a declared lightweight path is refused, or None when it is valid (or absent)."""
    if declaration is None:
        return None
    if not (declaration.get("rationale") or "").strip():
        return "`lightweight:` needs a one-line rationale: why this work is trivial and low risk"
    cls = classification(risk)
    if cls is None:
        return "this run predates risk classification, so it cannot take the lightweight path"
    if cls != LOW:
        return (f"risk is {cls} ({risk.get('classification_basis')}): the lightweight path needs an explicit low "
                "classification (`blast_radius: local` or `repo` in Requirements, nothing irreversible, size below L)")
    if run.get("gear") == "full":
        return "gear full is the full-ceremony path; pick a lighter gear or drop `lightweight:`"
    return None


def apply_lightweight(gates: dict, gear: str, config: dict, declaration: dict, plan_version: int,
                     plan_review_started: bool = False) -> dict:
    """Drop only the review a gear tunes (not one it firmly funds), and record the declaration and what it
    did. Scope, checks, tier-appropriate self-review, evidence and human landing authority are untouched."""
    preset = (config.get("gear_presets") or {}).get(gear, {})
    dropped = []
    out = {**gates}
    if preset.get("independent_code_review") in _TUNABLE and out.get("code_review"):
        out["code_review"], out["code_review_depth"] = False, "none"
        dropped.append("independent code review")
    if preset.get("plan_review") in _TUNABLE and out.get("plan_review") and not plan_review_started:
        out["plan_review"] = False
        dropped.append("plan review")
    out["lightweight"] = {"declared": True, "rationale": declaration["rationale"].strip(), "plan_version": plan_version,
                          "dropped": dropped,
                          "kept": ["explicit scope", "deterministic checks", "tier-appropriate self-review",
                                   "evidence receipt", "human landing authority"]}
    if dropped:
        out["review_basis"] = ("independent review not required: planner-declared lightweight path on explicit low "
                               f"risk ({declaration['rationale'].strip()[:120]}); dropped {', '.join(dropped)}")
    return out


def summary(run: dict) -> dict | None:
    """What route, review and landing receipts show: the effective classification and why review was or was not
    required. None for a run that predates #420."""
    risk, gates = run.get("risk") or {}, run.get("gates") or {}
    cls = classification(risk)
    if cls is None:
        return None
    return {"classification": cls, "basis": risk.get("classification_basis"), "classified_by": risk.get("classified_by"),
            "blast_radius": risk.get("blast_radius"), "independent_code_review": bool(gates.get("code_review")),
            "review_floor": bool(gates.get("review_floor")), "why": gates.get("review_basis"),
            "lightweight": gates.get("lightweight")}


def line(run: dict) -> str | None:
    s = summary(run)
    if s is None:
        return None
    by = f", classified by {s['classified_by']}" if s["classified_by"] else ""
    lw = f" | lightweight path: {s['lightweight']['rationale'][:80]}" if s.get("lightweight") else ""
    return f"risk {s['classification']}{by} | {s['why']}{lw}"
