#!/usr/bin/env node
/**
 * Stop hook. Closes the Herdr panes of delegated agents that have finished.
 *
 * Why a hook and not a rule: the Herdr skill has said for
 * several releases that a dispatch is finished when its pane is gone, and
 * panes still accumulated one dead agent per dispatch, because closing depended
 * on the planner choosing to notice. This closes them mechanically, from the
 * ledger the spawn recipe writes.
 *
 * Stop, not PostToolUse: a delegated agent has just reported when the turn ends,
 * which is exactly the moment its pane became closable. PostToolUse would run
 * this on every tool call for no additional closures.
 *
 * It closes ONLY panes recorded in the ledger. `herdr pane list` also shows the
 * user's own panes and other sessions' panes, so the listing is never the input.
 *
 * Closable, with no role exceptions: `done` or gone (herdr answers
 * `agent_not_found`). An idle agent may have dropped its prompt, so it stays
 * open until completion is confirmed. A closed pane is not lost work — the
 * ledger records each agent's session id, so a session is restored by id in a
 * fresh pane. Continuity lives in the id and the agent's written report, never
 * in a pane left open after confirmed completion.
 *
 * Never closable: an agent that is `working`, `blocked`, or `unknown`; a pane
 * whose agent has since moved to a different pane than the ledger recorded; and
 * anything not in the ledger.
 *
 * Installed by `scripts/hooks/install_hooks.sh` as a Stop hook. The runtime
 * guard below is kept regardless — Herdr being present at install time does not
 * mean it is present at run time, and v3 dispatches are not all pane-hosted.
 *
 * The ledger is written by `scripts/office_spawn.sh --pane-id`. A dispatch that
 * does not name a pane never appears here and is never closed.
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
const FINISHED = new Set(["done", "gone"]);

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
 * turns `agent_not_found` — the single most common case here, an agent whose
 * process is already gone — into "CLI unreachable", and the pane never closes.
 */
const herdr = (args) => {
  const parse = (s) => { try { return JSON.parse(s); } catch { return null; } };
  try {
    return parse(execFileSync("herdr", args, { encoding: "utf8", timeout: 8000, stdio: ["ignore", "pipe", "pipe"] }));
  } catch (e) {
    return parse(e?.stdout || "") || parse(e?.stderr || "") || null;
  }
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

  const raw = readFileSync(LEDGER, "utf8");
  const lines = raw.split("\n").filter((l) => l.trim());
  if (!lines.length) process.exit(0);

  const kept = [];
  const looked = new Map(); // one `agent get` per agent name, however many entries reference it

  for (const line of lines) {
    let e;
    try { e = JSON.parse(line); } catch { continue; } // malformed: drop, it names no pane we can act on
    const name = e?.agent || e?.name;
    const pane = e?.pane_id;
    if (!pane) continue;
    // A planner that closed explicitly and marked the entry instead of removing
    // it: drop it, and do not announce a close that already happened.
    if (e.closed === true) continue;
    if (!name) { kept.push(e); continue; } // no name, no status; leave it for the planner

    if (!looked.has(name)) {
      const res = herdr(["agent", "get", name]);
      if (!res) looked.set(name, { status: null, pane: null, session: null });          // CLI unreachable: decide nothing
      else if (res.error?.code === "agent_not_found") looked.set(name, { status: "gone", pane: null, session: null });
      else looked.set(name, {
        status: res.result?.agent?.agent_status || null,
        pane: res.result?.agent?.pane_id || null,
        session: res.result?.agent?.agent_session?.value || null,
      });
    }
    const live = looked.get(name);

    // The agent moved panes since it was recorded: the ledger's pane id may now
    // belong to something else. Not ours to close.
    const paneMatches = live.status === "gone" || live.pane === null || live.pane === pane;

    if (!(live.status && FINISHED.has(live.status)) || !paneMatches) { kept.push(e); continue; }

    // Last chance to capture the session id: after the pane is gone, `agent get`
    // no longer answers, and the id is the only way back into this session.
    const session = e.session_id || live.session || null;

    const res = herdr(["pane", "close", pane]);
    const gone = res?.error?.code && /not_?found/.test(res.error.code);
    if ((res && !res.error) || gone) closed.push({ pane, name, status: live.status, session });
    else kept.push(e); // close failed for a reason we do not understand: retry next Stop
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
