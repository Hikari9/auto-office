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
 * gone (the agent has disappeared from `herdr agent list`). An idle agent may
 * have dropped its prompt, so it stays open until completion is confirmed. A closed pane is not lost work — the
 * ledger records each agent's session id, so a session is restored by id in a
 * fresh pane. Continuity lives in the session id and the agent's written report, never
 * in a pane left open after confirmed completion.
 *
 * Never closable: an agent that is `working`, `idle`, `blocked`, or `unknown`; a pane
 * whose agent has since moved to a different pane than the ledger recorded; and
 * anything not in the ledger.
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
 */
import { existsSync, readFileSync, writeFileSync, renameSync, unlinkSync, accessSync, constants } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";

const LEDGER = process.env.OFFICE_PANE_LEDGER || join("/tmp", "office", "panes.jsonl");
const FINISHED = new Set(["done", "gone", "halted", "dead", "stopped", "exited", "terminated"]);
const CURRENT_RUN_ID = process.env.OFFICE_RUN_ID || null;
const CURRENT_SESSION_ID = process.env.OFFICE_SESSION_ID || process.env.HERDR_SESSION_ID || null;

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

const closed = [];
try {
  await drainStdin(); // the hook payload is unused; not reading it can block the caller
  if (!existsSync(LEDGER)) process.exit(0);
  if (!onPath("herdr")) process.exit(0);

  const agents = listItems(herdr(["agent", "list"]), "agents");
  const panes = listItems(herdr(["pane", "list"]), "panes");
  if (!agents || !panes) process.exit(0); // no liveness evidence: fail safe

  const raw = readFileSync(LEDGER, "utf8");
  const lines = raw.split("\n").filter((l) => l.trim());
  if (!lines.length) process.exit(0);

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
