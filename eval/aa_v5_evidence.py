#!/usr/bin/env python3
"""AA v5.0.1 exact-effort research evidence. Only 'collect' makes HTTP requests."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "research" / "aa-v5.0.1"
EDITION = "Artificial Analysis Intelligence Index v5.0.1"
NUMERIC = ("intelligence_index_raw", "official_intelligence_index",
           "frontier_code2_percent", "terminal_bench4_percent", "engineering_index",
           "input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok",
           "cache_write_usd_per_mtok", "intelligence_task_time_seconds",
           "end_to_end_time_seconds", "output_speed_tokens_per_second")

class EvidenceError(ValueError):
    pass

class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocked = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.blocked += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.blocked:
            self.blocked -= 1
    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)

def _model_nodes(node, slug, into):
    if isinstance(node, dict):
        if node.get("slug") == slug and "frontierCode20" in node and "terminalBench40" in node:
            into.append(node)
        for value in node.values():
            _model_nodes(value, slug, into)
    elif isinstance(node, list):
        for value in node:
            _model_nodes(value, slug, into)

def extract_html(html, slug, effort):
    if EDITION not in html:
        raise EvidenceError("not explicitly AA v5.0.1")
    rows = []
    for script in re.findall(r"<script[^>]*>(self\.__next_f\.push\(.*?\))</script>", html, re.S):
        if slug not in script or "frontierCode20" not in script:
            continue
        try:
            envelope = json.loads(script[len("self.__next_f.push("):-1])
            payload = json.loads(envelope[1].split(":", 1)[1])
        except (ValueError, IndexError, TypeError):
            continue
        _model_nodes(payload, slug, rows)
    rows = [row for row in rows if isinstance(row.get("effort"), dict) and row["effort"].get("slug") == effort]
    if not rows:
        raise EvidenceError(f"no measured exact {slug}@{effort} on v5.0.1 page")
    # Repeated initialModels mentions are okay only when their actual measurements agree.
    fingerprints = {(x.get("intelligenceIndex"), x.get("frontierCode20"), x.get("terminalBench40"),
                     (x.get("capabilities") or {}).get("engineering")) for x in rows}
    if len(fingerprints) > 1:
        raise EvidenceError("conflicting measurements on page")
    m = rows[0]
    def pct(key):
        x = m.get(key)
        return round(100 * x, 6) if type(x) in (int, float) else None
    raw = m.get("intelligenceIndex")
    v = Visible()
    v.feed(html)
    visible = " ".join(" ".join(v.parts).split())
    speed = re.search(r"Output speed\s+([0-9.]+)\s*tok/s", visible)
    return {
        "page_model_slug": slug, "page_effort": effort,
        "engineering_index_edition": None,
        "engineering_index_edition_missing_reason": "AA does not expose separate Engineering Index revision in model payload; compare only same-date snapshots",
        "intelligence_index_raw": raw,
        "official_intelligence_index": math.floor(raw + 0.5) if type(raw) in (int, float) else None,
        "frontier_code2_percent": pct("frontierCode20"),
        "terminal_bench4_percent": pct("terminalBench40"),
        "engineering_index": (m.get("capabilities") or {}).get("engineering"),
        "input_usd_per_mtok": m.get("price1mInputTokens"),
        "output_usd_per_mtok": m.get("price1mOutputTokens"),
        "cache_read_usd_per_mtok": m.get("cacheHitPrice"),
        "cache_write_usd_per_mtok": m.get("cacheWritePrice"),
        "intelligence_task_time_seconds": m.get("intelligenceIndexTimePerTask"),
        "end_to_end_time_seconds": m.get("endToEndTime"),
        "output_speed_tokens_per_second": float(speed.group(1)) if speed else None,
        "price_source": m.get("priceDataSource"),
    }

def manifest(folder=FOLDER):
    data = json.loads((folder / "manifest.json").read_text())
    if data.get("research_only") is not True or data.get("index_version") != EDITION:
        raise EvidenceError("sidecar edition/authority mismatch")
    if data.get("production_index_unchanged") != "Artificial Analysis Intelligence Index v4.3.2":
        raise EvidenceError("v4 production guard missing")
    keys = set()
    for t in data["targets"]:
        key = t["route_key"]
        if not re.fullmatch(r"[a-z0-9-]+/[A-Za-z0-9_.-]+@(none|low|medium|high|xhigh|max)", key):
            raise EvidenceError(f"invalid exact route: {key}")
        if key in keys:
            raise EvidenceError(f"duplicate: {key}")
        keys.add(key)
        if t["role"] not in ("executor", "planner") or t["catalog_status"] not in ("in_catalog", "pending_pr_1142", "pending_pr_1133"):
            raise EvidenceError(f"unsupported identity: {key}")
        if "fable" in key and t["role"] != "planner":
            raise EvidenceError("Fable must remain planner-only")
    return data

def empty(t, version, reason):
    key, slug = t["route_key"], t.get("aa_slug")
    return {
        "route_key": key, "model_slug": slug, "role": t["role"],
        "catalog_status": t["catalog_status"], "index_version": version,
        "source_url": f"https://artificialanalysis.ai/models/{slug}" if slug else None,
        "source_html_sha256": None, "fetched_at": None, "confidence": "unverified",
        "page_model_slug": None, "page_effort": None, "price_source": None,
        "engineering_index_edition": None,
        "engineering_index_edition_missing_reason": "no independently versioned Engineering Index source",
        "missing_reasons": {k: reason for k in NUMERIC}, **{k: None for k in NUMERIC},
    }

def fetch_one(t, version):
    if not t.get("aa_slug"):
        return empty(t, version, t.get("missing_source_reason") or "no exact-effort source")
    r = empty(t, version, "source unavailable")
    try:
        with urlopen(Request(r["source_url"], headers={"User-Agent": "AutoOffice-v5-research/1.0"}), timeout=30) as resp:
            if resp.status != 200:
                raise EvidenceError(f"HTTP {resp.status}")
            raw = resp.read(6_000_001)
        if len(raw) > 6_000_000:
            raise EvidenceError("HTML exceeds 6 MB")
        vals = extract_html(raw.decode("utf-8"), t["aa_slug"], t["route_key"].rsplit("@", 1)[-1])
        r.update(vals)
        r["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        r["source_html_sha256"] = hashlib.sha256(raw).hexdigest()
        r["confidence"] = "official-public-page-exact-effort"
        r["missing_reasons"] = {k: "not published on exact-effort page" for k in NUMERIC if r.get(k) is None}
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        r["missing_reasons"] = {k: f"source unavailable: {type(exc).__name__}: {str(exc)[:160]}" for k in NUMERIC}
    return r

def rows(folder=FOLDER):
    return [json.loads(x) for x in (folder / "observations.jsonl").read_text().splitlines() if x.strip()]

def validate(folder=FOLDER, catalog=None):
    m = manifest(folder)
    obs = rows(folder)
    if len(obs) != len(m["targets"]) or [r["route_key"] for r in obs] != [t["route_key"] for t in m["targets"]]:
        raise EvidenceError("observations do not match manifest exactly")
    with ((catalog or ROOT / "catalog") / "routes.tsv").open() as fh:
        actual = {x["route_key"] for x in csv.DictReader(fh, delimiter="\t")}
    for t, r in zip(m["targets"], obs):
        key = t["route_key"]
        if (key in actual) != (t["catalog_status"] == "in_catalog"):
            raise EvidenceError(f"catalog presence drift {key}")
        if r["index_version"] != EDITION or r["role"] != t["role"] or r["model_slug"] != t["aa_slug"] or r["catalog_status"] != t["catalog_status"]:
            raise EvidenceError(f"model/role/index mismatch: {key}")
        if r.get("source_url"):
            u = urlparse(r["source_url"])
            if u.scheme != "https" or u.netloc != "artificialanalysis.ai" or u.path != f"/models/{t['aa_slug']}":
                raise EvidenceError(f"source URL not canonical: {key}")
        if not r.get("engineering_index_edition_missing_reason") and r.get("engineering_index_edition") is None:
            raise EvidenceError(f"unversioned Engineering Index without reason: {key}")
        if r["confidence"] == "official-public-page-exact-effort":
            if r.get("page_model_slug") != t["aa_slug"] or r.get("page_effort") != key.rsplit("@", 1)[-1]:
                raise EvidenceError(f"exact effort conflict: {key}")
            if not re.fullmatch(r"[a-f0-9]{64}", r.get("source_html_sha256") or ""):
                raise EvidenceError(f"source fingerprint missing: {key}")
            if datetime.fromisoformat(r["fetched_at"]).tzinfo is None:
                raise EvidenceError(f"missing timezone: {key}")
        elif r["confidence"] != "unverified":
            raise EvidenceError(f"unknown confidence: {key}")
        for field in NUMERIC:
            value = r.get(field)
            if value is None and not (r.get("missing_reasons") or {}).get(field):
                raise EvidenceError(f"null missing reason: {key}/{field}")
            if value is not None:
                if r["confidence"] != "official-public-page-exact-effort" or type(value) not in (int, float) or not math.isfinite(value):
                    raise EvidenceError(f"unsupported observed value: {key}/{field}")
                if field in ("frontier_code2_percent", "terminal_bench4_percent", "engineering_index", "official_intelligence_index") and not 0 <= value <= 100:
                    raise EvidenceError(f"out of range: {key}/{field}")
    return {"edition": EDITION, "rows": len(obs),
            "verified_model_effort": sum(x["confidence"] == "official-public-page-exact-effort" for x in obs),
            "missing_coding": sum(x["frontier_code2_percent"] is None or x["terminal_bench4_percent"] is None for x in obs)}


SHADOW_INDEX = "Auto Office-derived v5.0.1 coding FC2/TB4 60/40 (SHADOW ONLY)"

def export_shadow(folder=FOLDER):
    """TSV research sidecar, never a replacement for catalog/benchmarks.tsv."""
    validate(folder)
    fields=("route_key","role","research_index_label","official_aa_v5_headline",
            "raw_frontier_code2_percent","raw_terminal_bench4_percent","office_60_40_coding",
            "official_engineering_index_public_snapshot","source_url","source_html_sha256",
            "fetched_at","null_reason")
    target=folder/"coding-shadow.tsv"
    with target.open("w",newline="") as fh:
        writer=csv.DictWriter(fh,fieldnames=fields,delimiter="\t",lineterminator="\n")
        writer.writeheader()
        for r in rows(folder):
            fc,tb=r["frontier_code2_percent"],r["terminal_bench4_percent"]
            composite=round(.6*fc+.4*tb,6) if fc is not None and tb is not None else None
            nul=r.get("missing_reasons") or {}
            def cell(v):
                return r"\N" if v is None else v
            writer.writerow({
                "route_key":r["route_key"],"role":r["role"],
                "research_index_label":SHADOW_INDEX,
                "official_aa_v5_headline":cell(r["official_intelligence_index"]),
                "raw_frontier_code2_percent":cell(fc),
                "raw_terminal_bench4_percent":cell(tb),
                "office_60_40_coding":cell(composite),
                "official_engineering_index_public_snapshot":cell(r["engineering_index"]),
                "source_url":cell(r["source_url"]),
                "source_html_sha256":cell(r["source_html_sha256"]),
                "fetched_at":cell(r["fetched_at"]),
                "null_reason":nul.get("frontier_code2_percent") or nul.get("terminal_bench4_percent") or "",
            })
    return target

def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("collect", "validate", "summary", "export"))
    p.add_argument("--folder", type=Path, default=FOLDER)
    p.add_argument("--catalog", type=Path, default=ROOT / "catalog")
    a = p.parse_args()
    m = manifest(a.folder)
    if a.action == "collect":
        with ThreadPoolExecutor(max_workers=4) as pool:
            data = list(pool.map(lambda t: fetch_one(t, m["index_version"]), m["targets"]))
        (a.folder / "observations.jsonl").write_text("".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in data))
    stats = validate(a.folder, a.catalog)
    if a.action == "export":
        print("research-only TSV:", export_shadow(a.folder))
    if a.action == "summary":
        for x in rows(a.folder):
            fc, tb = x["frontier_code2_percent"], x["terminal_bench4_percent"]
            blend = round(.6*fc + .4*tb, 2) if fc is not None and tb is not None else None
            print(x["route_key"], "AA", x["official_intelligence_index"], "FC", fc, "TB", tb, "Office 60/40", blend, "AA Eng", round(x["engineering_index"],2) if x["engineering_index"] is not None else None, x["confidence"])
    print(json.dumps(stats, sort_keys=True))

if __name__ == "__main__":
    main()
