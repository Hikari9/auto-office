"""Parse reviewer replies into a verdict plus findings.

A reply that does not follow the format is not a pass. It is an invalid
result, which the gate engine treats as an environment/adapter failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

VERDICTS = ("PASS", "CHANGES_REQUIRED", "PLAN_DEFECT", "BRIEF_DEFECT", "UNAVAILABLE")
DEFECT_CLASSES = ("requirement-contradiction", "false-contract-assumption",
                  "unsafe-or-unauthorized-action", "double-scope-ownership")
EVIDENCE_STATUSES = ("COMPARABLE", "INVALID_COMPARISON", "NOT_APPLICABLE", "CAPTURE_BLOCKED")
_ALIASES = {"CHANGES REQUIRED": "CHANGES_REQUIRED", "PLAN DEFECT": "PLAN_DEFECT", "BRIEF DEFECT": "BRIEF_DEFECT",
            "APPROVED": "PASS", "ACCEPTED": "PASS", "IMPLEMENTATION_DEFECT": "CHANGES_REQUIRED",
            "IMPLEMENTATION DEFECT": "CHANGES_REQUIRED"}


@dataclass
class Parsed:
    verdict: str | None = None
    evidence_status: str | None = None
    findings: list[dict] = field(default_factory=list)
    defects: list[dict] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    retracted: list[dict] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _clean(line: str) -> str:
    line = line.strip().strip("`").strip()
    line = re.sub(r"^[-*>]\s+", "", line)
    return line.replace("**", "")


def normalize_verdict(raw: str) -> str | None:
    v = raw.strip().upper().replace("-", "_")
    v = _ALIASES.get(v, _ALIASES.get(v.replace("_", " "), v))
    return v if v in VERDICTS else None


def parse(text: str, *, plan_review: bool = False, visual: bool = False) -> Parsed:
    out = Parsed()
    if not text or not text.strip():
        out.errors.append("empty reply")
        return out
    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        m = re.match(r"^VERDICT\s*[:=]\s*(.+)$", line, re.I)
        if m:
            v = normalize_verdict(m.group(1).split()[0] if m.group(1).split() else "")
            if v is None:
                v = normalize_verdict(m.group(1))
            if v is None:
                out.errors.append(f"unknown verdict {m.group(1)!r}")
            elif out.verdict and out.verdict != v:
                out.errors.append("conflicting verdicts")
            else:
                out.verdict = v
            continue
        m = re.match(r"^EVIDENCE_STATUS\s*[:=]\s*([A-Z_]+)", line, re.I)
        if m:
            status = m.group(1).upper()
            if status not in EVIDENCE_STATUSES:
                out.errors.append(f"unknown evidence status {status}")
            out.evidence_status = status
            continue
        m = re.match(r"^FINDING\s+([A-Za-z]+\d+)\s*\|(.*)$", line, re.I)
        if m:
            parts = [p.strip() for p in m.group(2).split("|")]
            severity = (parts[0].lower() if parts else "")
            if severity not in ("material", "minor"):
                out.errors.append(f"{m.group(1)}: severity must be material or minor")
                severity = "material"
            finding = {"code": m.group(1).upper(), "severity": severity,
                       "location": parts[1] if len(parts) > 1 else "",
                       "summary": parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else ""),
                       "action": parts[3] if len(parts) > 3 else ""}
            if visual and len(parts) > 4:
                finding["measurement"] = " | ".join(parts[4:])
            if not finding["summary"]:
                out.errors.append(f"{finding['code']}: missing description")
            out.findings.append(finding)
            continue
        m = re.match(r"^DEFECT\s+([A-Za-z]+\d+)\s*\|(.*)$", line, re.I)
        if m:
            parts = [p.strip() for p in m.group(2).split("|")]
            cls = parts[0].lower() if parts else ""
            # The brief's form is <class> | <task> | <what is wrong> | <evidence: ...>; an older
            # form put an action before the evidence. Evidence is the labelled field, else the last.
            ev_at = next((i for i, p in enumerate(parts) if i >= 3 and re.match(r"evidence\b", p, re.I)), None)
            if ev_at is None and len(parts) >= 4:
                ev_at = len(parts) - 1
            evidence = re.sub(r"^evidence\s*:\s*", "", parts[ev_at], flags=re.I) if ev_at is not None else ""
            if cls not in DEFECT_CLASSES:
                out.errors.append(f"{m.group(1)}: defect class {cls!r} is not in the closed list")
            if not evidence:
                out.errors.append(f"{m.group(1)}: a defect must cite evidence")
            out.defects.append({"code": m.group(1).upper(), "category": cls,
                                "location": parts[1] if len(parts) > 1 else "",
                                "summary": parts[2] if len(parts) > 2 else "",
                                "action": " | ".join(parts[3:ev_at]) if ev_at is not None else "",
                                "evidence": evidence, "severity": "material"})
            continue
        m = re.match(r"^(RESOLVED|CLEARED)\s+([A-Za-z]+\d+)", line, re.I)
        if m:
            (out.resolved if m.group(1).upper() == "RESOLVED" else out.cleared).append(m.group(2).upper())
            continue
        m = re.match(r"^RETRACT(?:ED)?\s+([A-Za-z]+\d+)\s*\|?\s*(.*)$", line, re.I)
        if m:
            out.retracted.append({"code": m.group(1).upper(), "evidence": m.group(2).strip()})
    if out.verdict is None:
        out.errors.append("no VERDICT line")
    material = [f for f in out.findings if f["severity"] == "material"]
    if out.verdict == "PASS" and (material or out.defects):
        out.errors.append("PASS with open material findings")
    if out.verdict == "CHANGES_REQUIRED" and not material and not out.defects:
        out.errors.append("CHANGES_REQUIRED without a material finding")
    if out.verdict == "PLAN_DEFECT":
        if not plan_review:
            out.errors.append("PLAN_DEFECT is a plan-review verdict")
        if not out.defects:
            out.errors.append("PLAN_DEFECT without a DEFECT line")
    if visual and out.evidence_status is None:
        out.errors.append("no EVIDENCE_STATUS line")
    return out
