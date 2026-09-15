#!/bin/sh
':' //; NODE_BIN="${HERDR_NODE_BIN:-$(command -v node 2>/dev/null || true)}"; if [ "$NODE_BIN" = "node" ] || [ ! -x "$NODE_BIN" ]; then NODE_BIN="$(type -P node 2>/dev/null || true)"; fi; if [ -z "$NODE_BIN" ]; then echo '{"error":"node not found"}' >&2; exit 1; fi; exec "$NODE_BIN" "$0" "$@"
/**
 * herdr-ledger: a small, worktree-local record of the panes one orchestrator
 * spawned, so it can be swept later without touching anyone else's panes.
 *
 * File: $HERDR_LEDGER, else $OFFICE_STATE_DIR/panes.jsonl.
 * One ledger per office run, living in that run's own state directory. Not
 * worktree-local, because a run's agents sit in several worktrees and each
 * would then see only its own spawns. Not a single global file either: a
 * shared /tmp path accumulates rows from every run that ever executed, so a
 * sweep has to reason about panes it has no business touching.
 * With no $OFFICE_STATE_DIR set there is no current run, and the ledger has
 * no meaningful scope -- the script exits rather than guessing one.
 * One JSON object per line, keyed by pane_id. Fields:
 *   pane_id, agent, kind, session_id, worktree, spawned_at,
 *   orchestrator_pane_id, orchestrator_session_id,
 *   status ("working"|"idle"|"blocked"|"done"|"gone"|"unknown"),
 *   suggestion (null|"closeable"|"reusable"|"compactable"),
 *   note, closed (bool), updated_at.
 *
 * Subcommands: add, update, list, sweep. See herdr-close-panes/SKILL.md for
 * the recipes each one backs.
 */
import { existsSync, readFileSync, writeFileSync, mkdirSync, renameSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";

const FINISHED = new Set(["done", "gone", "halted", "dead", "stopped", "exited", "terminated"]);
const SUGGESTIONS = new Set(["closeable", "reusable", "compactable", "keep"]);

function ledgerPath() {
  if (process.env.HERDR_LEDGER) return process.env.HERDR_LEDGER;
  const stateDir = process.env.OFFICE_STATE_DIR;
  if (!stateDir) {
    console.error(
      "herdr-ledger: no ledger scope. Set OFFICE_STATE_DIR to the current run's " +
      "state directory (office_runtime.py start returns it as state_dir), or set " +
      "HERDR_LEDGER explicitly. A pane ledger belongs to one run; there is no global one."
    );
    process.exit(2);
  }
  return join(stateDir, "panes.jsonl");
}

function readLedger(path) {
  if (!existsSync(path)) return [];
  const rows = [];
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try { rows.push(JSON.parse(trimmed)); } catch { /* drop malformed line */ }
  }
  return rows;
}

function writeLedger(path, rows) {
  mkdirSync(dirname(path), { recursive: true });
  if (!rows.length) {
    try { writeFileSync(path, ""); } catch { /* ignore */ }
    return;
  }
  const tmp = join(tmpdir(), `herdr-ledger.${process.pid}.jsonl`);
  writeFileSync(tmp, rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
  renameSync(tmp, path); // atomic: a concurrent reader never sees a half file
}

function herdr(args) {
  const parse = (s) => { try { return JSON.parse(s); } catch { return null; } };
  try {
    return parse(execFileSync("herdr", args, { encoding: "utf8", timeout: 8000, stdio: ["ignore", "pipe", "pipe"] }));
  } catch (e) {
    return parse(e?.stdout || "") || parse(e?.stderr || "") || null;
  }
}

function listItems(response, key) {
  if (!response || response.error) return null;
  const result = response.result;
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.[key])) return result[key];
  if (Array.isArray(response[key])) return response[key];
  return null;
}

function field(entry, ...names) {
  for (const name of names) {
    if (entry && entry[name] !== undefined && entry[name] !== null) return entry[name];
  }
  return null;
}

function parseFlags(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) out[key] = true;
      else { out[key] = next; i++; }
    }
  }
  return out;
}

function nowIso() { return new Date().toISOString(); }

function cmdAdd(flags) {
  const path = ledgerPath();
  const rows = readLedger(path);
  const paneId = flags.pane;
  if (!paneId) { console.error("herdr-ledger add: --pane is required"); process.exit(2); }
  const row = {
    pane_id: paneId,
    agent: flags.agent || null,
    kind: flags.kind || null,
    session_id: flags.session || null,
    worktree: flags.worktree || process.cwd(),
    spawned_at: nowIso(),
    // close_finished_panes.mjs reads recorded_at; office_spawn.sh writes it.
    recorded_at: nowIso(),
    orchestrator_pane_id: flags["orchestrator-pane"] || process.env.HERDR_PANE_ID || null,
    orchestrator_session_id: flags["orchestrator-session"] || process.env.HERDR_SESSION_ID || null,
    run_id: flags.run || process.env.OFFICE_RUN_ID || null,
    dispatch_id: flags.dispatch || null,
    status: "working",
    suggestion: null,
    note: null,
    closed: false,
    updated_at: nowIso(),
  };
  const kept = rows.filter((r) => field(r, "pane_id") !== paneId);
  kept.push(row);
  writeLedger(path, kept);
  console.log(JSON.stringify({ ledger: path, added: row }));
}

function cmdUpdate(flags) {
  const path = ledgerPath();
  const rows = readLedger(path);
  const paneId = flags.pane || process.env.HERDR_PANE_ID;
  if (!paneId) { console.error("herdr-ledger update: --pane is required (or set HERDR_PANE_ID)"); process.exit(2); }
  if (flags.suggestion && !SUGGESTIONS.has(flags.suggestion)) {
    console.error(`herdr-ledger update: --suggestion must be one of ${[...SUGGESTIONS].join("|")}`);
    process.exit(2);
  }
  let found = false;
  const next = rows.map((r) => {
    if (field(r, "pane_id") !== paneId) return r;
    found = true;
    const merged = { ...r };
    if (flags.status) merged.status = flags.status;
    if (flags.suggestion) merged.suggestion = flags.suggestion;
    if (flags.note !== undefined) merged.note = flags.note;
    if (flags.session) merged.session_id = flags.session;
    merged.updated_at = nowIso();
    return merged;
  });
  if (!found) { console.error(`herdr-ledger update: no ledger row for pane ${paneId}`); process.exit(1); }
  writeLedger(path, next);
  console.log(JSON.stringify({ ledger: path, updated: paneId }));
}

function cmdList(flags) {
  const path = ledgerPath();
  let rows = readLedger(path);
  if (flags.mine) {
    const mine = process.env.HERDR_PANE_ID;
    rows = rows.filter((r) => field(r, "orchestrator_pane_id") === mine);
  }
  console.log(JSON.stringify({ ledger: path, rows }, null, 2));
}

function cmdSweep(flags) {
  const path = ledgerPath();
  const rows = readLedger(path);
  const callerPane = process.env.HERDR_PANE_ID;
  if (!callerPane) {
    console.error("herdr-ledger sweep: HERDR_PANE_ID is not set (not a Herdr pane) — refusing to sweep");
    process.exit(2);
  }
  const dryRun = !!flags["dry-run"];

  const agents = listItems(herdr(["agent", "list"]), "agents");
  const panes = listItems(herdr(["pane", "list"]), "panes");
  if (!agents || !panes) {
    console.error("herdr-ledger sweep: could not read herdr agent/pane list — failing safe, closing nothing");
    process.exit(0);
  }

  const closed = [];
  const suggestions = [];
  const kept = [];

  for (const row of rows) {
    const mine = field(row, "orchestrator_pane_id") === callerPane;
    if (!mine) { kept.push(row); continue; }
    if (row.closed === true) continue; // already handled, drop silently

    const paneId = field(row, "pane_id");
    const expectedSession = field(row, "session_id");
    const agentMatch = agents.find((a) => field(a, "pane_id", "pane") === paneId);

    let effectiveStatus;
    if (agentMatch) {
      const actualSession = field(agentMatch, "session_id", "sessionId", "agent_session_id");
      if (expectedSession && actualSession && expectedSession !== actualSession) {
        // A different agent now occupies this pane id; ours moved elsewhere.
        effectiveStatus = "moved";
      } else {
        effectiveStatus = field(agentMatch, "agent_status", "status") || row.status;
      }
    } else if (expectedSession && agents.some((a) => field(a, "session_id", "sessionId", "agent_session_id") === expectedSession)) {
      // The session is alive but its recorded pane is no longer ours to close.
      effectiveStatus = "moved";
    } else {
      // No agent row means the recorded agent is gone. A remaining unknown pane
      // is the shell left behind, not proof of a live one.
      effectiveStatus = "gone";
    }

    const closeableNow =
      FINISHED.has(effectiveStatus) ||
      (row.suggestion === "closeable" && (effectiveStatus === "idle" || effectiveStatus === "done"));

    if (!closeableNow) {
      if (row.suggestion === "reusable" || row.suggestion === "compactable") {
        suggestions.push({ pane_id: paneId, agent: row.agent, suggestion: row.suggestion, note: row.note || null });
      }
      kept.push({ ...row, status: effectiveStatus });
      continue;
    }

    if (dryRun) {
      closed.push({ pane: paneId, agent: row.agent, status: effectiveStatus, session_id: row.session_id, dry_run: true });
      kept.push(row);
      continue;
    }

    let res = null;
    try { res = herdr(["pane", "close", paneId]); } catch { /* fall through to retain */ }
    const gone = res?.error?.code && /not_?found/.test(res.error.code);
    if ((res && !res.error) || gone) {
      closed.push({ pane: paneId, agent: row.agent, status: effectiveStatus, session_id: row.session_id });
    } else {
      kept.push({ ...row, status: effectiveStatus });
    }
  }

  if (!dryRun) writeLedger(path, kept);
  console.log(JSON.stringify({ ledger: path, closed, kept_open_with_suggestions: suggestions }, null, 2));
}

const [, , sub, ...rest] = process.argv;
const flags = parseFlags(rest);

switch (sub) {
  case "add": cmdAdd(flags); break;
  case "update": cmdUpdate(flags); break;
  case "list": cmdList(flags); break;
  case "sweep": cmdSweep(flags); break;
  default:
    console.error("usage: herdr-ledger.mjs <add|update|list|sweep> [--flags...]");
    process.exit(2);
}
