// Auto Office Workstation shell: header freshness, banners, product and
// repository rails, and the Issues surface. Persistent controls (search
// fields, the table scroller) are never rebuilt; regions that are rebuilt
// keep the focused control, its text selection and their scroll position.
import { Store } from "./store.js";
import { AUTHORIZATIONS, QUEUE_AUTHORIZATION, QUEUE_ONLY_WHY, endStateLabel, githubMark, issueActions,
  issueRows, localMark, receiptState, repoName, shortRun, startCommand } from "./model.js";

const ROW = 44;
const OVERSCAN = 8;
const COLUMNS = ["Repository", "Issue", "Owner", "Phase", "Progress", "Priority", "Authorization", "PR / run",
  "State", "Office gates", "GitHub checks", "Action"];
const SURFACES = { agents: "Agents", allocation: "Allocation", settings: "Settings" };
const $ = (id) => document.getElementById(id);
const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
const token = meta("office-token");
const fixture = meta("office-fixture");

const ui = {
  surface: "issues", repo: null, repoQuery: "", query: "", selected: null, cursor: null,
  drafts: new Map(), local: new Map(), copied: null, selectOpen: false,
};
let rows = [];
let visible = [];
const store = new Store();
window.officeStore = store; // inspected by browser tests

// ------------------------------------------------------------------ DOM helpers

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) el.append(c);
  return el;
}

// Rebuild `container` with `build()` while keeping its focused control, text selection and scroll.
function keep(container, build) {
  const active = document.activeElement;
  const key = active && container.contains(active) ? active.dataset.key : null;
  let range = null;
  if (key && typeof active.selectionStart === "number") range = [active.selectionStart, active.selectionEnd];
  const scroll = container.scrollTop;
  container.replaceChildren(...[build()].flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  container.scrollTop = scroll;
  if (!key) return;
  const again = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (!again) return;
  again.focus({ preventScroll: true });
  if (range) again.setSelectionRange(...range);
}

// Links from GitHub or runs.db open only when they are https URLs.
const safeHref = (url) => { try { return new URL(url).protocol === "https:" ? url : null; } catch { return null; } };

const ago = (seconds) => {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "never";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
};
const now = () => Date.now() / 1000;
const isoSeconds = (iso) => (iso ? Date.parse(iso) / 1000 : null);
const clock = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// ------------------------------------------------------------------ freshness and banners

function githubSources(state) {
  const sources = (state.freshness.github || {}).sources || {};
  const out = [];
  if (sources.discovery) out.push({ repo: null, source: "discovery", ...sources.discovery });
  for (const [repo, srcs] of Object.entries(sources.repos || {})) {
    for (const [source, f] of Object.entries(srcs)) out.push({ repo, source, ...f });
  }
  return out;
}

function renderHeader() {
  const s = store.state;
  const office = $("office").parentElement;
  const gh = document.querySelector("[data-testid=github-indicator]");
  const stream = document.querySelector("[data-testid=stream-indicator]");
  stream.dataset.state = store.status;
  stream.querySelector(".state").textContent = { live: "stream live", connecting: "connecting",
    reconnecting: "reconnecting", disconnected: "disconnected" }[store.status];
  if (!s) return;
  const o = s.freshness.office || {};
  office.dataset.state = o.state;
  $("office").textContent = o.state;
  office.querySelector(".age").textContent = o.last_ok_at ? `read ${ago(now() - isoSeconds(o.last_ok_at))}` : "no read yet";
  const g = s.freshness.github || {};
  gh.dataset.state = g.state;
  gh.querySelector(".state").textContent = g.state;
  const fetched = githubSources(s).map((f) => f.fetched_at).filter((t) => typeof t === "number");
  gh.querySelector(".age").textContent = fetched.length ? `oldest ${ago(now() - Math.min(...fetched))}` : "never fetched";
  document.querySelector("[data-testid=rev]").textContent = `${s.epoch}:${s.rev}`;
}

function banner(kind, testid, title, text) {
  return h("div", { class: `banner ${kind}`, dataset: { testid }, role: kind === "err" ? "alert" : "status" },
    h("strong", { text: title }), h("span", { text }));
}

function renderBanners() {
  const out = [];
  const s = store.state;
  if (store.status === "reconnecting" || store.status === "disconnected") {
    out.push(banner("err", "banner-disconnected", "Service disconnected",
      `The local Office service is not answering; ${store.status === "disconnected" ? "retrying with backoff" : "reconnecting"}. Data shown may be out of date.`));
  }
  if (s) {
    const o = s.freshness.office || {};
    if (o.state === "disconnected") out.push(banner("err", "banner-office-disconnected", "Office disconnected", o.reason || "runs.db is unreadable"));
    if (o.state === "stale") {
      out.push(banner("warn", "banner-office-stale", "Office data stale",
        `${o.reason || "runs.db has not been read recently"}; last read ${o.last_ok_at ? ago(now() - isoSeconds(o.last_ok_at)) : "never"}. Commands are refused until it is live.`));
    }
    const g = s.freshness.github || {};
    const srcs = githubSources(s);
    const repos = (state) => [...new Set(srcs.filter((f) => f.state === state).map((f) => f.repo || "discovery"))];
    const revoked = new Set(repos("revoked"));
    for (const r of Object.values(s.entities.repos)) if (r.github && r.github.access === "revoked") revoked.add(r.github.full_name);
    const limited = srcs.filter((f) => f.state === "rate_limited");
    if (limited.length) {
      const resets = limited.map((f) => f.reset_at).filter((t) => typeof t === "number");
      const when = resets.length ? `resets at ${clock(Math.max(...resets))} (in ${Math.max(0, Math.ceil((Math.max(...resets) - now()) / 60))}m)` : "reset time unknown";
      out.push(banner("warn", "banner-rate-limited", "GitHub rate limited", `${repos("rate_limited").join(", ")}: ${when}. GitHub data is held at its last fetch.`));
    }
    if (revoked.size) out.push(banner("err", "banner-revoked", "GitHub access revoked", `${[...revoked].join(", ")}: no longer visible to the GitHub token; its issues and PRs are hidden.`));
    if (repos("unauthenticated").length) out.push(banner("err", "banner-unauthenticated", "GitHub sign-in failed", "The GitHub token was rejected (HTTP 401)."));
    const stale = repos("stale");
    if (stale.length) out.push(banner("warn", "banner-github-stale", "GitHub data stale", `${stale.join(", ")}: the last refresh failed; showing the previous fetch.`));
    if (g.state === "disabled") out.push(banner("info", "banner-github-disabled", "GitHub not connected", g.reason || "no GitHub client; issues come from Office records only"));
  }
  $("banners").replaceChildren(...out);
}

// ------------------------------------------------------------------ rails

function renderSurfaces() {
  for (const b of document.querySelectorAll(".surface")) {
    if (b.dataset.surface === ui.surface) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  $("surface-issues").hidden = ui.surface !== "issues";
  const other = $("surface-other");
  other.hidden = ui.surface === "issues";
  if (ui.surface !== "issues") {
    other.replaceChildren(h("h1", { text: SURFACES[ui.surface] }),
      h("p", { class: "sub", text: `The ${SURFACES[ui.surface]} surface is not part of this build yet. Issues is available.` }));
  }
}

function mark([tone, label, detail], kind) {
  return h("span", { class: `mark ${tone}`, title: `${kind}: ${detail}`, dataset: { testid: `mark-${kind}`, tone } },
    label, h("span", { class: "sr", text: ` (${detail})` }));
}

function renderRepos() {
  const s = store.state;
  const list = $("repo-list");
  keep(list, () => {
    if (!s) return h("p", { class: "muted", text: "Loading repositories…" });
    const repos = Object.values(s.entities.repos).sort((a, b) => repoName(a).localeCompare(repoName(b)));
    if (!repos.length) return h("p", { class: "muted", dataset: { testid: "no-repos" }, text: "No repositories: no GitHub repository is visible and no run is recorded." });
    const counts = {};
    for (const r of rows) counts[r.repoKey] = (counts[r.repoKey] || 0) + 1;
    const q = ui.repoQuery.toLowerCase();
    const item = (key, name, count, marks) => h("button", {
      type: "button", class: "repo", role: "listitem", "aria-pressed": String(ui.repo === key), dataset: { key: `repo:${key ?? "all"}`, testid: key ? `repo-${name}` : "repo-all" },
      onclick: () => { ui.repo = key; ui.cursor = null; renderAll(); },
    }, h("span", { class: "name", text: name }), h("span", { class: "n", text: String(count) }), marks);
    return [
      item(null, "All repositories", rows.length, null),
      ...repos.filter((r) => !q || repoName(r).toLowerCase().includes(q)).map((r) => item(r.id, repoName(r), counts[r.id] || 0,
        h("span", { class: "marks" }, mark(githubMark(r, s.freshness), "github"), mark(localMark(r), "local")))),
    ];
  });
}

// ------------------------------------------------------------------ KPIs

function renderKpis() {
  const s = store.state;
  if (!s) return;
  const running = rows.filter((r) => r.liveness === "live").length;
  const agents = Object.values(s.entities.agents).filter((a) => a.state && !a.state.complete && !a.state.unavailable
    && a.state.process !== "exited").length;
  const cpu = ((s.scalars.host || {}).telemetry || {}).cpu || {};
  const measured = cpu.status === "measured" && typeof cpu.value === "number";
  const kpi = (tone, value, label, testid) => h("div", { class: `kpi ${tone}`, dataset: { testid } },
    h("div", { class: "v", text: value }), h("div", { class: "l", text: label }));
  $("kpis").replaceChildren(
    kpi("", String(running), running ? "Running issues" : "No active runs", "kpi-running"),
    kpi("green", String(agents), "Active agents", "kpi-agents"),
    kpi("purple", measured ? `${Math.round(cpu.value)}%` : "—", measured ? "CPU load" : "CPU telemetry unavailable", "kpi-cpu"));
}

// ------------------------------------------------------------------ table

function filtered() {
  const q = ui.query.trim().toLowerCase();
  return rows.filter((r) => (!ui.repo || r.repoKey === ui.repo) && (!q || r.search.includes(q)));
}

function rowIndex(id) { return visible.findIndex((r) => r.id === id); }

function cells(r, active) {
  const p = r.progress;
  const pct = p && typeof p.value === "number" ? Math.round(p.value * 100) : null;
  const actions = issueActions(r, store.state, inFlight);
  const a = actions.primary;
  const prCount = r.prs.length;
  const td = (cls, ...c) => h("div", { class: `td ${cls}`, role: "gridcell" }, ...c);
  return [
    td("repo", h("span", { text: r.repoName })),
    td("issue", h("div", { class: "t", text: `#${r.number} ${r.title || "(not among GitHub's open issues)"}` }),
      h("div", { class: "s", text: r.provenance === "github" ? "GitHub issue" : "From Office record" })),
    td(r.owner === "No owner" ? "muted" : "", r.owner),
    td("", h("span", { class: "phase", dataset: { tone: r.run ? r.liveness : "incoming" }, text: r.phase })),
    td("progress", pct === null ? h("span", { class: "muted", text: "—", title: "no task structure recorded" })
      : [h("span", { class: "bar", "aria-hidden": "true" }, h("span", { style: `width:${pct}%` })),
        h("span", { text: `${p.accepted_weight}/${p.total_weight}` })]),
    td(r.priority ? "" : "muted", r.priority || "—"),
    td("", r.authorization ? h("span", { class: "pill", text: r.authorization }) : h("span", { class: "muted", text: "—" })),
    td("links", r.run ? h("span", { text: `run ${shortRun(r.run.id)}` }) : h("span", { class: "muted", text: "no run" }),
      prCount ? h("span", { class: "s", text: ` · ${prCount} PR${prCount > 1 ? "s" : ""}` }) : null),
    td("", { live: "Live run", resumable: "Resumable", terminal: "Closed run", none: "No run" }[r.liveness]
      + (r.queue ? ` · ${r.queue.decision}` : "")),
    td(r.gates ? "" : "muted", r.gates || "—"),
    td(r.checks && r.checks !== "unavailable" ? "" : "muted", r.checks || "—"),
    td("act", h("button", { type: "button", class: `btn ${a.enabled ? "primary" : ""}`, tabindex: active ? "0" : "-1",
      disabled: !a.enabled, title: a.reason || a.label, "aria-label": `${a.label} #${r.number}${a.reason ? ` (unavailable: ${a.reason})` : ""}`,
      dataset: { key: `row-action:${r.id}` }, onclick: (ev) => { ev.stopPropagation(); act(a, r); } }, a.label)),
  ];
}

function renderRows() {
  const scroller = $("issue-table");
  const body = $("issue-body");
  const height = scroller.clientHeight || 600;
  const first = Math.max(0, Math.floor(scroller.scrollTop / ROW) - OVERSCAN);
  const last = Math.min(visible.length, Math.ceil((scroller.scrollTop + height) / ROW) + OVERSCAN);
  if (ui.cursor === null || rowIndex(ui.cursor) < 0) ui.cursor = (visible[rowIndex(ui.selected)] || visible[first] || {}).id ?? null;
  keep(body, () => {
    const out = [];
    for (let i = first; i < last; i += 1) {
      const r = visible[i];
      const active = r.id === ui.cursor;
      out.push(h("div", {
        class: "tr", role: "row", "aria-rowindex": String(i + 2), "aria-selected": String(r.id === ui.selected),
        tabindex: active ? "0" : "-1", style: `top:${i * ROW}px`, dataset: { key: `row:${r.id}`, testid: "issue-row", id: r.id },
        onclick: () => select(r.id), onfocus: () => { ui.cursor = r.id; },
      }, cells(r, active)));
    }
    return out;
  });
}

function renderTable() {
  const table = $("issue-table");
  visible = filtered();
  table.setAttribute("aria-rowcount", String(visible.length + 1));
  $("issue-body").style.height = `${visible.length * ROW}px`;
  $("issue-count").textContent = store.state ? `${visible.length} of ${rows.length} issues` : "";
  const empty = $("table-empty");
  let kind = null;
  let text = null;
  if (!store.state) [kind, text] = ["loading", "Loading Office data…"];
  else if (!Object.keys(store.state.entities.repos).length) [kind, text] = ["no-repos", "No repositories yet. Connect GitHub or start a run from a checkout."];
  else if (!visible.length && ui.query.trim()) [kind, text] = ["no-results", `No issues match “${ui.query.trim()}”${ui.repo ? " in this repository" : ""}.`];
  else if (!visible.length) [kind, text] = ["no-issues", ui.repo ? "No open issues in this repository." : "No open issues."];
  empty.hidden = !kind;
  if (kind) {
    empty.dataset.kind = kind;
    empty.replaceChildren(h("strong", { text }),
      kind === "no-results" ? h("button", { type: "button", class: "btn", text: "Clear search", onclick: () => { $("issue-search").value = ""; ui.query = ""; renderAll(); } }) : null);
  }
  renderRows();
}

function select(id, { focusRow = false } = {}) {
  ui.selected = id;
  ui.cursor = id;
  renderTable();
  renderInspector();
  if (focusRow) focusCursor();
}

function focusCursor() {
  const id = ui.cursor; // re-rendering refocuses the previous row, whose focus handler moves the cursor
  const i = rowIndex(id);
  if (i < 0) return;
  const scroller = $("issue-table");
  const top = i * ROW;
  const head = 40;
  if (top < scroller.scrollTop) scroller.scrollTop = top;
  else if (top + ROW > scroller.scrollTop + scroller.clientHeight - head) scroller.scrollTop = top + ROW - scroller.clientHeight + head;
  renderRows();
  ui.cursor = id;
  const el = $("issue-body").querySelector(`[data-key="${CSS.escape(`row:${id}`)}"]`);
  if (el) el.focus({ preventScroll: true });
}

function onTableKey(ev) {
  const row = ev.target.closest && ev.target.closest(".tr");
  if (!row || ev.target !== row) return;
  const i = rowIndex(row.dataset.id);
  const page = Math.max(1, Math.floor($("issue-table").clientHeight / ROW) - 1);
  const to = { ArrowDown: i + 1, ArrowUp: i - 1, PageDown: i + page, PageUp: i - page, Home: 0, End: visible.length - 1 }[ev.key];
  if (to !== undefined) {
    ev.preventDefault();
    ui.cursor = visible[Math.max(0, Math.min(visible.length - 1, to))].id;
    focusCursor();
  } else if (ev.key === "Enter" || ev.key === " ") {
    ev.preventDefault();
    select(row.dataset.id);
    const first = $("inspector").querySelector("button, select, a, input");
    if (first) first.focus();
  }
}

// ------------------------------------------------------------------ inspector

function draft(id) {
  if (!ui.drafts.has(id)) ui.drafts.set(id, { auth: "preview" });
  return ui.drafts.get(id);
}

const kv = (k, v, testid) => h("div", { class: "row", dataset: testid ? { testid } : {} }, h("span", { text: k }), h("b", { text: v ?? "—" }));

function actionButton(a, r, extra = {}) {
  return h("button", { type: "button", class: `btn ${extra.primary ? "primary" : ""}`, disabled: !a.enabled,
    "aria-describedby": a.reason ? `why-${a.kind}` : null, dataset: { key: `action:${a.kind}`, testid: `action-${a.kind}` },
    onclick: () => act(a, r) }, a.label);
}

const why = (a) => (a.reason ? h("span", { class: "why warn", id: `why-${a.kind}`, dataset: { testid: `why-${a.kind}` }, text: a.reason }) : null);

function authSelect(id, key, value, onchange, disabledWhy) {
  return h("select", { "aria-label": key === "queue" ? "Auto Queue authorization" : "Start authorization", dataset: { key: `auth:${key}`, testid: `auth-${key}` },
    onchange: (ev) => onchange(ev.target.value) },
  AUTHORIZATIONS.map((o) => {
    const off = disabledWhy && o.value !== QUEUE_AUTHORIZATION;
    return h("option", { value: o.value, selected: o.value === value, disabled: off, title: off ? disabledWhy : null },
      off ? `${o.label} (not for queued work)` : o.label);
  }));
}

function inspectorActions(r) {
  const s = store.state;
  const acts = issueActions(r, s, inFlight);
  const by = Object.fromEntries(acts.list.map((a) => [a.kind, a]));
  const out = [];
  if (by.copy_start) {
    const d = draft(r.id);
    const cmd = r.url ? startCommand(r.url, d.auth) : null;
    out.push(h("div", { class: "action" }, h("code", { class: "copy", dataset: { testid: "start-command" }, text: cmd || "—" }),
      actionButton(by.copy_start, r, { primary: true }),
      ui.copied === r.id ? h("span", { class: "why", role: "status", text: "Copied to the clipboard" }) : null, why(by.copy_start)));
    out.push(h("div", { class: "action" }, authSelect(r.id, "start", d.auth, (v) => { d.auth = v; renderInspector({ force: true }); }),
      actionButton(by.start_issue, r), why(by.start_issue)));
    out.push(h("div", { class: "action" }, authSelect(r.id, "queue", QUEUE_AUTHORIZATION, () => {}, QUEUE_ONLY_WHY),
      actionButton(by.queue_issue, r), why(by.queue_issue),
      h("span", { class: "why", text: `Auto Queue authorizes an unattended local launch when capacity permits. ${QUEUE_ONLY_WHY}.` })));
    const readiness = r.repo && r.repo.readiness;
    if (readiness && !readiness.ready) {
      out.push(h("h3", { text: "Missing prerequisites" }), h("ul", { class: "prereq", dataset: { testid: "prerequisites" } },
        (readiness.prerequisites || []).map((p) => h("li", { class: p.ok ? "" : "fail", text: p.ok ? p.name : `${p.name}: ${p.detail || "failing"}` }))));
    }
  } else {
    out.push(h("div", { class: "action" }, acts.list.map((a, i) => actionButton(a, r, { primary: i === 0 })), acts.list.map(why)));
  }
  return out;
}

function runCard(run) {
  const p = run.progress;
  const c = run.controls || {};
  return h("div", { class: "card", dataset: { testid: "run-card", run: run.id } },
    kv("Run", run.run_id), kv("Phase", run.phase), kv("Liveness", run.liveness),
    kv("Owner", run.owner && run.owner.kind === "session" ? `${run.owner.harness} orchestrator` : "No owner"),
    kv("Progress", p && p.total_weight ? `${p.accepted_weight}/${p.total_weight} weighted tasks` : "—"),
    kv("Authorization", endStateLabel(run.end_state && run.end_state.value) || "—"),
    kv("Runtime", run.office_version ? `${run.office_version}${c.runtime && c.runtime.read_only ? " (read-only)" : ""}` : "unknown"),
    kv("Office gates", run.gates && run.gates.office ? `${run.gates.office.total} recorded` : "—"),
    kv("GitHub checks", "see PRs"),
    run.launch ? kv("Launched", `from the web (command ${run.launch.command})`) : null,
    c.runtime && c.runtime.read_only && c.resume_run ? h("div", { class: "why warn", text: `Capability unavailable: ${c.resume_run.reason}` }) : null,
    h("div", { class: "prov", text: `Provenance: ${run.issue ? `${run.issue.provenance} (${run.issue.source})` : "runs.db"}` }));
}

function prCard(p) {
  const gh = p.github;
  return h("div", { class: "card", dataset: { testid: "pr-card" } },
    h("div", { class: "row" }, h("span", {}, safeHref(p.url) ? h("a", { href: safeHref(p.url), target: "_blank", rel: "noopener noreferrer", text: `PR #${p.number}` }) : `PR #${p.number}`),
      h("b", { text: p.merged ? "merged" : gh ? gh.state : "state unknown" })),
    kv("Base ← head", `${p.base || "?"} ← ${p.branch || (gh && gh.head) || "?"}`, "pr-base-head"),
    p.stacked_on ? kv("Stacked on", `task ${p.stacked_on}`, "pr-stacked") : null,
    kv("Run", shortRun(p.run)),
    kv("GitHub checks", p.github_checks || "unavailable"),
    h("div", { class: "prov", text: `Provenance: ${p.provenance} (${p.source})${gh ? "; state from GitHub" : ""}` }));
}

function renderInspector({ force = false } = {}) {
  const panel = $("inspector");
  const r = ui.selected && rows.find((x) => x.id === ui.selected);
  panel.hidden = !ui.selected;
  if (!ui.selected) return;
  // An open native <select> closes when it is replaced: leave the panel alone while one is open.
  if (!force && ui.selectOpen && panel.dataset.issue === ui.selected) return;
  panel.dataset.issue = ui.selected;
  keep(panel, () => {
    if (!r) return [h("p", { class: "muted", text: "This issue is no longer listed." })];
    return [
      h("button", { type: "button", class: "btn close", "aria-label": "Close inspector", dataset: { key: "close" }, onclick: closeInspector }, "Close"),
      h("h2", { dataset: { testid: "inspector-title" }, text: `#${r.number} ${r.title || ""}` }),
      h("div", { class: "prov" }, `${r.repoName} · ${r.provenance === "github" ? "GitHub issue" : "known from an Office record"} `,
        safeHref(r.url) ? h("a", { href: safeHref(r.url), target: "_blank", rel: "noopener noreferrer", text: "open on GitHub" }) : null),
      h("h3", { text: "Actions" }), h("div", { class: "actions", dataset: { testid: "actions" } }, inspectorActions(r)),
      h("h3", { text: `Runs (${r.runs.length})` }),
      r.runs.length ? r.runs.map(runCard) : h("p", { class: "muted", text: "No run yet." }),
      h("h3", { text: `Pull requests (${r.prs.length})` }),
      r.prs.length ? r.prs.map(prCard) : h("p", { class: "muted", text: "No linked pull requests." }),
    ];
  });
}

function closeInspector() {
  const id = ui.selected;
  ui.selected = null;
  $("inspector").dataset.issue = "";
  renderTable();
  renderInspector();
  ui.cursor = id;
  focusCursor();
}

// ------------------------------------------------------------------ commands

// A command of this kind for this issue/run is pending or has an unknown result: no second send.
function inFlight(kind, subject) {
  const server = store.state ? store.state.entities.commands : {};
  for (const l of ui.local.values()) {
    if (l.kind !== kind || l.subject !== subject) continue;
    const status = receiptState((server[`command:${l.id}`] || {}).status || l.status);
    if (status === "pending" || status === "unknown") return true;
  }
  return false;
}

function newId() {
  if (crypto.randomUUID) return `web-${crypto.randomUUID()}`;
  const b = crypto.getRandomValues(new Uint8Array(16));
  return `web-${Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("")}`;
}

async function send(kind, subject, target, payload, expect, label) {
  const id = newId();
  ui.local.set(id, { id, kind, subject, status: "pending", label, error: null, at: new Date().toISOString() });
  renderAll();
  let res;
  let body = null;
  try {
    res = await fetch("/api/commands", { method: "POST", headers: { "Content-Type": "application/json", "X-Office-Token": token },
      body: JSON.stringify({ id, kind, target, expect, payload }) });
    body = await res.json().catch(() => null);
  } catch {
    res = null;
  }
  const entry = ui.local.get(id);
  if (res && body && body.receipt && (res.ok || body.receipt.status)) {
    Object.assign(entry, { status: res.ok ? body.receipt.status : "failed", error: res.ok ? body.receipt.error : body.message });
  } else if (res && res.status < 500 && body) {
    Object.assign(entry, { status: "failed", error: body.message || body.reason });
  } else {
    // No answer: the command may or may not have run. Never retried automatically.
    Object.assign(entry, { status: "unknown", error: "no answer from the service; check the result before sending again" });
  }
  renderAll();
}

async function act(a, r) {
  if (!a.enabled) return;
  if (a.kind === "copy_start") {
    const cmd = startCommand(r.url, draft(r.id).auth);
    try { await navigator.clipboard.writeText(cmd); ui.copied = r.id; } catch { ui.copied = null; }
    renderInspector();
    return;
  }
  const label = `${a.label} #${r.number}`;
  if (a.kind === "start_issue") {
    send("start_issue", r.id, { repo: r.repo.github.full_name, issue: r.number },
      { end_state: draft(r.id).auth, title: r.title, new_run_confirmed: false }, { live_run: null, resumable_run: null }, label);
  } else if (a.kind === "queue_issue") {
    send("queue_issue", r.id, { repo: r.repo.github.full_name, issue: r.number }, { priority: "normal", title: r.title }, {}, label);
  } else {
    send(a.kind, a.run.id, { run_id: a.run.run_id }, {}, {}, label);
  }
}

function renderReceipts() {
  const s = store.state;
  const server = s ? s.entities.commands : {};
  const merged = new Map();
  for (const c of Object.values(server)) merged.set(c.id, { ...c, label: `${c.kind.replace(/_/g, " ")} ${c.target ? Object.values(c.target).join(" ") : ""}` });
  for (const l of ui.local.values()) {
    const sv = merged.get(l.id);
    merged.set(l.id, sv ? { ...sv, label: l.label } : { ...l, accepted_at: l.at });
  }
  const list = [...merged.values()].sort((a, b) => String(b.accepted_at).localeCompare(String(a.accepted_at))).slice(0, 8);
  const text = { pending: "pending", completed: "completed", failed: "failed", unknown: "result unknown, not retried",
    checked: "result unknown, checked" };
  $("receipts").replaceChildren(...(list.length ? [h("span", { class: "muted", text: "Commands" })] : []), ...list.map((c) => {
    const st = receiptState(c.status);
    const local = ui.local.get(c.id);
    const clear = st === "unknown" && local && local.status === "unknown" && !server[`command:${c.id}`]
      ? h("button", { type: "button", class: "btn", dataset: { testid: "receipt-checked", key: `checked:${c.id}` },
        "aria-label": `I checked ${c.label}; allow sending it again`,
        onclick: () => { local.status = "checked"; renderAll(); } }, "Checked, allow resend") : null;
    return h("span", { class: "receipt", dataset: { status: st, testid: "receipt", id: c.id }, title: c.error || c.id },
      `${c.label} · ${text[st] || st}`, clear);
  }));
}

// ------------------------------------------------------------------ wiring

function renderAll() {
  rows = store.state ? issueRows(store.state) : [];
  renderHeader();
  renderBanners();
  renderSurfaces();
  renderRepos();
  renderKpis();
  renderTable();
  renderInspector();
  renderReceipts();
}

let frame = 0;
function schedule() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; renderAll(); });
}

function init() {
  if (fixture) {
    const marker = $("fixture");
    marker.textContent = `FIXTURE MODE (${fixture})`;
    marker.hidden = false;
  }
  $("issue-table").querySelector(".thead").replaceChildren(...COLUMNS.map((c) => h("div", { class: "th", role: "columnheader", text: c })));
  for (const b of document.querySelectorAll(".surface")) b.addEventListener("click", () => { ui.surface = b.dataset.surface; renderSurfaces(); });
  $("issue-search").addEventListener("input", (ev) => { ui.query = ev.target.value; ui.cursor = null; renderTable(); });
  $("repo-search").addEventListener("input", (ev) => { ui.repoQuery = ev.target.value; renderRepos(); });
  $("issue-table").addEventListener("scroll", () => renderRows(), { passive: true });
  $("issue-table").addEventListener("keydown", onTableKey);
  $("inspector").addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeInspector(); });
  const closeSelect = (ev) => { if (ev.target.tagName === "SELECT" && ui.selectOpen) { ui.selectOpen = false; schedule(); } };
  $("inspector").addEventListener("pointerdown", (ev) => { if (ev.target.tagName === "SELECT") ui.selectOpen = true; });
  $("inspector").addEventListener("change", closeSelect);
  $("inspector").addEventListener("focusout", closeSelect);
  window.addEventListener("resize", () => renderRows());
  store.subscribe(schedule);
  setInterval(() => { renderHeader(); renderBanners(); }, 1000);
  renderAll();
  store.connect();
}

init();
