#!/bin/sh
':' //; NODE_BIN="${OFFICE_NODE_BIN:-$(command -v node 2>/dev/null || true)}"; if [ "$NODE_BIN" = "node" ] || [ ! -x "$NODE_BIN" ]; then NODE_BIN="$(type -P node 2>/dev/null || true)"; fi; if [ -z "$NODE_BIN" ]; then echo "Node.js not found; close_finished_panes is a no-op." >&2; exit 0; fi; "$NODE_BIN" "$0" "$@"; exit $?
/**
 * Stop hook. Closes the Herdr panes of delegated agents that have finished.
 *
 * Why a hook and not a rule: the Herdr skill has said for
 * several releases that a dispatch is finished when its pane is gone, and
 * panes still accumulated one dead agent per dispatch because closing depended
 * on the planner choosing to notice. This closes them mechanically, from the
 * ledger the spawn recipe writes.
 *
 * Stop, not PostToolUse: a delegated agent has just reported when the turn ends,
 * which is exactly the moment its pane became closable. PostToolUse would run
 * this on every tool call for no additional closures.
 *
 * It closes ONLY panes recorded in the ledger. `herdr agent list` and
 * `herdr pane list` are the liveness source of truth; the ledger is the
 * ownership boundary. The pane listing also shows the user's own panes and
 * other sessions' panes, so the listing is never the candidate set.
 *
 * Closable, with no role exceptions: `done`, an explicit halted/dead status, or
 * gone (the agent has disappeared from `herdr agent list`) — but a reported
 * status alone is a *candidate* for closure, never proof of it (issue 93 / plan
 * v2 finding F9: `herdr agent prompt --wait` and the `agent_status` field have
 * both been observed reporting a settled/done state while the pane was still
 * actively working). Before closing, this hook additionally requires a durable,
 * independently corroborated terminal completion event for the ledger entry's
 * `dispatch_id` in `${OFFICE_STATE_DIR:-.office}/events/completions.jsonl`,
 * written by `scripts/office_monitor.py` with `source` in `monitor_bridge`
 * (multiple consistent samples) or `process_exit` (the OS's own exit code) —
 * never a raw, single `source: "herdr"` read, which is recorded only for
 * replay visibility and is never trusted alone. No event, or a dispatch_id not
 * in the log yet, means no independent evidence yet: the pane stays open. An
 * idle agent may have dropped its prompt, so it stays open until completion is
 * confirmed. A closed pane is not lost work — the ledger records each agent's
 * session id, so a session is restored by id in a fresh pane. Continuity lives
 * in the session id and the agent's written report, never in a pane left open
 * after confirmed completion.
 *
 * Never closable: an agent that is `working`, `idle`, `blocked`, or `unknown`; a pane
 * whose agent has since moved to a different pane than the ledger recorded;
 * anything not in the ledger; and a reported-finished pane with no durable,
 * independently corroborated terminal event backing it.
 *
 * Installed by `scripts/hooks/install_hooks.sh` as a Stop hook. The runtime
 * guard below is kept regardless — Herdr being present at install time does not
 * mean it is present at run time, and v3 dispatches are not all pane-hosted.
 *
 * The ledger is written by `scripts/office_spawn.sh --pane-id`. A dispatch that
 * does not name a pane never appears here and is never closed, even if the
 * agent list later reports that pane as finished.
 *
 * Contract, same as the eval hooks: never blocks, exits 0 on any internal error,
 * and prints nothing when it closed nothing. A hygiene hook that can fail a turn
 * is worse than a dead pane.
 *
 * v3.1 run ledgers (safety net). The 3.1 runtime closes a dispatch pane itself
 * once it has an accepted result: it writes the pane's final text, appends
 * `{result: "accepted", snapshot}` to `<state root>/runs/<run_id>/panes.jsonl`,
 * closes the pane, then appends `{closed_at}`. If the runtime died between the
 * accepted row and the close, this hook finishes the job. Runs are found from
 * the repo's `.office/active/<run_id>` pointers (never the legacy v3
 * `.office/runs/*.ref`). Per pane the latest launch row starts a fresh record;
 * a pane closes only with an accepted row whose snapshot file exists and is
 * non-empty, no later `kept` or `closed_at` row, and an explicit run and
 * dispatch identity. The caller's own pane is never closed, and a herdr outage
 * closes nothing.
 */
import { existsSync, readFileSync, writeFileSync, renameSync, unlinkSync, accessSync, constants, readdirSync,
  statSync, appendFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir, homedir } from "node:os";
import { join, delimiter, resolve } from "node:path";

const LEDGER = process.env.OFFICE_PANE_LEDGER || join(process.env.OFFICE_STATE_DIR || ".office", "panes.jsonl");
const FINISHED = new Set(["done", "gone", "halted", "dead", "stopped", "exited", "terminated"]);
const CURRENT_RUN_ID = process.env.OFFICE_RUN_ID || null;
const CURRENT_SESSION_ID = process.env.OFFICE_SESSION_ID || process.env.HERDR_SESSION_ID || null;
const STATE_DIR = process.env.OFFICE_STATE_DIR || ".office";
// Sources office_monitor.py treats as independently corroborated, not a raw
// single reported status. Keep in sync with TRUSTED_SOURCES in
// scripts/office_monitor.py.
const TRUSTED_EVENT_SOURCES = new Set(["monitor_bridge", "process_exit"]);

/**
 * Latest completion event for `dispatchId` that is both terminal
 * (`terminal_classification !== "non_terminal"`) and from a trusted,
 * independently corroborated source. Returns null on any missing/unreadable
 * state — a hook that cannot find durable evidence must fail closed (keep the
 * pane), never treat "I couldn't check" as "it's done".
 */
const latestTrustedTerminalEvent = (dispatchId) => {
  if (!dispatchId) return null;
  const path = join(STATE_DIR, "events", "completions.jsonl");
  if (!existsSync(path)) return null;
  let raw;
  try { raw = readFileSync(path, "utf8"); } catch { return null; }
  let best = null;
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let e;
    try { e = JSON.parse(trimmed); } catch { continue; }
    if (e.dispatch_id !== dispatchId) continue;
    if (e.terminal_classification === "non_terminal") continue;
    if (!TRUSTED_EVENT_SOURCES.has(e.source)) continue;
    if (!best || (e.sequence || 0) > (best.sequence || 0)) best = e;
  }
  return best;
};

/** herdr on PATH? Outside a Herdr environment this hook is a no-op. */
const onPath = (bin) => {
  for (const dir of (process.env.PATH || "").split(delimiter)) {
    if (!dir) continue;
    try { accessSync(join(dir, bin), constants.X_OK); return true; } catch { /* keep looking */ }
  }
  return false;
};

/**
 * Every herdr CLI call answers JSON, but a failure exits 1 and puts its
 * `{"error":{"code":...}}` body on **stderr**, not stdout. Reading only stdout
 * turns a closed or already-gone pane into "CLI unreachable", and the ledger
 * entry never gets retired.
 */
const herdr = (args) => {
  const parse = (s) => { try { return JSON.parse(s); } catch { return null; } };
  try {
    return parse(execFileSync("herdr", args, { encoding: "utf8", timeout: 8000, stdio: ["ignore", "pipe", "pipe"] }));
  } catch (e) {
    return parse(e?.stdout || "") || parse(e?.stderr || "") || null;
  }
};

const listItems = (response, key) => {
  if (!response || response.error) return null;
  const result = response.result;
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.[key])) return result[key];
  if (Array.isArray(response[key])) return response[key];
  return null;
};

const field = (entry, ...names) => {
  for (const name of names) {
    if (entry && entry[name] !== undefined && entry[name] !== null) return entry[name];
  }
  return null;
};

const sessionIdentity = (entry) => field(entry, "session_id", "sessionId", "agent_session_id");

const ownsLedgerEntry = (entry) => {
  const runId = field(entry, "run_id", "office_run_id");
  const sessionId = sessionIdentity(entry);
  // A pane is owned only when the ledger carries an explicit run/session
  // identity; display names are never an ownership signal.
  if (!runId && !sessionId) return false;
  if (CURRENT_RUN_ID && runId !== CURRENT_RUN_ID) return false;
  if (CURRENT_SESSION_ID && sessionId !== CURRENT_SESSION_ID) return false;
  return true;
};

const liveness = (pane, expectedSessionId, agents, panes) => {
  const agentMatches = agents.filter((entry) => field(entry, "pane_id", "pane") === pane);
  const agent = agentMatches[0] || null;

  if (agent) {
    const actualSessionId = sessionIdentity(agent);
    if (expectedSessionId && actualSessionId && expectedSessionId !== actualSessionId) {
      return { status: "moved", paneEntry: null };
    }
    const status = field(agent, "agent_status", "status");
    const paneEntry = panes.find((entry) => field(entry, "pane_id", "pane") === pane) || null;
    const paneStatus = field(paneEntry, "agent_status", "status");
    return { status: FINISHED.has(status) ? status : (FINISHED.has(paneStatus) ? paneStatus : status), paneEntry };
  }

  if (expectedSessionId && agents.some((entry) => sessionIdentity(entry) === expectedSessionId)) {
    // The session moved: its recorded pane is no longer ours to close.
    return { status: "moved", paneEntry: null };
  }
  // No agent row means the recorded agent is gone. A remaining unknown pane is
  // the shell/terminal left behind by that dead agent, not proof of a live one.
  return { status: "gone", paneEntry: panes.find((entry) => field(entry, "pane_id", "pane") === pane) || null };
};

const drainStdin = () =>
  new Promise((res) => {
    let b = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (b += c));
    process.stdin.on("end", () => res(b));
    process.stdin.on("error", () => res(b));
    setTimeout(() => res(b), 2000).unref();
  });

/** Where `paths.state_home()` (src/office/paths.py) puts run directories. */
const stateHome = () => {
  if (process.env.OFFICE_STATE_HOME) return resolve(process.env.OFFICE_STATE_HOME.replace(/^~(?=\/|$)/, homedir()));
  const xdg = process.env.XDG_STATE_HOME ? process.env.XDG_STATE_HOME.replace(/^~(?=\/|$)/, homedir())
    : join(homedir(), ".local", "state");
  return resolve(xdg, "auto-office");
};

const readRows = (path) => {
  let raw;
  try { raw = readFileSync(path, "utf8"); } catch { return []; }
  const rows = [];
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    try { rows.push(JSON.parse(line)); } catch { /* skip malformed */ }
  }
  return rows;
};

const nonEmptyFile = (p) => {
  try { return Boolean(p) && statSync(p).isFile() && statSync(p).size > 0; } catch { return false; }
};

/**
 * Per pane, the record since its latest launch row. A launch row is one with a
 * dispatch identity and none of result/kept/closed_at (a reused pane id starts
 * a fresh record). Rows naming only a dispatch are attributed to that
 * dispatch's pane.
 */
const paneRecords = (rows) => {
  const byPane = new Map();
  const paneOfDispatch = new Map();
  for (const r of rows) {
    const pane = r.pane_id || (r.dispatch_id && paneOfDispatch.get(r.dispatch_id)) || null;
    if (!pane) continue;
    if (r.dispatch_id && r.pane_id) paneOfDispatch.set(r.dispatch_id, r.pane_id);
    const isClose = Boolean(r.closed_at) || r.closed === true;
    const isKept = r.kept === true;
    const isAccepted = r.result === "accepted";
    if (!isClose && !isKept && !isAccepted) {
      if (r.dispatch_id) byPane.set(pane, { launch: r, accepted: null, kept: false, closed: false });
      continue;
    }
    const rec = byPane.get(pane) || { launch: null, accepted: null, kept: false, closed: false };
    if (isAccepted) rec.accepted = r;
    if (isKept) rec.kept = true;
    if (isClose) rec.closed = true;
    byPane.set(pane, rec);
  }
  return byPane;
};

/** Close accepted, snapshotted v3.1 dispatch panes the runtime did not get to. */
const sweepRunLedgers = (closedOut) => {
  const activeDir = join(process.cwd(), ".office", "active");
  let runIds;
  try { runIds = readdirSync(activeDir).filter((n) => !n.startsWith(".")); } catch { return; }
  if (!runIds.length || !onPath("herdr")) return;
  const panes = listItems(herdr(["pane", "list"]), "panes");
  if (!panes) return; // herdr unreachable: no liveness evidence, close nothing
  const self = process.env.HERDR_PANE_ID || null;
  for (const runId of runIds) {
    const ledger = join(stateHome(), "runs", runId, "panes.jsonl");
    if (!existsSync(ledger)) continue;
    for (const [pane, rec] of paneRecords(readRows(ledger))) {
      if (!rec.accepted || rec.kept || rec.closed) continue;
      if (self && pane === self) continue;
      const acc = rec.accepted;
      const rid = acc.run_id || rec.launch?.run_id || null;
      const did = acc.dispatch_id || rec.launch?.dispatch_id || null;
      if (!rid || !did || rid !== runId) continue;
      if (!nonEmptyFile(acc.snapshot)) continue;
      const present = panes.some((p) => field(p, "pane_id", "pane") === pane);
      if (present) {
        herdr(["pane", "close", pane]);
        const probe = herdr(["pane", "get", pane]);
        if (!(probe?.error?.code && /not_?found/.test(probe.error.code))) continue; // still open: retry next Stop
      }
      try {
        appendFileSync(ledger, JSON.stringify({ pane_id: pane, run_id: rid, dispatch_id: did,
          closed_at: new Date().toISOString(), closed_by: "stop_hook" }) + "\n");
      } catch { /* the close happened; the next sweep sees the pane gone */ }
      if (present) closedOut.push({ pane, name: rec.launch?.agent || did, status: "accepted", session: rec.launch?.session_id || null });
    }
  }
};

const closed = [];
const sweepLegacyLedger = () => {
  if (!existsSync(LEDGER)) return;
  if (!onPath("herdr")) return;

  const agents = listItems(herdr(["agent", "list"]), "agents");
  const panes = listItems(herdr(["pane", "list"]), "panes");
  if (!agents || !panes) return; // no liveness evidence: fail safe

  const raw = readFileSync(LEDGER, "utf8");
  const lines = raw.split("\n").filter((l) => l.trim());
  if (!lines.length) return;

  const kept = [];
  for (const line of lines) {
    let e;
    try { e = JSON.parse(line); } catch { continue; } // malformed: drop, it names no pane we can act on
    const displayName = e?.agent || e?.name;
    const pane = e?.pane_id;
    if (!pane) continue;
    // A planner that closed explicitly and marked the entry instead of removing
    // it: drop it, and do not announce a close that already happened.
    if (e.closed === true) continue;
    if (!ownsLedgerEntry(e)) { kept.push(e); continue; }
    const live = liveness(pane, sessionIdentity(e), agents, panes);
    if (!FINISHED.has(live.status)) { kept.push(e); continue; }

    // A reported terminal status is a candidate, never proof: require a
    // durable, independently corroborated terminal event for this dispatch
    // before treating the reported status as verified completion. This is
    // the agy false-done gate (issue 93 / plan v2 finding F9) — without it, a
    // single unreliable `done`/`gone` read is enough to reclaim a pane whose
    // agent is still working.
    const durableEvent = latestTrustedTerminalEvent(e?.dispatch_id || null);
    if (!durableEvent) { kept.push(e); continue; }

    // Preserve the ledger session id; a gone agent no longer appears in either
    // list, and the id is the only way back into this session.
    const session = sessionIdentity(e);

    let res = null;
    try { res = herdr(["pane", "close", pane]); } catch { /* continue with other panes */ }
    const gone = res?.error?.code && /not_?found/.test(res.error.code);
    if ((res && !res.error) || gone) closed.push({ pane, name: displayName || "unnamed", status: live.status, session });
    else kept.push(e); // close failed: retain it and continue with other panes
  }

  if (kept.length !== lines.length) {
    if (!kept.length) {
      unlinkSync(LEDGER);
    } else {
      const tmp = join(tmpdir(), `office-panes.${process.pid}.jsonl`);
      writeFileSync(tmp, kept.map((x) => JSON.stringify(x)).join("\n") + "\n");
      renameSync(tmp, LEDGER); // atomic: a concurrent reader never sees a half file
    }
  }
};

try {
  await drainStdin(); // the hook payload is unused; not reading it can block the caller
  try { sweepLegacyLedger(); } catch { /* independent of the run-ledger pass */ }
  try { sweepRunLedgers(closed); } catch { /* swallow, see contract */ }
} catch {
  // Swallow. See contract above.
}

if (closed.length) {
  console.log(
    `closed ${closed.length} finished Herdr pane(s): ` +
      closed.map((c) => `${c.pane} ${c.name} (${c.status}${c.session ? `, session ${c.session}` : ""})`).join(", ")
  );
}
process.exit(0);
