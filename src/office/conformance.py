"""Route capability conformance.

A visual reviewer route qualifies only after its exact harness, harness
version, model, effort and adapter configuration answered an image-sensitive
probe correctly, in a separate session, in the required structured format.
The result is cached under a key that changes whenever any of those change.
Model family names and vendor documentation grant nothing.
"""
from __future__ import annotations

import random
import struct
import uuid
import zlib
from pathlib import Path

from office import adapters, db, paths, routing, state
from office.util import now_iso, sha256_bytes, sha256_file

# 5x7 bitmap digits.
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}
COLORS = {"red": (220, 30, 30), "green": (30, 170, 60), "blue": (30, 70, 220)}


def key(cand: dict, adapter: dict, capability: str) -> str:
    return "|".join([cand.get("harness") or "", cand.get("harness_version") or "", cand.get("invocation_model_id") or "",
                     cand.get("effort") or "", adapters.adapter_hash(adapter), capability])


def proof_status(con, cand: dict, adapter: dict, capability: str) -> str | None:
    row = con.execute("SELECT result FROM capability_proofs WHERE key=?", (key(cand, adapter, capability),)).fetchone()
    return row["result"] if row else None


def write_png(path: Path, width: int, height: int, pixel) -> None:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def probe_image(path: Path, digits: str, color: str) -> None:
    scale, pad = 12, 24
    width, height = pad * 2 + len(digits) * 6 * scale + 180, pad * 2 + 7 * scale
    rgb = COLORS[color]
    sq_x0 = width - pad - 140

    def pixel(x, y):
        if sq_x0 <= x < sq_x0 + 120 and pad <= y < pad + 7 * scale:
            return rgb
        gx, gy = (x - pad) // scale, (y - pad) // scale
        if 0 <= gy < 7 and x >= pad:
            idx, col = divmod(gx, 6)
            if idx < len(digits) and col < 5 and _FONT[digits[idx]][gy][col] == "1":
                return (0, 0, 0)
        return (255, 255, 255)

    write_png(path, width, height, pixel)


def probe_vision(con, run: dict, cand: dict, adapter: dict) -> dict:
    """Run one image-sensitive probe on this exact route; record the outcome."""
    from office import dispatch as dispatch_mod, review_parse
    digits = "".join(random.choice("0123456789") for _ in range(4))
    color = random.choice(list(COLORS))
    ddir = paths.run_dir(run["id"]) / "conformance" / uuid.uuid4().hex[:8]
    ddir.mkdir(parents=True, exist_ok=True)
    image = ddir / "probe.png"
    probe_image(image, digits, color)
    brief = (
        "ROLE visual conformance probe (read-only).\n"
        "Look at the attached image. It shows a four-digit number in black and a filled square.\n"
        "Reply with ONLY these three lines:\n"
        "PROBE <the four digits> <the square's color: red, green, or blue>\n"
        "EVIDENCE_STATUS: COMPARABLE\n"
        "VERDICT: PASS\n"
        "If you cannot see an image, reply exactly: PROBE NO_IMAGE\n")
    (ddir / "brief.md").write_text(brief, encoding="utf-8")
    dispatch_id = "D" + uuid.uuid4().hex[:8]
    with db.transaction(con):
        con.execute("INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, started_at, kind, "
                    "office_version, status, harness, model, effort, adapter_id, route_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dispatch_id, run["id"], "visual_reviewer", dispatch_id, routing.candidate_id(cand),
                     cand.get("invocation_model_id"), now_iso(), "conformance", run["office_version"], "launching",
                     cand["harness"], cand.get("invocation_model_id"), cand.get("effort"), cand.get("adapter_id"),
                     "{}"))
    d = state.get_dispatch(con, dispatch_id)
    out = ddir / "reply.txt"
    dispatch_mod.launch(run, d, "vision", ddir, cwd=ddir, wait=True, output=out, images=[image], include_dirs=[ddir])
    d = state.get_dispatch(con, dispatch_id)
    text = out.read_text(errors="replace") if out.is_file() and out.stat().st_size else ""
    if not text and d.get("log_path") and Path(d["log_path"]).is_file():
        text = Path(d["log_path"]).read_text(errors="replace")
    import re
    m = re.findall(r"PROBE\s+(\d{4})\s+(red|green|blue)", text, re.I)
    seen = m[-1] if m else None
    structured = review_parse.parse(text[text.rfind("PROBE"):] if "PROBE" in text else text, visual=True)
    ok = bool(seen and seen[0] == digits and seen[1].lower() == color and structured.valid
              and d.get("terminal_classification") == "success")
    result = "pass" if ok else "fail"
    detail = (f"expected {digits} {color}; got {' '.join(seen) if seen else 'no answer'}; "
              f"exit {d.get('exit_code')}; structured={'ok' if structured.valid else ','.join(structured.errors[:2])}")
    with db.transaction(con):
        con.execute("INSERT OR REPLACE INTO capability_proofs(key, harness, harness_version, model, effort, adapter_hash, capability, "
                    "result, evidence_hash, details, proved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (key(cand, adapter, "vision"), cand["harness"], cand.get("harness_version"), cand.get("invocation_model_id"),
                     cand.get("effort"), adapters.adapter_hash(adapter), "vision", result, sha256_file(image), detail, now_iso()))
        state.emit(con, run, "conformance", f"vision probe {routing.candidate_id(cand)}: {result}", audience="runtime",
                   payload={"detail": detail})
    return {"result": result, "detail": detail, "triple": routing.candidate_id(cand)}


def ensure_vision_route(con, run: dict, config: dict) -> None:
    """Probe the preferred visual routes in order until one proves image
    capability. Stops at the first pass; never marks a route without a probe."""
    from office import candidates
    all_adapters = adapters.load_all()
    cands, _ = candidates.build_candidates(con, "visual_reviewer", probe=False)
    seed = (candidates.role_policy(config, "visual_reviewer").get("preferred_seed") or [])
    ordered = sorted(cands, key=lambda c: (routing.preferred_rank(c, seed) if routing.preferred_rank(c, seed) is not None
                                           else len(seed), routing.candidate_id(c)))
    for cand in ordered[:4]:
        adapter = all_adapters[cand["adapter_id"]]
        status = proof_status(con, cand, adapter, "vision")
        if status == "pass":
            return
        if status == "fail":
            continue
        if probe_vision(con, run, cand, adapter)["result"] == "pass":
            return
