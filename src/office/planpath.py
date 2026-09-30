"""Where a run's plan draft lives: .office/plans/<run>/PLAN.md.

The draft is per run so a new run in the same checkout never sees an older
run's plan. The draft is only a buffer; submitted versions live in runs.db, so
the draft is removed when the run ends.

A legacy .office/PLAN.md is moved to the draft of the run whose stored plan it
matches, or to .office/plans/legacy-<date>.md when none does.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from office.util import now_iso, sha256_bytes, short

LEGACY = Path(".office") / "PLAN.md"


def rel(run: dict) -> str:
    """The draft path relative to the worktree, for briefs and next steps."""
    return f".office/plans/{short(run['id'])}/PLAN.md"


def draft(root: Path, run: dict) -> Path:
    return Path(root) / rel(run)


def relocate_legacy(con, root: Path) -> str | None:
    """Move a legacy .office/PLAN.md out of the shared path. Returns a warning
    line when it moved something."""
    legacy = Path(root) / LEGACY
    if not legacy.is_file():
        return None
    text = legacy.read_text(encoding="utf-8")
    from office import planfile
    row = con.execute("SELECT run_id FROM plans WHERE content_hash=? ORDER BY created_at DESC LIMIT 1",
                      (sha256_bytes(planfile.strip_generated(text).encode()),)).fetchone()
    if row is not None:
        dest = draft(root, {"id": row["run_id"]})
        if dest.exists():
            dest = dest.with_name(f"PLAN.legacy-{now_iso()[:10]}.md")
    else:
        dest = Path(root) / ".office" / "plans" / f"legacy-{now_iso()[:10]}.md"
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"legacy-{now_iso()[:10]}-{n}.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    legacy.rename(dest)
    return f"moved legacy .office/PLAN.md to {dest.relative_to(root)}; plans are now per run"


def remove(root: Path, run: dict) -> None:
    shutil.rmtree(draft(root, run).parent, ignore_errors=True)
