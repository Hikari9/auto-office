// Settings: the Machine -> Repository -> Run cascade from GET /api/settings. Each row shows the
// effective value, the tier it comes from and whether it is inherited or overridden; inspection
// shows every tier. Edits go through settings_set / settings_unset, only on tiers the service
// lists as editable, and the view shows the re-read server value, never the typed one.

export const INTAKE = ["intake.queue_issues", "intake.authorization", "intake.ready_to_land"];
export const PINNED_WHY = "Pinned by this run at office start: a run's policy is never edited. Change the machine or repository value for future runs.";
const TIER_LABEL = { default: "Default", machine: "Machine", repository: "Repository", "run-pinned": "Run (pinned)" };
const APPLY_LABEL = { immediate: "applies immediately", "before-dispatch": "applies before next dispatch",
  "future-runs": "applies to future runs", restart: "applies after restart" };

const ui = { scope: "machine", repo: "", run: "", query: "", inspect: new Set(), edit: null, data: null, loadedFor: null,
  error: null, loading: false, retryAt: 0, retryTimer: 0, seen: new Set() };

export const settingsUi = ui; // inspected by browser tests

export function query() {
  if (ui.scope === "run" && ui.run) return `?run=${encodeURIComponent(ui.run)}`;
  if (ui.scope === "repository" && ui.repo) return `?repo=${encodeURIComponent(ui.repo)}`;
  return "";
}

const RETRY_MS = 3000;

async function load(ctx, force = false) {
  const q = query();
  if (!force && (ui.loadedFor === q || ui.loading || Date.now() < ui.retryAt)) return;
  ui.loading = true;
  ui.loadedFor = q;
  try {
    const res = await fetch(`/api/settings${q}`, { headers: { "X-Office-Token": ctx.token } });
    const body = await res.json().catch(() => null);
    if (query() !== q) return; // the scope moved on while this was in flight
    if (res.ok && body && Array.isArray(body.entries)) { ui.data = body; ui.error = null; } else {
      fail(ctx, `${(body && (body.message || body.reason)) || `settings unavailable (HTTP ${res.status})`}`);
    }
  } catch {
    if (query() === q) fail(ctx, "the Office service did not answer; settings unavailable");
  } finally {
    ui.loading = false;
    ctx.rerender();
  }
}

// A failed read is retried on a later render, after RETRY_MS.
function fail(ctx, message) {
  clearTimeout(ui.retryTimer);
  ui.retryTimer = setTimeout(ctx.rerender, RETRY_MS);
  ui.data = null;
  ui.error = `${message}; retrying`;
  ui.loadedFor = null;
  ui.retryAt = Date.now() + RETRY_MS;
}

// A settings command that finished (either way) means the files may have changed: re-read them.
function watchCommands(ctx) {
  for (const l of ctx.local.values()) {
    if (!String(l.subject).startsWith("settings:") || ui.seen.has(l.id)) continue;
    const st = ctx.receiptState((ctx.state.entities.commands[`command:${l.id}`] || {}).status || l.status);
    if (st === "pending") continue;
    ui.seen.add(l.id);
    load(ctx, true);
  }
}

// After the editor closes, focus returns to its Edit button once the view has re-rendered.
function refocus(key) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.querySelector(`#surface-settings [data-key="${CSS.escape(key)}"]`);
    if (el) el.focus();
  }));
}

// Text for a string-valued key stays a string ("123" is not 123); other keys parse as JSON when they can.
export function parseValue(text, like) {
  if (typeof like === "string") return text;
  try { return JSON.parse(text.trim()); } catch { return text; }
}

const show = (v) => (v === null || v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));

function editor(ctx, e, tier) {
  const h = ctx.h;
  const current = e.values[tier];
  const bool = typeof e.value === "boolean";
  const subject = `settings:${tier}:${e.key}`;
  const office = (ctx.state.freshness.office || {}).state;
  const stale = office !== "live" ? "Office data is not live; commands are refused" : null;
  const pending = ctx.inFlight("settings_set", subject) || ctx.inFlight("settings_unset", subject) || !!stale;
  const target = { tier, key: e.key, ...(ui.scope === "run" ? { run_id: ui.run } : ui.scope === "repository" ? { repo: ui.repo } : {}) };
  const input = bool
    ? h("select", { class: "field", "aria-label": `New ${TIER_LABEL[tier]} value for ${e.key}`, dataset: { key: `edit-input:${e.key}`, testid: "setting-input" },
      onchange: (ev) => { ui.edit.text = ev.target.value; } },
      ["true", "false"].map((v) => h("option", { value: v, selected: ui.edit.text === v }, v)))
    : h("input", { class: "field", "aria-label": `New ${TIER_LABEL[tier]} value for ${e.key}`, value: ui.edit.text,
      dataset: { key: `edit-input:${e.key}`, testid: "setting-input" }, oninput: (ev) => { ui.edit.text = ev.target.value; } });
  if (ui.edit.focus) { ui.edit.focus = false; setTimeout(() => input.focus(), 0); }
  const close = () => { ui.edit = null; ctx.rerender(); refocus(`edit:${tier}:${e.key}`); };
  const save = () => {
    if (pending) return;
    ctx.send("settings_set", subject, target, { value: parseValue(ui.edit.text, current ?? e.value) }, {}, `Set ${e.key} (${TIER_LABEL[tier]})`);
    ui.edit = null;
    refocus(`edit:${tier}:${e.key}`);
  };
  return h("form", { class: "edit", dataset: { testid: "setting-editor" }, onsubmit: (ev) => { ev.preventDefault(); save(); },
    onkeydown: (ev) => { if (ev.key === "Escape") { ev.preventDefault(); close(); } } },
    input,
    h("button", { type: "submit", class: "btn primary", disabled: pending, title: stale, dataset: { key: `edit-save:${e.key}`, testid: "setting-save" } }, "Save"),
    current !== null && current !== undefined
      ? h("button", { type: "button", class: "btn", disabled: pending, dataset: { key: `edit-unset:${e.key}`, testid: "setting-unset" },
        onclick: () => { ctx.send("settings_unset", subject, target, {}, {}, `Unset ${e.key} (${TIER_LABEL[tier]})`); ui.edit = null; refocus(`edit:${tier}:${e.key}`); } },
      `Remove ${TIER_LABEL[tier]} value`) : null,
    h("button", { type: "button", class: "btn", dataset: { key: `edit-cancel:${e.key}` }, onclick: close }, "Cancel"));
}

function settingRow(ctx, e) {
  const h = ctx.h;
  const open = ui.inspect.has(e.key);
  const marker = e.inherited ? "inherited" : e.overridden ? "overridden" : "set";
  const editing = ui.edit && ui.edit.key === e.key ? ui.edit.tier : null;
  const pinned = e.source === "run-pinned";
  return h("div", { class: "srow", role: "listitem", dataset: { testid: "setting-row", key: e.key, source: e.source } },
    h("div", { class: "line" },
      h("code", { class: "k", text: e.key }),
      h("span", { class: "v", dataset: { testid: "setting-value" }, text: show(e.value) }),
      h("span", { class: `tier t-${e.source}`, dataset: { testid: "setting-source" }, text: TIER_LABEL[e.source] || e.source }),
      h("span", { class: `pill m-${marker}`, dataset: { testid: "setting-marker" }, text: marker }),
      h("span", { class: `pill a-${e.apply}`, dataset: { testid: "setting-apply", apply: e.apply }, text: APPLY_LABEL[e.apply] || e.apply }),
      h("button", { type: "button", class: "btn", "aria-expanded": String(open), dataset: { key: `inspect:${e.key}`, testid: "setting-inspect" },
        "aria-label": `Inspect tiers of ${e.key}`,
        onclick: () => { if (open) ui.inspect.delete(e.key); else ui.inspect.add(e.key); ctx.rerender(); } }, open ? "Hide tiers" : "Tiers")),
    pinned ? h("p", { class: "why", dataset: { testid: "setting-pinned" }, text: PINNED_WHY }) : null,
    open ? h("dl", { class: "tiers", dataset: { testid: "setting-tiers" } }, (ctx.tiers || []).map((t) => {
      const editable = (e.editable || []).includes(t);
      const has = e.values[t] !== null && e.values[t] !== undefined;
      return h("div", { class: `tierrow${t === e.source ? " effective" : ""}`, dataset: { testid: "setting-tier", tier: t } },
        h("dt", { text: TIER_LABEL[t] || t }), h("dd", { text: has ? show(e.values[t]) : "not set" }),
        editable
          ? (editing === t ? editor(ctx, e, t) : h("button", { type: "button", class: "btn", dataset: { key: `edit:${t}:${e.key}`, testid: "setting-edit", tier: t },
            "aria-label": `Edit ${TIER_LABEL[t]} value of ${e.key}`,
            onclick: () => { ui.edit = { key: e.key, tier: t, text: has ? show(e.values[t]) : show(e.value), focus: true }; ctx.rerender(); } }, "Edit"))
          : h("span", { class: "muted ro", dataset: { testid: "setting-readonly" },
            text: t === "run-pinned" ? PINNED_WHY : t === "default" ? "shipped default; read-only" : "not editable in this scope" }));
    })) : null);
}

function scopePicker(ctx) {
  const h = ctx.h;
  const s = ctx.state;
  const repos = Object.values(s.entities.repos).map((r) => r.full_name).filter(Boolean).sort();
  const runs = Object.values(s.entities.runs).filter((r) => !ui.repo || (r.repo && r.repo.slug === ui.repo))
    .sort((a, b) => String(a.run_id).localeCompare(String(b.run_id)));
  const set = (patch) => {
    Object.assign(ui, patch);
    // Rows of the previous scope must not be shown (or edited) under the new one.
    clearTimeout(ui.retryTimer);
    Object.assign(ui, { edit: null, data: null, error: null, retryAt: 0 });
    ui.inspect.clear();
    load(ctx);
    ctx.rerender();
  };
  return h("nav", { class: "cascade", "aria-label": "Settings scope", dataset: { testid: "settings-cascade" } },
    h("button", { type: "button", class: "btn", "aria-pressed": String(ui.scope === "machine"), dataset: { key: "scope-machine", testid: "scope-machine" },
      onclick: () => set({ scope: "machine", repo: "", run: "" }) }, "Machine"),
    h("span", { class: "arrow", "aria-hidden": "true", text: "→" }),
    h("select", { class: "field", "aria-label": "Repository", dataset: { key: "scope-repo", testid: "scope-repo" },
      onchange: (ev) => set(ev.target.value ? { scope: "repository", repo: ev.target.value, run: "" } : { scope: "machine", repo: "", run: "" }) },
      h("option", { value: "", text: "Repository…" }), repos.map((r) => h("option", { value: r, selected: r === ui.repo, text: r }))),
    h("span", { class: "arrow", "aria-hidden": "true", text: "→" }),
    h("select", { class: "field", "aria-label": "Run", dataset: { key: "scope-run", testid: "scope-run" },
      onchange: (ev) => {
        const run = runs.find((r) => r.run_id === ev.target.value);
        set(run ? { scope: "run", run: run.run_id, repo: (run.repo && run.repo.slug) || ui.repo } : { scope: ui.repo ? "repository" : "machine", run: "" });
      } },
      h("option", { value: "", text: "Run…" }), runs.map((r) => h("option", { value: r.run_id, selected: r.run_id === ui.run, text: `${ctx.shortRun(r.run_id)} ${r.goal || ""}` }))));
}

export function render(ctx) {
  const h = ctx.h;
  if (!ctx.state) return h("p", { class: "muted", text: "Loading settings…" });
  load(ctx);
  watchCommands(ctx);
  const d = ui.data;
  const scopeText = ui.scope === "run" ? `Run ${ctx.shortRun(ui.run)}` : ui.scope === "repository" ? `Repository ${ui.repo}` : "Machine";
  const head = h("div", { class: "canvas-head" },
    h("div", {}, h("h1", { text: "Settings" }), h("p", { class: "sub", text: "Machine → Repository → Run. Each value names the tier it comes from." })),
    scopePicker(ctx));
  const last = [...ctx.local.values()].filter((l) => String(l.subject).startsWith("settings:")).pop();
  const lastLine = last ? h("p", { class: "alloc-last", dataset: { testid: "settings-last-command" },
    text: `${last.label}: ${ctx.receiptState((ctx.state.entities.commands[`command:${last.id}`] || {}).status || last.status)}${last.error ? ` (${last.error})` : ""}` }) : null;
  if (!d) {
    return [head, lastLine, h("p", { class: ui.error ? "banner err" : "muted", dataset: { testid: "settings-status" },
      text: ui.error || "Loading settings…" })];
  }
  const tctx = { ...ctx, tiers: d.tiers };
  const q = ui.query.toLowerCase();
  const byKey = new Map(d.entries.map((e) => [e.key, e]));
  const intake = INTAKE.map((k) => byKey.get(k)).filter(Boolean);
  const rest = d.entries.filter((e) => !INTAKE.includes(e.key) && (!q || e.key.toLowerCase().includes(q)));
  const groups = new Map();
  for (const e of rest) {
    const g = e.key.split(".")[0];
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(e);
  }
  return [
    head, lastLine,
    h("p", { class: "scope-now", dataset: { testid: "settings-scope", scope: d.scope }, text: `Showing effective values for: ${scopeText}` }),
    h("section", { class: "scard", dataset: { testid: "settings-intake" }, "aria-label": "Auto Intake repository defaults" },
      h("h2", { text: "Auto Intake repository defaults" }),
      h("p", { class: "sub", text: "Whether issues queue automatically, the authorization they inherit, and whether queued work may reach Ready to Land. Landing authority stays explicit policy." }),
      intake.length ? h("div", { role: "list" }, intake.map((e) => settingRow(tctx, e)))
        : h("p", { class: "muted", text: "This runtime reports no Auto Intake settings." })),
    h("div", { class: "toolbar" }, h("input", { type: "search", class: "field", placeholder: "Filter settings…", "aria-label": "Filter settings",
      value: ui.query, dataset: { key: "settings-filter", testid: "settings-filter" }, oninput: (ev) => { ui.query = ev.target.value; ctx.rerender(); } })),
    [...groups.entries()].map(([g, list]) => h("section", { class: "scard", "aria-label": g, dataset: { testid: "settings-group", group: g } },
      h("h2", { text: g }), h("div", { role: "list" }, list.map((e) => settingRow(tctx, e))))),
  ];
}
