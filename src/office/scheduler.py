"""Scheduler policy: which queued work is admitted now, and why.

Pure functions over plain dicts; `office.queuecmd` reads and writes the
tables. Every entry carries its score components and one reason, so an
operator can see why work waits. Absent evidence is reported as such:
an unmeasured host does not gate admission, unknown quota is `unknown`
(never `ok`), and no concurrency figure is recommended without calibration.
"""
from __future__ import annotations

from datetime import datetime

PRIORITY_WEIGHTS = {"urgent": 100.0, "high": 50.0, "normal": 20.0, "low": 0.0}
CRITICAL_PATH_BOOST = 5.0  # per item that waits on this one
PROTECTED_BOOST = 1000.0
DEFAULTS = {"auto_mode": True, "aging_per_hour": 1.0, "cpu_pressure": 0.9, "ram_pressure": 0.9,
            "max_active_runs": None}
ACTIVE_STATES = ("running",)  # idle and paused agents hold no capacity


def settings(config: dict | None) -> dict:
    sched = (config or {}).get("scheduler", config or {})
    return {**DEFAULTS, **{k: v for k, v in (sched or {}).items() if k in DEFAULTS}}


def _hours(since: str | None, now: datetime) -> float:
    if not since:
        return 0.0
    try:
        start = datetime.fromisoformat(since)
    except ValueError:
        return 0.0
    return max(0.0, (now - start).total_seconds() / 3600)


def score(item: dict, conf: dict, now: datetime) -> dict:
    components = {
        "priority": PRIORITY_WEIGHTS.get(item.get("priority") or "normal", PRIORITY_WEIGHTS["normal"]),
        "aging": round(float(conf["aging_per_hour"]) * _hours(item.get("enqueued_at"), now), 3),
        "critical_path": CRITICAL_PATH_BOOST * int(item.get("blocks") or 0),
        "protected": PROTECTED_BOOST if item.get("protected") else 0.0,
    }
    return {"total": round(sum(components.values()), 3), "components": components}


def ready_order(entries: list[dict]) -> list[dict]:
    """Paused work sits at the end of the ready order, demoted work just before it."""
    return sorted(entries, key=lambda e: (bool(e.get("paused")), e.get("demoted_seq") is not None,
                                          e.get("demoted_seq") or 0, -e["score"]["total"],
                                          e.get("enqueued_at") or ""))


def _pressure(sample: dict | None, limit: float, name: str) -> dict:
    if not sample or sample.get("status") != "ok" or sample.get("value") is None:
        return {"status": "unmeasured", "gates": False, "reason": f"{name} unmeasured; not gating"}
    value = float(sample["value"])
    if value >= limit:
        return {"status": "pressure", "gates": True, "value": value,
                "reason": f"{name} pressure {value:g} >= {limit:g}"}
    return {"status": "ok", "gates": False, "value": value, "reason": f"{name} {value:g} < {limit:g}"}


def _quota(provider: str | None, quota: dict | None, reserve: float) -> dict:
    entry = (quota or {}).get(provider) if provider else None
    remaining = (entry or {}).get("remaining_percent")
    if remaining is None:
        return {"status": "unknown", "gates": False}
    if float(remaining) <= reserve:
        return {"status": "exhausted", "gates": True, "remaining_percent": remaining}
    return {"status": "ok", "gates": False, "remaining_percent": remaining}


def plan_admission(items: list[dict], active: list[dict], host: dict | None, quota: dict | None,
                   config: dict | None, now: datetime) -> dict:
    """Order `items` and decide admission for each.

    items: {id, priority, enqueued_at, blocks, protected, paused, demoted_seq,
            auto_mode ('on'|'paused'|'off'), provider}
    active: {id, state ('running'|'idle'|'paused')}; only running work holds capacity.
    host: {cpu: sample, ram: sample} from office.hostmetrics.
    quota: {provider: {remaining_percent}}; a provider absent here is unknown.
    """
    conf = settings(config)
    reserve = float(((config or {}).get("quota") or {}).get("reserve_percent", 5))
    cpu = _pressure((host or {}).get("cpu"), float(conf["cpu_pressure"]), "cpu")
    ram = _pressure((host or {}).get("ram"), float(conf["ram_pressure"]), "ram")
    running = sum(1 for a in active if a.get("state") in ACTIVE_STATES)
    cap = conf["max_active_runs"]
    entries = ready_order([{**item, "score": score(item, conf, now)} for item in items])
    admitted = 0
    for entry in entries:
        q = _quota(entry.get("provider"), quota, reserve)
        entry["quota"] = q["status"]
        decision, reason = "admit", "ready"
        if entry.get("paused"):
            decision, reason = "hold", "paused by operator"
        elif entry.get("protected"):
            decision, reason = "admit", "protected active orchestrator"
        elif entry.get("auto_mode", "on") != "on":
            decision, reason = "hold", f"auto mode {entry.get('auto_mode')}"
        elif cpu["gates"]:
            decision, reason = "hold", cpu["reason"]
        elif ram["gates"]:
            decision, reason = "hold", ram["reason"]
        elif q["gates"]:
            decision, reason = "hold", f"provider quota at or below the {reserve:g}% reserve"
        elif cap is not None and running + admitted >= int(cap):
            decision, reason = "hold", f"max_active_runs {cap} reached"
        notes = [n["reason"] for n in (cpu, ram) if n["status"] == "unmeasured"]
        if q["status"] == "unknown":
            notes.append("quota unknown; not gating")
        if decision == "admit" and not entry.get("protected"):
            admitted += 1
        entry["decision"], entry["reason"], entry["notes"] = decision, reason, notes
    return {"entries": entries, "active": running,
            "host": {"cpu": cpu["status"], "ram": ram["status"]}}
