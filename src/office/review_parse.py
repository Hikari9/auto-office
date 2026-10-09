"""Parse reviewer replies into a verdict plus findings.

A reply that does not follow the format is not a pass. It is an invalid
result, which the gate engine treats as an environment/adapter failure.

Two grammars, chosen by the run's pinned review contract (office.contract):
`parse(..., contract="v3.1")` keeps the v3.1 reply format byte for byte, so a
run started before #337 resumes and decodes exactly as it did. The
convergence contract's grammar is APPROVED | RECHECK | INTAKE_GAP with an
explicit blocking flag per finding (independent of its severity), a
recommended next action, and, for an intake gap, the user decision it needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

VERDICTS = ("PASS", "CHANGES_REQUIRED", "PLAN_DEFECT", "BRIEF_DEFECT", "UNAVAILABLE")
DEFECT_CLASSES = ("requirement-contradiction", "false-contract-assumption",
                  "unsafe-or-unauthorized-action", "double-scope-ownership")
EVIDENCE_STATUSES = ("COMPARABLE", "INVALID_COMPARISON", "NOT_APPLICABLE", "CAPTURE_BLOCKED")
# Finding severity word -> (blocking severity, level). After the round budget,
# only a `high` finding keeps a task from acceptance (gates._verify_only).
LEVELS = {"high": ("material", "high"), "medium": ("material", "medium"), "low": ("minor", "low"),
          "material": ("material", "high"), "minor": ("minor", "low")}
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
    # Convergence contract only.
    contract: str = "v3.1"
    next_action: str | None = None
    decision: str | None = None
    why: str | None = None
    affects: str | None = None

    @property
    def blocking(self) -> list[dict]:
        return [f for f in self.findings if f.get("blocking")]

    @property
    def valid(self) -> bool:
        return not self.errors


# What a TUI puts around a reply line when it is read off a pane: message
# bullets (`• `, `⏺ `), tree and box-drawing gutters, and check marks.
_LEAD_GLYPHS = "•●⏺◦▪▸►‣∙·○◆⎿└├│┃║▌▎▏╭╰✓✔"
_LEAD = re.compile(r"^(?:[-*>]\s+|[" + _LEAD_GLYPHS + r"]\s*)+")
_TRAIL = re.compile(r"\s*[│┃║▌▐╮╯]+\s*$")


# A finding id: letters then digits. `F-1` / `P-2` (a common reviewer spelling) are the
# same ids as `F1` / `P2`; `finding_code` normalizes them so a hyphen never drops a line.
_ID = r"([A-Za-z]+-?\d+)"


def finding_code(raw: str) -> str:
    return re.sub(r"^([A-Za-z]+)-(\d+)$", r"\1\2", raw.strip()).upper()


def _clean(line: str) -> str:
    line = line.strip().strip("`").strip()
    line = _LEAD.sub("", line)
    line = _TRAIL.sub("", line).strip().strip("`").strip()
    return line.replace("**", "")


# The reply grammar's record starters. A line that begins with none of them
# and follows a long line is the tail of a record a terminal wrapped.
# A record the grammar reads or complains about (the shapes parse() reacts to, malformed ones included), so a
# wrapped tail is never taken for one and a real record is never taken for a tail: `Finding the cause...` is a
# tail, `FINDING F2 no pipes` and a bare `FINDING` are records.
_ID_WORD = r"[A-Za-z]+-?\d+\b"
_RECORD = re.compile(r"^(?:(?:VERDICT|EVIDENCE_STATUS)\s*[:=]|(?:FINDING|DEFECT)(?:\s+" + _ID_WORD + r"|\s*\||\s*$)|"
                     r"(?:RESOLVED|CLEARED|RETRACT(?:ED)?)\s+\S|(?:NEXT|DECISION|WHY|AFFECTS)\s*[:=]?\s+\S)", re.I)
_JOINABLE = re.compile(r"^(?:(?:FINDING|DEFECT)(?:\s+" + _ID_WORD + r"|\s*\|)|RETRACT(?:ED)?\s+\S|"
                       r"(?:NEXT|DECISION|WHY|AFFECTS)\s*[:=]?\s+\S)", re.I)
_WRAP_MIN_WIDTH = 40
_SENTENCE_START = re.compile(r"[A-Z][a-z]")  # a capitalised word: the reviewer's next paragraph, not a tail

# Lines a harness TUI draws around a conversation and that are never a review:
# rules, the composer, key hints, token and context counters, usage footers.
_CHROME = re.compile(
    r"^(?:[─━═\-_=·\s]*"
    r"|[>❯›]\s*"
    r"|\??\s*for shortcuts"
    r"|.*\b(?:esc to (?:interrupt|cancel)|shift\+tab|ctrl\+[a-z]|press enter|bypass permissions|auto-accept)\b.*"
    r"|.*\bctx:\s*\d.*"
    r"|.*\b\d+%\s*(?:left|context)\b.*"
    r"|.*\b\d+(?:\.\d+)?\s*k?\s*tokens\b.*"
    r"|.*\bimprove documentation in\b.*"
    r")$", re.I)


def footer_only(text: str) -> bool:
    """True when every non-blank line is terminal chrome: a capture of the
    composer and its hints, not a reply."""
    lines = [_clean(raw) for raw in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return bool(lines) and all(_CHROME.match(ln) for ln in lines)


def join_wrapped(text: str) -> str:
    """Rejoin the records a terminal hard-wrapped. A pane read without the
    unwrapped source breaks every long line at the pane width; the tail of a
    FINDING, NEXT or DECISION record then lands on a line of its own, where the
    grammar would drop it or read it as a malformed record. A line continues
    the record above when it starts no record of its own and that record's line
    reaches the wrap width (the longest line of the text). A break inside a word
    leaves the line exactly at the width and is joined without a space; one that
    fell on a space (kept at the line's end, or the line a little short because
    the capture trimmed it) or a TUI's indented tail is joined with one."""
    raw = [ln.rstrip("\r") for ln in (text or "").split("\n")]
    filled = [ln for ln in raw if ln.strip()]
    if sum(1 for ln in filled if ln != ln.rstrip()) * 2 > len(filled):
        raw = [ln.rstrip() for ln in raw]  # a capture padded to the pane width: its trailing spaces mean nothing
    width = max((len(ln) for ln in raw), default=0)
    if width < _WRAP_MIN_WIDTH:
        return text
    # A wrapped text has several lines cut at the width; an unwrapped one has a single longest line, and what
    # follows it is the reviewer's next paragraph. With one cut, only a tail that continues a sentence joins.
    cut_lines = sum(1 for ln in raw if len(ln) >= width)

    def reach(line: str) -> str | bool:
        body = line.rstrip()
        if line != body and len(body) >= width * 0.8:
            return "space"  # the break fell on a space the capture kept
        if len(line) >= width:
            return "exact"
        return "space" if len(body) >= width * 0.8 else False

    out: list[str] = []
    last = -1  # index in `out` of the record a continuation would extend
    full: str | bool = False
    for line in raw:
        clean = _clean(line)
        if (clean and not _RECORD.match(clean) and last >= 0 and full and not _CHROME.match(clean)
                and (cut_lines > 1 or not _SENTENCE_START.match(clean))):
            glue = "" if full == "exact" and not line[:1].isspace() else " "
            out[last] = out[last].rstrip() + glue + clean
            full = reach(line)
            continue
        out.append(line)
        if clean and _JOINABLE.match(clean):
            last, full = len(out) - 1, reach(line)
        else:
            last, full = -1, False
    return "\n".join(out)


def normalize_verdict(raw: str) -> str | None:
    v = raw.strip().upper().replace("-", "_")
    v = _ALIASES.get(v, _ALIASES.get(v.replace("_", " "), v))
    return v if v in VERDICTS else None


def _template_verdict(value: str) -> bool:
    """`PASS | CHANGES_REQUIRED | ...`: the brief's format line echoed back
    (a log that carries the prompt), never a verdict."""
    words = [w for w in re.split(r"[\s|/,]+", value.upper().replace("-", "_")) if w]
    known = {w for w in words if w in CONVERGENCE_VERDICTS or normalize_verdict(w)}
    return "|" in value and len(known) > 1


def parse(text: str, *, plan_review: bool = False, visual: bool = False, contract: str | None = None) -> Parsed:
    from office import contract as contract_mod
    if (contract or contract_mod.LEGACY) == contract_mod.CONVERGENCE:
        return parse_convergence(text, visual=visual)
    out = Parsed()
    if not text or not text.strip():
        out.errors.append("empty reply")
        return out
    if footer_only(text):
        out.errors.append("reply holds only the terminal footer, no review")
        return out
    for raw in join_wrapped(text).splitlines():
        line = _clean(raw)
        if not line:
            continue
        m = re.match(r"^VERDICT\s*[:=]\s*(.+)$", line, re.I)
        if m:
            if _template_verdict(m.group(1)):
                out.errors.append(f"VERDICT line lists several verdicts ({m.group(1)[:60]!r}): that is the format, "
                                  "not a verdict")
                continue
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
        m = re.match(r"^FINDING\s+" + _ID + r"\s*\|(.*)$", line, re.I)
        if m:
            parts = [p.strip() for p in m.group(2).split("|")]
            word = (parts[0].lower() if parts else "")
            if word not in LEVELS:
                out.errors.append(f"{m.group(1)}: severity must be high, medium or low")
                word = "material"
            # `severity` keeps its blocking meaning (material|minor); `level` grades a
            # blocking finding. An older reviewer's bare `material` grades as high.
            severity, level = LEVELS[word]
            finding = {"code": finding_code(m.group(1)), "severity": severity, "level": level,
                       "location": parts[1] if len(parts) > 1 else "",
                       "summary": parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else ""),
                       "action": parts[3] if len(parts) > 3 else ""}
            if visual and len(parts) > 4:
                finding["measurement"] = " | ".join(parts[4:])
            if not finding["summary"]:
                out.errors.append(f"{finding['code']}: missing description")
            out.findings.append(finding)
            continue
        m = re.match(r"^DEFECT\s+" + _ID + r"\s*\|(.*)$", line, re.I)
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
            out.defects.append({"code": finding_code(m.group(1)), "category": cls,
                                "location": parts[1] if len(parts) > 1 else "",
                                "summary": parts[2] if len(parts) > 2 else "",
                                "action": " | ".join(parts[3:ev_at]) if ev_at is not None else "",
                                "evidence": evidence, "severity": "material", "level": "high"})
            continue
        m = re.match(r"^(RESOLVED|CLEARED)\s+" + _ID, line, re.I)
        if m:
            (out.resolved if m.group(1).upper() == "RESOLVED" else out.cleared).append(finding_code(m.group(2)))
            continue
        m = re.match(r"^RETRACT(?:ED)?\s+" + _ID + r"\s*\|?\s*(.*)$", line, re.I)
        if m:
            out.retracted.append({"code": finding_code(m.group(1)), "evidence": m.group(2).strip()})
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


# ------------------------------------------------------------------ convergence contract (#337)

CONVERGENCE_VERDICTS = ("APPROVED", "RECHECK", "INTAKE_GAP")
_BLOCKING = {"blocking": True, "block": True, "non-blocking": False, "nonblocking": False, "non_blocking": False,
             "advisory": False}
_LABELLED = re.compile(r"^(owner|owners|seam|root[-_ ]?cause|method)\s*:\s*(.*)$", re.I)


def parse_convergence(text: str, *, visual: bool = False) -> Parsed:
    """APPROVED | RECHECK | INTAKE_GAP. The rules the runtime relies on:

    - APPROVED carries no blocking finding and no finding whose repair crosses a
      hard seam (a seam repair is RECHECK, or INTAKE_GAP when it needs the user);
    - RECHECK names at least one blocking finding;
    - INTAKE_GAP names the user decision (DECISION), why requirements and repo
      evidence cannot settle it (WHY), and what it affects (AFFECTS);
    - every reply recommends a next action (NEXT). It is guidance, not a verdict.

    Severity never implies blocking: a high finding may be non-blocking and a
    low one blocking. A visual reply that judges the capture not comparable
    reports evidence state, not a verdict, and may omit VERDICT."""
    from office import contract
    out = Parsed(contract=contract.CONVERGENCE)
    if not text or not text.strip():
        out.errors.append("empty reply")
        return out
    if footer_only(text):
        out.errors.append("reply holds only the terminal footer, no review")
        return out
    for raw in join_wrapped(text).splitlines():
        line = _clean(raw)
        if not line:
            continue
        m = re.match(r"^VERDICT\s*[:=]\s*(.+)$", line, re.I)
        if m:
            if _template_verdict(m.group(1)):
                out.errors.append(f"VERDICT line lists several verdicts ({m.group(1)[:60]!r}): that is the format, "
                                  "not a verdict")
                continue
            word = (m.group(1).split() or [""])[0].strip().upper().replace("-", "_")
            if word not in CONVERGENCE_VERDICTS:
                out.errors.append(f"unknown verdict {m.group(1)!r}: use APPROVED, RECHECK or INTAKE_GAP")
            elif out.verdict and out.verdict != word:
                out.errors.append("conflicting verdicts")
            else:
                out.verdict = word
            continue
        m = re.match(r"^EVIDENCE_STATUS\s*[:=]\s*([A-Z_]+)", line, re.I)
        if m:
            status = m.group(1).upper()
            if status not in EVIDENCE_STATUSES:
                out.errors.append(f"unknown evidence status {status}")
            out.evidence_status = status
            continue
        m = re.match(r"^(NEXT|DECISION|WHY|AFFECTS)\s*[:=]?\s+(.+)$", line, re.I)
        if m:
            key = m.group(1).lower()
            setattr(out, "next_action" if key == "next" else key, m.group(2).strip())
            continue
        m = re.match(r"^FINDING\s+" + _ID + r"\s*\|(.*)$", line, re.I)
        if m:
            out.findings.append(_convergence_finding(finding_code(m.group(1)), m.group(2), out.errors))
            continue
        m = re.match(r"^DEFECT\s+" + _ID, line, re.I)
        if m:
            out.errors.append(f"{m.group(1)}: DEFECT lines are not part of this contract; write a FINDING "
                              "(root-cause: <class>) and choose RECHECK or INTAKE_GAP")
            continue
        m = re.match(r"^RESOLVED\s+" + _ID, line, re.I)
        if m:
            out.resolved.append(finding_code(m.group(1)))
            continue
        m = re.match(r"^RETRACT(?:ED)?\s+" + _ID + r"\s*\|?\s*(.*)$", line, re.I)
        if m:
            out.retracted.append({"code": finding_code(m.group(1)), "evidence": m.group(2).strip()})
            continue
        # A FINDING line this grammar cannot read is an error, never a silently
        # dropped finding (an unread blocking finding turns into "RECHECK without
        # a blocking finding"; an unread non-blocking one vanishes from the record).
        m = re.match(r"^FINDING\b\s*(\S*)", line, re.I)
        if m:
            out.errors.append(f"unreadable FINDING line (id {m.group(1)!r}): write "
                              "FINDING F1 | <severity> | <blocking|non-blocking> | <where> | <what> | <fix>")
    not_comparable = visual and out.evidence_status == "INVALID_COMPARISON"
    if visual and out.evidence_status is None:
        out.errors.append("no EVIDENCE_STATUS line")
    if out.verdict is None and not not_comparable:
        out.errors.append("no VERDICT line")
    if out.verdict is not None and not out.next_action:
        out.errors.append("no NEXT line (the recommended next action)")
    blocking = out.blocking
    seams = [f for f in out.findings if f.get("seam")]
    if out.verdict == "APPROVED":
        if blocking:
            out.errors.append(f"APPROVED with blocking finding(s) {', '.join(f['code'] for f in blocking)}: "
                              "use RECHECK, or mark them non-blocking")
        if seams:
            out.errors.append(f"APPROVED cannot carry a hard-seam repair ({', '.join(f['code'] for f in seams)}): "
                              "use RECHECK, or INTAKE_GAP when the repair needs a user decision")
    if out.verdict == "RECHECK" and not blocking:
        out.errors.append("RECHECK without a blocking finding")
    if out.verdict == "INTAKE_GAP":
        for key, label in (("decision", "DECISION <the smallest user-owned decision>"),
                           ("why", "WHY <why requirements and repo evidence cannot settle it>"),
                           ("affects", "AFFECTS <the tasks, lanes or plan sections it affects>")):
            if not getattr(out, key):
                out.errors.append(f"INTAKE_GAP without a {label} line")
    return out


def _convergence_finding(code: str, body: str, errors: list[str]) -> dict:
    """FINDING <id> | <high|medium|low> | <blocking|non-blocking> | <where> | <what is wrong> | <fix>
    [| owner: T1,T2] [| seam: <hard seam>] [| root-cause: <class>] [| method: ...]"""
    from office import contract
    parts = [p.strip() for p in body.split("|")]
    labelled, plain = {}, []
    for p in parts:
        m = _LABELLED.match(p)
        if m:
            key = re.sub(r"[-_ ]", "", m.group(1).lower())
            labelled["owners" if key in ("owner", "owners") else key] = m.group(2).strip()
        else:
            plain.append(p)
    severity = (plain[0].lower() if plain else "")
    if severity not in contract.SEVERITIES:
        errors.append(f"{code}: severity must be high, medium or low")
    block_word = (plain[1].lower().replace(" ", "-") if len(plain) > 1 else "")
    if block_word not in _BLOCKING:
        errors.append(f"{code}: say blocking or non-blocking after the severity")
    finding = {"code": code, "severity": severity, "level": severity, "blocking": _BLOCKING.get(block_word, True),
               "location": plain[2] if len(plain) > 2 else "", "summary": plain[3] if len(plain) > 3 else "",
               "action": " | ".join(plain[4:]) if len(plain) > 4 else ""}
    if not finding["summary"]:
        errors.append(f"{code}: missing description")
    owners = re.findall(r"\b(?:T|P)\d+\b", labelled.get("owners", ""), re.I)
    if owners:
        finding["owners"] = sorted({o.upper() for o in owners})
    seam = labelled.get("seam", "").lower().strip()
    if seam and seam not in ("none", "no", "-"):
        if seam not in contract.HARD_SEAMS:
            errors.append(f"{code}: seam must be one of {', '.join(contract.HARD_SEAMS)} (or none)")
        finding["seam"] = seam
    if labelled.get("rootcause"):
        finding["root_cause"] = labelled["rootcause"]
    if labelled.get("method"):
        finding["measurement"] = "method: " + labelled["method"]
    return finding
