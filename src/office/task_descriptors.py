"""Per-task descriptors and calibrated benchmark evidence (#415).

No model-brand-specific routing rules. Unknown fields remain neutral.
"""
from __future__ import annotations
import math

OPTIONS = {
 "domain": ("ui", "backend", "data", "infrastructure", "mixed"),
 "work": ("mechanical", "implementation", "architecture"),
 "modality": ("code", "visual", "browser", "multimodal"),
 "quality": ("objective", "taste", "mixed"),
 "bounds": ("bounded", "open"),
 "task_size": ("S", "M", "L", "XL"),
}
# Evidence-only planner tags: persisted and snapshotted with the descriptor, but never read by
# dimensions(), benchmark_fit() or price_tier(). Omitted stays absent (unknown), never defaulted.
EVIDENCE_OPTIONS = {
 "evidence_domain": ("frontend", "backend", "infra", "docs", "tests"),
 "intent": ("feature", "fix", "refactor", "test-pruning", "migration"),
 "difficulty_estimate": ("low", "medium", "high", "very-high", "unknown"),
 "brief_shape": ("deliverables-enumerated", "checks-only", "unknown"),
}
TOKENS = ("estimated_input_tokens", "estimated_output_tokens")
PLAN_KEYS = frozenset((*OPTIONS, *EVIDENCE_OPTIONS, *TOKENS))
VERSION = "task-descriptor-1"


def parse_field(key: str, value: str):
    options = OPTIONS.get(key) or EVIDENCE_OPTIONS.get(key)
    if options:
        value = value.strip() if key == "task_size" else value.strip().lower()
        if value not in options:
            raise ValueError(f"{key} must be one of {', '.join(options)}")
        return value
    if key in TOKENS:
        if not value.isdecimal() or int(value) > 100000000:
            raise ValueError(f"{key} must be a nonnegative token count <= 100000000")
        return int(value)
    raise ValueError(f"unknown descriptor: {key}")


def bucket(descriptor: dict | None, threshold: int) -> str | None:
    n = (descriptor or {}).get("estimated_input_tokens")
    if type(n) is not int or n < 0:
        return None
    return "above" if n > threshold else "standard"


def price_tier(price: dict, descriptor: dict | None) -> dict:
    """Choose #413-compatible pricing; unknown prompt length uses higher tier."""
    price = price or {}
    info = {"tier": "flat", "estimated_input_tokens": (descriptor or {}).get("estimated_input_tokens"),
            "source_as_of": price.get("as_of"), "input_per_mtok": price.get("input_per_mtok"),
            "output_per_mtok": price.get("output_per_mtok")}
    threshold = price.get("prompt_token_threshold")
    if type(threshold) is int and threshold > 0:
        b = bucket(descriptor, threshold)
        info.update({"tier": ("unknown-conservative" if b is None else
                              "above-threshold" if b == "above" else "standard"),
                     "threshold": threshold})
        if b != "standard":
            info["input_per_mtok"] = price.get("above_threshold_input_per_mtok")
            info["output_per_mtok"] = price.get("above_threshold_output_per_mtok")
    return info


def dimensions(d: dict | None) -> dict:
    d = d or {}
    domain, work, modality = d.get("domain"), d.get("work"), d.get("modality")
    weights = {}
    if domain in ("backend", "data", "infrastructure"):
        weights["repo_engineering"] = 1.0
    elif domain == "ui":
        if d.get("quality") in ("taste", "mixed") or modality in ("visual", "multimodal"):
            weights["ui_appearance"] = 1.0
        if work in ("implementation", "mechanical"):
            weights["repo_engineering"] = 0.5
    elif domain == "mixed":
        weights["repo_engineering"] = 0.5
        if d.get("quality") in ("taste", "mixed"):
            weights["ui_appearance"] = 0.5
    if modality in ("browser", "multimodal"):
        weights["browser_interaction"] = 1.0
    if work == "architecture":
        weights["architecture"] = 1.0
        if "repo_engineering" in weights:
            weights["repo_engineering"] = min(weights["repo_engineering"], 0.5)
    return weights


def benchmark_fit(candidate: dict, descriptor: dict | None) -> dict:
    """Use only provenance-bearing calibrated scores; dedupe each family."""
    wanted = dimensions(descriptor)
    selected, ignored = {}, []
    for row in candidate.get("task_benchmarks") or []:
        if not isinstance(row, dict):
            ignored.append("malformed"); continue
        dim = row.get("dimension")
        if dim not in wanted:
            ignored.append(f"{dim}: unrelated"); continue
        required = ("benchmark_name", "benchmark_version", "source_url", "snapshot_date",
                    "normalization", "calibration_version", "model_id", "effort")
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in required) \
                or not row["source_url"].startswith("https://"):
            ignored.append(f"{dim}: uncalibrated or unversioned"); continue
        if row["model_id"] not in (candidate.get("model_id"), candidate.get("invocation_model_id")) \
                or row["effort"] != candidate.get("effort"):
            ignored.append(f"{dim}: wrong model or effort"); continue
        delta, confidence = row.get("calibrated_log_odds_delta"), row.get("confidence")
        if type(delta) not in (int, float) or type(confidence) not in (int, float) \
                or not math.isfinite(delta) or not math.isfinite(confidence) or not 0 < confidence <= 1:
            ignored.append(f"{dim}: invalid calibration"); continue
        if dim not in selected or confidence > selected[dim]["confidence"]:
            selected[dim] = {"dimension": dim, "benchmark": row["benchmark_name"],
                             "version": row["benchmark_version"], "calibration": row["calibration_version"],
                             "source_url": row["source_url"], "confidence": float(confidence),
                             "delta": float(delta)}
    weight = sum(wanted[k] * v["confidence"] for k, v in selected.items())
    delta = sum(wanted[k] * v["confidence"] * v["delta"] for k, v in selected.items()) / weight if weight else 0.0
    return {"dimensions": wanted, "applied": list(selected.values()), "ignored": ignored,
            "log_odds_adjustment": max(-0.75, min(0.75, delta))}


def apply_fit(base: float, fit: dict) -> float:
    delta = fit.get("log_odds_adjustment", 0)
    if not delta:
        return base
    base = max(0.0001, min(0.9999, base))
    return min(0.92, max(0.15, 1 / (1 + math.exp(-(math.log(base / (1 - base)) + delta)))))
