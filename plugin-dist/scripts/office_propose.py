#!/usr/bin/env python3
"""Dream compilation and idempotent proposal append (protocol/privacy-self-improvement.md).

Three steps, each a separate subcommand so each one's output is inspectable:

  compile-dream  private rows (runs.db + this run's state dir) -> sanitized dream JSON
  render         dream JSON -> public proposal markdown
  append         proposal markdown -> the standing proposal branch, idempotently

`append` is the deliverable the lifecycle actually depends on: replaying the same dream must
append nothing new. It is keyed by the deterministic identity hash from
`office_runtime.py proposal-id`, so a second run with the same input is a no-op rather than a
duplicate -- which is what makes a standing branch survive concurrent runs.

The append refuses material that still carries private text. Privacy lint is a gate here, not a
report: a proposal that fails it is never written to the public branch.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import office_runtime as rt  # noqa: E402

SCHEMA_VERSION = 1

# Deterministic redaction. Order matters: paths before the bare-name pass, so a repo name inside
# an absolute path is removed by the path rule rather than half-replaced by the name rule.
_REDACTIONS = [
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "<email>"),
    (re.compile(r"https?://[^\s)\]}>]+", re.I), "<url>"),
    (re.compile(r"(?<![\w.])/(?:Users|home|var|opt|srv|private|Volumes)/[^\s'\")]+"), "<path>"),
    (re.compile(r"\b[A-Z]:\\(?:[^\s\\]+\\)+[^\s]+", re.I), "<path>"),
    (re.compile(r"\b(?:token|api[_-]?key|secret|password)\s*[:=]\s*[^\s]{6,}", re.I), "<credential>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
]


def sanitize(text):
    """Remove the classes protocol/privacy-self-improvement.md names, deterministically."""
    if text is None:
        return ""
    out = str(text)
    for rx, replacement in _REDACTIONS:
        out = rx.sub(replacement, out)
    return out


def digest(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _read_route_defects(state_dir):
    path = Path(state_dir) / "route-defects.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _read_findings(db, family_id):
    """Findings for one family, or every finding when no family is named.

    The family predicate is a real filter: a finding whose dispatch has no run row, or no
    dispatch at all, belongs to no family and is therefore NOT in a family-scoped dream. An
    earlier `OR r.family_id IS NULL` disjunct admitted exactly those rows, so a scoped dream
    published occurrence counts inflated by other families' evidence.
    """
    if not db or not Path(db).exists():
        return []
    con = sqlite3.connect(db)
    try:
        sql = ("SELECT f.id, f.status, f.severity, f.summary FROM findings f "
               "LEFT JOIN dispatches d ON d.id = f.dispatch_id "
               "LEFT JOIN runs r ON r.id = d.run_id ")
        params = ()
        if family_id is not None:
            sql += "WHERE r.family_id = ? "
            params = (family_id,)
        cur = con.execute(sql + "ORDER BY f.id", params)
        return [dict(zip(("id", "status", "severity", "summary"), row)) for row in cur.fetchall()]
    finally:
        con.close()


def compile_dream(db, state_dir, run_id, family_id=None):
    """Private rows in, sanitized dream out. The run id never appears in the result."""
    defects = _read_route_defects(state_dir)
    findings = _read_findings(db, family_id)

    patterns = []

    by_harness = {}
    for d in defects:
        by_harness.setdefault(d.get("harness") or "unknown", []).append(d)
    for harness, rows in sorted(by_harness.items()):
        if len(rows) < 2:
            continue
        # Two defects on one harness is the signal: a single wrong slug is a typo, a second one
        # with the same shape is the harness accepting an under-specified identity.
        patterns.append({
            "pattern_id": "route-identity-silently-defaulted",
            "stream": "catalog-policy",
            "statement": (
                f"Harness '{sanitize(harness)}' accepted an under-specified routed identity and "
                "silently substituted a default instead of failing. A route notice therefore "
                "published an identity the dispatch did not run under."
            ),
            "occurrences": len(rows),
            "kinds": sorted({sanitize(r.get("kind")) for r in rows}),
            "correction_shape": "pass every routed dimension explicitly; treat a silent default as a route defect",
            "evidence_hashes": sorted(digest("route-defect", r.get("id")) for r in rows),
        })

    # The vocabulary lives in the STATUS column, not severity: review_finding.sh writes
    # status="accepted-material" with severity in critical/high/medium/low, and office_scoring
    # reads it the same way. Selecting on severity == "material" matched a value nothing in this
    # repo ever writes, so this branch was dead against every real recorder row.
    material = [f for f in findings if f.get("status") == "accepted-material"]
    if material:
        patterns.append({
            "pattern_id": "gate-satisfiable-by-excluded-evidence",
            "stream": "learned-pattern",
            "statement": (
                "Review found gates that admitted exactly the evidence they existed to exclude. "
                "Each repair was correct and left a narrower instance of the same hole, so the "
                "class survived consecutive fixes. Enumerate every required property of the "
                "evidence at once rather than patching the found instance."
            ),
            "occurrences": len(material),
            "kinds": sorted({sanitize(f.get("severity") or "unspecified") for f in material}),
            "correction_shape": "enumerate the rejecting cases and test each one",
            "evidence_hashes": sorted(digest("finding", f.get("id")) for f in material),
        })

    dream = {
        "schema_version": SCHEMA_VERSION,
        "source_counts": {"route_defects": len(defects), "findings": len(findings)},
        # Opaque: only this machine can map it back, by hashing a run id it already holds.
        "lineage_digest": digest("run", run_id),
        "patterns": patterns,
    }
    return dream


def render(dream):
    lines = [
        "# Self-improvement proposal",
        "",
        f"Schema version: {dream['schema_version']}",
        f"Lineage digest: `{dream['lineage_digest']}`",
        "Source counts: " + ", ".join(f"{k}={v}" for k, v in sorted(dream["source_counts"].items())),
        "",
        "Compiled from private run telemetry by deterministic sanitization. Evidence is referenced",
        "by opaque hash; the originating rows stay local.",
        "",
    ]
    for p in dream["patterns"]:
        lines += [
            f"## {p['pattern_id']} ({p['stream']})",
            "",
            p["statement"],
            "",
            f"- Occurrences: {p['occurrences']}",
            f"- Kinds: {', '.join(p['kinds']) if p['kinds'] else 'n/a'}",
            f"- Correction shape: {p['correction_shape']}",
            "- Evidence: " + ", ".join(f"`{h[:16]}`" for h in p["evidence_hashes"]),
            "",
        ]
    return "\n".join(lines)


def identity_hash(dream):
    """Same identity as office_runtime.py proposal-id, over the dream rather than the prose.

    Keyed on the dream, not the rendered markdown, so a wording change to render() does not
    create a second copy of a proposal that is already on the branch.
    """
    obj = {"stream": "learned-pattern", "kind": "dream", "payload": dream}
    return rt.sha256_obj(obj)


def append(branch_dir, dream, proposal_text, db=None):
    """Write the proposal onto the standing branch unless its identity is already there."""
    findings = rt.privacy_findings(proposal_text)
    if findings:
        return {"appended": False, "reason": "privacy_lint_failed",
                "findings": [f["kind"] for f in findings]}

    ident = identity_hash(dream)
    root = Path(branch_dir)
    proposals = root / "proposals"
    proposals.mkdir(parents=True, exist_ok=True)
    # The identity carries a `sha256:` prefix; a colon is a legal path character here but not on
    # every platform a proposal branch gets checked out on, so the file is named by the digest.
    target = proposals / f"{ident.split(':')[-1]}.md"
    index = root / "PROPOSALS.md"

    if target.exists():
        return {"appended": False, "reason": "identity_already_present", "identity_hash": ident,
                "path": str(target)}

    target.write_text(proposal_text, encoding="utf-8")
    header = "" if index.exists() else "# Standing proposals\n\n"
    with index.open("a", encoding="utf-8") as fh:
        fh.write(header + f"- `{ident}` lineage `{dream['lineage_digest'][:16]}` "
                          f"({len(dream['patterns'])} patterns)\n")

    if db:
        _record_lineage(db, ident, dream["lineage_digest"])
    return {"appended": True, "identity_hash": ident, "path": str(target)}


def _record_lineage(db, ident, lineage_digest):
    con = sqlite3.connect(db)
    try:
        row_id = digest("lineage", ident)
        exists = con.execute("SELECT 1 FROM lineage WHERE id = ?", (row_id,)).fetchone()
        if exists:
            return
        con.execute(
            "INSERT INTO lineage(id, component_kind, component_id, parent_id, event, multiplier, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (row_id, "proposal", ident, lineage_digest, "appended", 1.0,
             rt.datetime.now(rt.timezone.utc).isoformat()))
        con.commit()
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("compile-dream")
    c.add_argument("--db")
    c.add_argument("--state-dir", required=True)
    c.add_argument("--run-id", required=True)
    c.add_argument("--family-id")
    c.add_argument("--out")

    r = sp.add_parser("render")
    r.add_argument("--dream", required=True)
    r.add_argument("--out")

    a = sp.add_parser("append")
    a.add_argument("--dream", required=True)
    a.add_argument("--proposal", required=True)
    a.add_argument("--branch-dir", required=True)
    a.add_argument("--db")

    args = ap.parse_args(argv)

    if args.cmd == "compile-dream":
        dream = compile_dream(args.db, args.state_dir, args.run_id, args.family_id)
        text = json.dumps(dream, indent=2, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "render":
        dream = json.loads(Path(args.dream).read_text(encoding="utf-8"))
        text = render(dream)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    dream = json.loads(Path(args.dream).read_text(encoding="utf-8"))
    proposal_text = Path(args.proposal).read_text(encoding="utf-8")
    result = append(args.branch_dir, dream, proposal_text, db=args.db)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("appended") or result.get("reason") == "identity_already_present" else 3


if __name__ == "__main__":
    sys.exit(main())
