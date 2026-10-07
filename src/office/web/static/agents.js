// Agents surface: the machine-wide current topology in five role columns, with
// edges from each orchestrator to its run's agents, repository and run-state
// filters, and an inspector. Historical attempts appear only in the inspector's
// run history. Every runtime value is shown as recorded, or as unknown /
// unavailable: nothing is guessed and no missing metric is drawn as 0.
import { chatComposer } from "./chat.js";
import { routingView } from "./routing.js";

export const ROLE_COLUMNS = [
  ["orchestrators", "Orchestrators"], ["plan_reviewers", "Plan Reviewers"], ["executors", "Executors"],
  ["code_reviewers", "Code Reviewers"], ["visual_verifiers", "Visual Verifiers"],
];
const RUN_STATES = [["all", "All runs"], ["live", "Live"], ["resumable", "Resumable"], ["terminal", "Terminal"]];
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const UNITS = { percent: "%", bytes: "", tokens: " tokens", state: "" };

const view = { repo: "all", runState: "all", selected: null, routeDraft: new Map(), routePending: new Map() };

// Herdr names an Office agent `office-<dispatch id>` (lowercased, 32 chars), as dispatch.herdr_agent_name does.
export const herdrAgentName = (dispatchId) => `office-${dispatchId}`.toLowerCase().replace(/[^a-z0-9_-]/g, "-").slice(0, 32);
export const rawDispatch = (id) => String(id).replace(/^dispatch:/, "");

function bytes(n) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// One telemetry metric: its measured value, else its explicit status (never 0).
export function metricText(m) {
  if (!m || m.value === null || m.value === undefined || m.status !== "measured") {
    return (m && m.status === "unavailable") ? "unavailable" : "unknown";
  }
  if (m.unit === "bytes") return bytes(Number(m.value));
  return `${m.value}${UNITS[m.unit] ?? ""}`;
}

const yn = (v) => (v ? "yes" : "no");

// The labelled runtime evidence of one node, each item separate.
export function evidence(state) {
  const s = state || {};
  const q = s.quota_wait || {};
  return [
    ["Liveness", s.process || "unknown"],
    ["Activity", s.activity || "unknown"],
    ["Paused", yn(s.paused)],
    ["Blocked", yn(s.blocked)],
    ["Quota wait", q.active ? `yes${q.label ? ` (${q.label})` : ""}${q.resets_at ? `, resets ${q.resets_at}` : ""}` : "no"],
    ["Reply written, awaiting ingestion", yn(s.reply_written_awaiting_ingestion)],
    ["Complete", yn(s.complete)],
    ["Stale", yn(s.stale)],
    ["Unavailable", yn(s.unavailable)],
  ];
}

const routeOf = (n) => `${n.harness || "?"}/${n.model || "?"}@${n.effort || "?"}`;

function visibleNodes(state) {
  const { agents = {}, runs = {} } = state.entities;
  const cols = Object.fromEntries(ROLE_COLUMNS.map(([c]) => [c, []]));
  for (const a of Object.values(agents)) {
    const run = runs[a.run];
    if (!run || !cols[a.column]) continue;
    if (view.repo !== "all" && (run.repo && run.repo.key) !== view.repo) continue;
    if (view.runState !== "all" && run.liveness !== view.runState) continue;
    cols[a.column].push(a);
  }
  for (const list of Object.values(cols)) list.sort((x, y) => String(x.run).localeCompare(String(y.run)) || String(x.id).localeCompare(String(y.id)));
  return cols;
}

function repoOptions(state) {
  const seen = new Map();
  for (const r of Object.values(state.entities.runs)) if (r.repo && r.repo.key) seen.set(r.repo.key, r.repo.slug || r.repo.key);
  return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
}

function nodeCard(ctx, n, state) {
  const { h } = ctx;
  const run = state.entities.runs[n.run] || {};
  const flags = evidence(n.state).filter(([, v]) => v !== "no" && v !== "unknown");
  const record = view.routePending.get(n.id);
  const pending = record && !record.error ? record : null;
  const work = n.current_work || {};
  return h("button", { type: "button", class: `node ${n.column}`, "aria-pressed": view.selected === n.id ? "true" : "false",
    dataset: { testid: "agent-node", id: n.id, column: n.column, run: n.run, key: `node:${n.id}` },
    onclick: () => { view.selected = n.id; ctx.rerender(); } },
    h("span", { class: "node-head" },
      h("span", { class: "node-role", text: n.role || n.column }),
      h("span", { class: "node-run", text: (run.repo && run.repo.slug) || run.run_id || "" })),
    h("span", { class: "node-route", dataset: { testid: "node-route" }, text: `${n.harness || "unknown harness"} · ${n.model || "model unknown"} · ${n.effort || "effort unknown"}` }),
    pending ? h("span", { class: "pending", dataset: { testid: "route-pending" }, text: `route change pending: ${pending.route}` }) : null,
    h("span", { class: "node-work", text: work.title || work.task || work.phase || run.phase || "" }),
    h("span", { class: "node-flags secondary" }, ...flags.map(([k, v]) => h("span", { class: "flag", text: `${k}: ${v}` }))),
    h("span", { class: "node-telemetry secondary" }, ...["cpu", "ram", "context", "quota"].map((m) =>
      h("span", { class: "metric", dataset: { metric: m }, text: `${m.toUpperCase()} ${metricText((n.telemetry || {})[m])}` }))));
}

function moveFocus(ev) {
  const node = ev.target.closest(".node");
  if (!node) return;
  const col = node.closest(".role-col");
  const cols = [...col.parentElement.querySelectorAll(".role-col")];
  let target = null;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    const nodes = [...col.querySelectorAll(".node")];
    target = nodes[nodes.indexOf(node) + (ev.key === "ArrowDown" ? 1 : -1)];
  } else if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
    const step = ev.key === "ArrowRight" ? 1 : -1;
    for (let i = cols.indexOf(col) + step; i >= 0 && i < cols.length && !target; i += step) {
      target = cols[i].querySelector(`.node[data-run="${CSS.escape(node.dataset.run)}"]`) || cols[i].querySelector(".node");
    }
  } else {
    return;
  }
  ev.preventDefault();
  if (target) target.focus();
}

function drawEdges(graph) {
  if (!graph) return;
  let svg = graph.querySelector("svg.edges");
  if (!svg) {
    svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "edges");
    svg.setAttribute("aria-hidden", "true");
    graph.prepend(svg);
  }
  const box = graph.getBoundingClientRect();
  svg.setAttribute("width", graph.scrollWidth);
  svg.setAttribute("height", graph.scrollHeight);
  const lines = [];
  for (const o of graph.querySelectorAll('.node[data-column="orchestrators"]')) {
    const a = o.getBoundingClientRect();
    for (const n of graph.querySelectorAll(`.node[data-run="${CSS.escape(o.dataset.run)}"]:not([data-column="orchestrators"])`)) {
      const b = n.getBoundingClientRect();
      const x1 = a.right - box.left + graph.scrollLeft;
      const y1 = a.top + a.height / 2 - box.top + graph.scrollTop;
      const x2 = b.left - box.left + graph.scrollLeft;
      const y2 = b.top + b.height / 2 - box.top + graph.scrollTop;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
      line.setAttribute("d", `M${x1},${y1} C${x1 + 40},${y1} ${x2 - 40},${y2} ${x2},${y2}`);
      line.setAttribute("class", "edge");
      line.dataset.from = o.dataset.id;
      line.dataset.to = n.dataset.id;
      line.dataset.testid = "agent-edge";
      lines.push(line);
    }
  }
  svg.replaceChildren(...lines);
}

function knownModels(state, harness) {
  const out = new Set();
  for (const a of Object.values(state.entities.agents)) if (a.harness === harness && a.model) out.add(a.model);
  for (const r of Object.values(state.entities.runs)) for (const d of r.history || []) if (d.harness === harness && d.model) out.add(d.model);
  return [...out].sort();
}

// Model + effort change: only for a current live dispatch whose run reports the capability. Harness stays fixed.
function routeControl(ctx, n, state) {
  const { h } = ctx;
  const run = state.entities.runs[n.run] || {};
  const cap = run.controls && run.controls.change_route;
  const s = n.state || {};
  if (n.kind !== "dispatch" || !cap || !cap.allowed || s.process === "exited" || s.complete || s.stale) return null;
  const draft = view.routeDraft.get(n.id) || { model: n.model, effort: n.effort };
  view.routeDraft.set(n.id, draft);
  const record = view.routePending.get(n.id);
  const pending = record && !record.error ? record : null;
  const target = `${n.harness}/${draft.model}@${draft.effort}`;
  const unchanged = draft.model === n.model && draft.effort === n.effort;
  const pick = (field, options, label) => {
    const sel = h("select", { class: "field", dataset: { testid: `route-${field}`, key: `route-${field}:${n.id}` }, "aria-label": label,
      disabled: Boolean(pending), onchange: (ev) => { draft[field] = ev.target.value; ctx.rerender(); } },
    ...options.map((o) => h("option", { value: o, text: o })));
    sel.value = draft[field];
    return sel;
  };
  const efforts = EFFORTS.includes(n.effort) || !n.effort ? EFFORTS : [n.effort, ...EFFORTS];
  return h("section", { class: "route-change", dataset: { testid: "route-change" } },
    h("h3", { text: "Change model + effort" }),
    h("p", { class: "muted", text: `Harness stays ${n.harness}. The current model work restarts; run and worktree state are kept.` }),
    h("div", { class: "row" },
      h("span", { class: "fixed", dataset: { testid: "route-harness" }, text: n.harness }),
      pick("model", [...new Set([n.model, ...knownModels(state, n.harness)])].filter(Boolean), "Model"),
      pick("effort", efforts, "Effort"),
      h("button", { type: "button", class: "btn primary", dataset: { testid: "route-apply", key: `route-apply:${n.id}` },
        disabled: Boolean(pending) || unchanged, onclick: () => {
          const id = ctx.newId();
          view.routePending.set(n.id, { id, route: target, model: draft.model, effort: draft.effort });
          ctx.send("change_route", n.id, { run_id: run.run_id, dispatch_id: rawDispatch(n.id) },
            { route: target, quote: `web UI: change ${rawDispatch(n.id)} to ${target}` },
            { dispatch_id: rawDispatch(n.id), route: routeOf(n) }, `Route ${rawDispatch(n.id)}`, id);
        } }, "Apply")),
    pending ? h("p", { class: "pending", dataset: { testid: "route-pending-detail" },
      text: `Pending: ${pending.route}. Shown as current once Office records it.` }) : null,
    record && record.error ? h("p", { class: "why", dataset: { testid: "route-failed" },
      text: `Route change to ${record.route} failed: ${record.error}` }) : null);
}

// A pending route change ends when the snapshot reports the new model and effort, or its command fails.
function settlePending(ctx, state) {
  for (const [id, p] of view.routePending) {
    const n = state.entities.agents[id];
    const r = ctx.receipt(p.id);
    if (!n || (n.model === p.model && n.effort === p.effort)) { view.routePending.delete(id); view.routeDraft.delete(id); }
    else if (r.status === "failed" && !p.error) p.error = r.error || "refused";
  }
}

function inspector(ctx, n, state) {
  const { h } = ctx;
  const run = state.entities.runs[n.run] || {};
  const task = n.task ? state.entities.tasks[n.task] : null;
  const history = (run.history || []).filter((d) => !n.task || d.task === n.task);
  const kv = (k, v, testid) => h("div", { class: "kv" }, h("dt", { text: k }), h("dd", { dataset: testid ? { testid } : {}, text: v }));
  const evid = evidence(n.state);
  const work = n.current_work || {};
  return [
    h("div", { class: "insp-head" },
      h("h2", { text: `${n.role || n.column} · ${n.kind === "dispatch" ? rawDispatch(n.id) : n.harness}` }),
      h("button", { type: "button", class: "btn", dataset: { testid: "agent-inspector-close", key: "agent-close" }, "aria-label": "Close inspector",
        onclick: () => { view.selected = null; ctx.rerender(); } }, "Close")),
    h("dl", { class: "kvs" },
      kv("Run", run.run_id || "unknown"), kv("Repository", (run.repo && run.repo.slug) || "unknown"),
      kv("Harness", n.harness || "unknown"), kv("Model", n.model || "unknown", "insp-model"), kv("Effort", n.effort || "unknown", "insp-effort"),
      kv("Current work", work.title || work.task || work.phase || "unknown")),
    h("h3", { text: "Runtime state" }),
    h("dl", { class: "kvs evidence-list", dataset: { testid: "agent-evidence" } }, ...evid.map(([k, v]) => kv(k, v, `ev-${k.split(/[ ,]/)[0].toLowerCase()}`))),
    h("h3", { text: "Telemetry" }),
    h("dl", { class: "kvs", dataset: { testid: "agent-telemetry" } },
      ...["cpu", "ram", "context", "quota"].map((m) => kv(m.toUpperCase(), metricText((n.telemetry || {})[m]), `tm-${m}`))),
    n.column === "orchestrators" ? chatComposer(ctx, n) : h("section", { class: "handoff", dataset: { testid: "handoff" } },
      h("h3", { text: "Herdr pane" }),
      h("code", { dataset: { testid: "handoff-command" }, text: `herdr agent attach ${herdrAgentName(rawDispatch(n.id))}` }),
      h("button", { type: "button", class: "btn", dataset: { testid: "handoff-copy", key: `copy:${n.id}` }, onclick: async (ev) => {
        try { await navigator.clipboard.writeText(`herdr agent attach ${herdrAgentName(rawDispatch(n.id))}`); ev.target.textContent = "Copied"; }
        catch { ev.target.textContent = "Copy failed"; }
      } }, "Copy command")),
    routeControl(ctx, n, state),
    task ? routingView(h, task.route, history) : null,
    h("section", { class: "history", dataset: { testid: "run-history" } },
      h("h3", { text: "Run history" }),
      (run.history || []).length
        ? h("ol", {}, ...(run.history || []).map((d) => h("li", { dataset: { testid: "history-item", id: d.id },
          text: `${rawDispatch(d.id)} ${d.role || ""} ${(d.task || "").split("/").pop()} ${routeOf(d)} · ${d.status || "?"}${d.terminal_classification ? ` (${d.terminal_classification})` : ""}` })))
        : h("p", { class: "muted", text: "No earlier attempts recorded" })),
  ];
}

export function renderAgents(base, root) {
  const ctx = { ...base, selectedNode: () => (view.selected && base.store.state ? base.store.state.entities.agents[view.selected] : null) };
  const { h, store, keep } = ctx;
  const state = store.state;
  if (!state) { root.replaceChildren(h("p", { class: "muted", text: "Waiting for Office data…" })); return; }
  settlePending(ctx, state);
  const cols = visibleNodes(state);
  if (view.selected && !state.entities.agents[view.selected]) view.selected = null;
  const selected = view.selected ? state.entities.agents[view.selected] : null;
  const total = Object.values(cols).reduce((n, l) => n + l.length, 0);
  const select = (testid, label, value, options, onchange) => {
    const s = h("select", { class: "field", dataset: { testid, key: testid }, "aria-label": label, onchange },
      ...options.map(([v, t]) => h("option", { value: v, text: t })));
    s.value = value;
    return s;
  };
  keep(root, () => [
    h("div", { class: "canvas-head" },
      h("div", {}, h("h1", { text: "Agents" }), h("p", { class: "sub", text: "Current topology across this machine. Earlier attempts live in each run's history." }))),
    h("div", { class: "toolbar" },
      select("agents-repo", "Repository filter", view.repo, [["all", "All repositories"], ...repoOptions(state)],
        (ev) => { view.repo = ev.target.value; ctx.rerender(); }),
      select("agents-state", "Run state filter", view.runState, RUN_STATES, (ev) => { view.runState = ev.target.value; ctx.rerender(); }),
      h("span", { class: "count", dataset: { testid: "agents-count" }, text: `${total} agent${total === 1 ? "" : "s"}` })),
    h("div", { class: "agents-work" },
      h("div", { class: "graph", role: "group", "aria-label": "Agents graph", dataset: { testid: "agents-graph" }, onkeydown: moveFocus },
        ...ROLE_COLUMNS.map(([c, label]) => h("div", { class: "role-col", role: "group", "aria-label": label, dataset: { testid: "role-column", column: c } },
          h("h2", { class: "col-title", text: `${label} (${cols[c].length})` }),
          ...(cols[c].length ? cols[c].map((n) => nodeCard(ctx, n, state)) : [h("p", { class: "muted empty-col", text: "None" })])))),
      selected ? h("aside", { class: "agent-inspector", dataset: { testid: "agent-inspector", id: selected.id }, "aria-label": "Agent inspector",
        onkeydown: (ev) => { if (ev.key === "Escape") { const id = view.selected; view.selected = null; ctx.rerender(); root.querySelector(`.node[data-id="${CSS.escape(id)}"]`)?.focus(); } } },
      ...inspector(ctx, selected, state)) : null),
  ]);
  const graph = root.querySelector(".graph");
  requestAnimationFrame(() => drawEdges(graph));
}
