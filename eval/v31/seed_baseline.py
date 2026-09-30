#!/usr/bin/env python3
"""Seed an isolated evaluation DB with the same explicit trust baseline for both versions.

Both runtimes refuse mutable/gate routes whose adapter trust is not `proven`, and
trust only rises through an attributed act. Every job gets an identical set of
acts (all dispatchable codex/claude rows present in both catalogs, under each
harness-version spelling an orchestrator may write), attributed to the user's
authorization of this evaluation. v3.1 also receives the steady-state vision
proofs from the live-conformance home so that each job does not re-probe.

    seed_baseline.py <v3|v31> <plugin_dir> <db_path> [<proofs_db>]
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

version, plugin, db = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
proofs = sys.argv[4] if len(sys.argv) > 4 else None
if version == "v3":
    sys.path.insert(0, str(plugin / "scripts"))
    subprocess.run([sys.executable, str(plugin / "scripts" / "office_runtime.py"), "init-db", "--db", db],
                   check=True, capture_output=True)
    import office_scoring as scoring
else:
    sys.path.insert(0, str(plugin / "src"))
    from office import db as office_db, scoring
    office_db.connect().close()

versions = {"codex": ["0.157.1", "codex-cli 0.157.1", "local"], "claude": ["2.1.283", "2.1.283 (Claude Code)", "local"]}
rows = yaml.safe_load((plugin / "catalog" / "seed.yaml").read_text())["models"]
acts = 0
for row in rows:
    harness = row.get("invocation_harness")
    if row.get("dispatchable") is False or harness not in versions:
        continue
    for hv in versions[harness]:
        for model in {row["model_id"], row.get("invocation_model_id") or row["model_id"]}:
            triple = f"{harness}@{hv}/{model}@{row.get('effort')}"
            scoring.record_trust_act(db, triple, "proven", "user",
                                     "evaluation baseline: the user authorized the v3.1 prospective evaluation "
                                     "(2026-09-27); identical acts are seeded for both versions")
            acts += 1
copied = 0
if proofs and Path(proofs).exists():
    con = sqlite3.connect(db)
    con.execute("ATTACH ? AS live", (proofs,))
    copied = con.execute("INSERT OR IGNORE INTO capability_proofs SELECT * FROM live.capability_proofs").rowcount
    con.commit()
print(f"trust acts {acts}, vision proofs {copied}")
