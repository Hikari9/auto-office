#!/usr/bin/env python3
"""Shadow-review scorecard (#1168): planted recall, blocker agreement with Luna, false positives, escapes.

    .venv/bin/python research/hydrogen-shadow/scorecard.py            # print and write SCORECARD.md

Inputs under research/hydrogen-shadow/:
  results/<case>/<reviewer>.json   written by shadow_review.py
  planted/<case>/truth.yaml        the seeded defect for planted cases (live PRs have none)
  dispositions.yaml                {case: {reviewer: {finding index: accept|reject}}}, set by the orchestrator
  escapes.yaml                     [{case: pr-N, summary: ..., missed_by: [luna, hydrogen]}]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BASELINE = "luna"
CANDIDATE = "hydrogen"
LINE_SLACK = 5
REGION_SLACK = 10
MIN_LIVE_PRS = 10
SERIOUS = ("blocker", "major")


def _yaml(path: Path, default):
    if not path.exists():
        return default
    return yaml.safe_load(path.read_text()) or default


def load(root: Path) -> dict:
    results: dict[str, dict[str, dict]] = {}
    for f in sorted((root / "results").glob("*/*.json")):
        results.setdefault(f.parent.name, {})[f.stem] = json.loads(f.read_text())
    truths = {f.parent.name: _yaml(f, {}) for f in sorted((root / "planted").glob("*/truth.yaml"))}
    return {"results": results, "truths": truths,
            "dispositions": _yaml(root / "dispositions.yaml", {}),
            "escapes": _yaml(root / "escapes.yaml", [])}


def _line(f: dict) -> int | None:
    try:
        return int(f.get("line"))
    except (TypeError, ValueError):
        return None


def matches_truth(finding: dict, truth: dict) -> str | None:
    """Why a finding counts as catching the planted defect, or None.

    The finding must name the truth's file, and then either point within LINE_SLACK lines of it
    or name one of its must_mention terms or keywords. A keyword in another file is not a catch."""
    if finding.get("file") != truth.get("file"):
        return None
    line = _line(finding)
    if line is not None and abs(line - int(truth["line"])) <= LINE_SLACK:
        return f"file and line within {LINE_SLACK}"
    summary = str(finding.get("summary") or "").lower()
    terms = list(truth.get("must_mention") or []) + list(truth.get("keywords") or [])
    hit = [k for k in dict.fromkeys(map(str, terms)) if k.lower() in summary]
    if hit:
        return f"same file, summary names {', '.join(hit)}"
    return None


def serious(rec: dict) -> list[dict]:
    return [f for f in rec.get("findings") or [] if str(f.get("severity")).lower() in SERIOUS]


def blocker_agree(a: dict, b: dict) -> bool:
    """Both block on the same file region, or neither blocks."""
    sa, sb = serious(a), serious(b)
    if not sa and not sb:
        return True
    for x in sa:
        for y in sb:
            lx, ly = _line(x), _line(y)
            if x.get("file") == y.get("file") and (lx is None or ly is None or abs(lx - ly) <= REGION_SLACK):
                return True
    return False


def disposition(data: dict, case: str, reviewer: str, i: int, finding: dict) -> str:
    marks = ((data["dispositions"].get(case) or {}).get(reviewer) or {})
    mark = marks.get(i, marks.get(str(i)))
    if mark in ("accept", "reject"):
        return mark
    truth = data["truths"].get(case)
    if truth and matches_truth(finding, truth):
        return "accept"
    return "pending"


def score(data: dict) -> dict:
    out: dict[str, dict] = {}
    reviewers = sorted({r for recs in data["results"].values() for r in recs})
    for r in reviewers:
        s = {"planted": 0, "caught": 0, "planted_failed": 0, "failed": [], "matches": [], "findings": 0, "rejected": 0, "pending": 0,
             "agree": 0, "compared": 0, "live_prs": 0, "escapes": 0}
        for case, recs in sorted(data["results"].items()):
            rec = recs.get(r)
            if rec is None:
                continue
            truth = data["truths"].get(case)
            if rec.get("status") != "ok":
                # A failed planted run is a miss: it stays in the recall denominator and blocks promotion.
                s["failed"].append(f"{case}: {rec.get('status')}")
                if truth:
                    s["planted"] += 1
                    s["planted_failed"] += 1
                continue
            if truth:
                s["planted"] += 1
                why = next(((i, w) for i, f in enumerate(rec.get("findings") or [])
                            if (w := matches_truth(f, truth))), None)
                if why:
                    s["caught"] += 1
                    s["matches"].append(f"{case} finding {why[0]}: {why[1]}")
            else:
                s["live_prs"] += 1
            for i, f in enumerate(rec.get("findings") or []):
                s["findings"] += 1
                d = disposition(data, case, r, i, f)
                s["rejected"] += d == "reject"
                s["pending"] += d == "pending"
            base = recs.get(BASELINE)
            if r != BASELINE and base and base.get("status") == "ok":
                s["compared"] += 1
                s["agree"] += blocker_agree(rec, base)
        s["escapes"] = sum(1 for e in data["escapes"] if r in (e.get("missed_by") or []))
        s["recall"] = s["caught"] / s["planted"] if s["planted"] else None
        s["fp_rate"] = s["rejected"] / s["findings"] if s["findings"] else 0.0
        s["agreement"] = s["agree"] / s["compared"] if s["compared"] else None
        out[r] = s
    return out


def promotion(scores: dict) -> tuple[bool, str]:
    b, c = scores.get(BASELINE), scores.get(CANDIDATE)
    if not b or not c:
        return False, "not enough data: both reviewers need results"
    failed = [r for r, s in ((BASELINE, b), (CANDIDATE, c)) if s.get("planted_failed")]
    if failed:
        return False, f"hold: a planted run failed for {', '.join(failed)}; re-run it"
    if c["live_prs"] < MIN_LIVE_PRS:
        return False, f"hold: {c['live_prs']} of {MIN_LIVE_PRS} live PRs shadow-reviewed"
    if c["pending"] or b["pending"]:
        return False, "hold: findings still need a disposition"
    if (c["recall"] or 0) < (b["recall"] or 0):
        return False, "do not promote: Hydrogen catches fewer planted bugs than Luna"
    if c["escapes"] > b["escapes"]:
        return False, "do not promote: more post-merge escapes than Luna"
    if c["fp_rate"] >= b["fp_rate"]:
        return False, "do not promote: false-positive rate is not lower than Luna's"
    return True, "promote: matches Luna on catches with fewer false alarms"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render(scores: dict) -> str:
    ok, why = promotion(scores)
    lines = ["# Hydrogen shadow-review scorecard (#1168)", "",
             "Generated by `scorecard.py`. Luna alone gates Office reviews; Hydrogen is a non-voting shadow.", "",
             "| Reviewer | Planted recall | Blocker agreement with Luna | False-positive rate | Findings | "
             "Pending | Live PRs | Escapes | Failed runs |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r, s in scores.items():
        agree = "baseline" if r == BASELINE else f"{_pct(s['agreement'])} ({s['agree']}/{s['compared']})"
        failed_note = f", {s['planted_failed']} failed" if s["planted_failed"] else ""
        lines.append(f"| {r} | {_pct(s['recall'])} ({s['caught']}/{s['planted']}{failed_note}) | {agree} | "
                     f"{_pct(s['fp_rate'])} ({s['rejected']}/{s['findings']}) | {s['findings']} | {s['pending']} | "
                     f"{s['live_prs']} | {s['escapes']} | {'; '.join(s['failed']) or 'none'} |")
    lines += ["", "Planted matches (check by hand):"]
    for r, s in scores.items():
        for m in s["matches"]:
            lines.append(f"- {r}: {m}")
    lines += ["", f"Promotion rule: promote only if Hydrogen planted recall >= Luna's, Hydrogen escapes <= Luna's "
                  f"and Hydrogen false-positive rate < Luna's, over the planted set plus at least {MIN_LIVE_PRS} "
                  f"live PRs with every finding dispositioned.",
              f"Decision now: {why}."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hydrogen shadow-review scorecard")
    ap.add_argument("--root", default=str(HERE))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    text = render(score(load(root)))
    print(text, end="")
    if not args.no_write:
        (root / "SCORECARD.md").write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
